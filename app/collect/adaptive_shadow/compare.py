"""The shadow comparison, in memory and pure (ADR-0017 §10.3, §10.4).

Both sides produce ``FieldFact``s and both go through the same pure ``evaluate``: the canonical
side from the exact ``CollectedFacts`` its revision was appended from, the Adaptive side from the
same facts with its own fields. Image references are paired in memory by M1 (the exact resolved
reference) and M2 (the exact written reference), never by a persisted locator, and what is kept
of the pairing is roles, ordinals, outcomes and the canonical checksum — never a URL, a written
reference or a digest of either.
"""

import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.collect.adaptive.engine import Extraction, TemplateOutcome
from app.collect.adaptive_shadow.evidence import RunVerdict
from app.collect.facts import (
    IMAGES_FIELD,
    CollectedFacts,
    EvaluatedFacts,
    FieldStatus,
    ImageReference,
    evaluate,
)
from app.collect.urls import UrlPolicy
from integrations.suppliers.collection import ImageCandidate, SourceIdentity, SourceIdentityResult


class Severity(StrEnum):
    CONFIDENT_DISAGREEMENT = "CONFIDENT_DISAGREEMENT"
    ADAPTIVE_OVERCONFIDENT = "ADAPTIVE_OVERCONFIDENT"
    ADAPTIVE_CONSERVATIVE = "ADAPTIVE_CONSERVATIVE"


_SEVERITY_ORDER = (
    Severity.CONFIDENT_DISAGREEMENT,
    Severity.ADAPTIVE_OVERCONFIDENT,
    Severity.ADAPTIVE_CONSERVATIVE,
)


class FieldVerdict(StrEnum):
    MATCH = "MATCH"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    VALUE_MISMATCH = "VALUE_MISMATCH"


class ImageOutcome(StrEnum):
    MATCHED = "MATCHED"
    MATCHED_NO_BYTES = "MATCHED_NO_BYTES"
    UNOBSERVED = "UNOBSERVED"
    MISSED = "MISSED"
    UNMATCHABLE = "UNMATCHABLE"


# The M3 boundary (docs/acceptance/M3.md §2): until an ADR accepts positive option and tier
# support, an Adaptive CONFIRMED option or tier value is overconfident, whatever the other side.
BOUNDED_FIELDS = frozenset({"options", "quantity_tiers"})
_CONFIDENT = frozenset({FieldStatus.CONFIRMED, FieldStatus.ABSENT})


class ShadowEvaluationFailed(RuntimeError):
    """The Adaptive side produced facts the canonical evaluation refuses: a shadow failure."""


@dataclass(frozen=True)
class FieldComparison:
    key: str
    verdict: FieldVerdict
    canonical_status: str
    adaptive_status: str
    canonical_value: str | None
    adaptive_value: str | None
    severity: Severity | None

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "verdict": self.verdict.value,
            "canonical": [self.canonical_status, self.canonical_value],
            "adaptive": [self.adaptive_status, self.adaptive_value],
            "severity": None if self.severity is None else self.severity.value,
        }


@dataclass(frozen=True)
class ImageComparison:
    outcome: ImageOutcome
    canonical: tuple[str, int] | None
    adaptive: tuple[str, int] | None
    sha256: str | None
    agrees: bool
    severity: Severity | None

    def as_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "canonical": None if self.canonical is None else list(self.canonical),
            "adaptive": None if self.adaptive is None else list(self.adaptive),
            # Inherited from the canonical fetch, never independently observed (§10.4).
            "sha256_inherited": self.sha256,
            "agrees": self.agrees,
            "severity": None if self.severity is None else self.severity.value,
        }


@dataclass(frozen=True)
class Comparison:
    verdict: RunVerdict
    severity: Severity | None
    identity: dict[str, Any]
    template: dict[str, Any]
    fields: tuple[FieldComparison, ...] = ()
    images: tuple[ImageComparison, ...] = ()
    facts_status: dict[str, str | None] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "severity": None if self.severity is None else self.severity.value,
            "identity": self.identity,
            "template": self.template,
            "fields": [f.as_json() for f in self.fields],
            "images": [i.as_json() for i in self.images],
            "facts_status": self.facts_status,
            "observed": self.observed,
        }


def most_severe(severities: Sequence[Severity | None]) -> Severity | None:
    present = {s for s in severities if s is not None}
    return next((s for s in _SEVERITY_ORDER if s in present), None)


def _field_severity(key: str, canonical: FieldStatus, adaptive: FieldStatus) -> Severity:
    if canonical in _CONFIDENT and adaptive in _CONFIDENT:
        return Severity.CONFIDENT_DISAGREEMENT
    if adaptive in _CONFIDENT:
        return Severity.ADAPTIVE_OVERCONFIDENT
    return Severity.ADAPTIVE_CONSERVATIVE


def _positive(value_json: str | None) -> bool:
    """A value that states at least one option configuration or quantity tier. A CONFIRMED value
    with none — the product has no options — is not a positive claim."""
    if value_json is None:
        return False
    value = json.loads(value_json)
    return isinstance(value, dict) and any(
        isinstance(part, list) and part for part in value.values()
    )


def compare_fields(
    canonical: EvaluatedFacts, adaptive: EvaluatedFacts
) -> tuple[FieldComparison, ...]:
    ours = {f.key: f for f in canonical.fields}
    theirs = {f.key: f for f in adaptive.fields}
    compared: list[FieldComparison] = []
    for key in sorted(ours):
        if key == IMAGES_FIELD:
            continue  # images are paired reference by reference (§10.4)
        left, right = ours[key], theirs[key]
        bounded = (
            key in BOUNDED_FIELDS
            and right.status is FieldStatus.CONFIRMED
            and _positive(right.value_json)
        )
        if left.status != right.status:
            verdict = FieldVerdict.STATUS_MISMATCH
        elif left.value_json != right.value_json:
            verdict = FieldVerdict.VALUE_MISMATCH
        else:
            verdict = FieldVerdict.MATCH
        if bounded and verdict is FieldVerdict.MATCH:
            verdict = FieldVerdict.STATUS_MISMATCH
        severity = (
            Severity.ADAPTIVE_OVERCONFIDENT
            if bounded
            else None
            if verdict is FieldVerdict.MATCH
            else _field_severity(key, left.status, right.status)
        )
        compared.append(
            FieldComparison(
                key,
                verdict,
                left.status.value,
                right.status.value,
                left.value_json,
                right.value_json,
                severity,
            )
        )
    return tuple(compared)


# ---------------------------------------------------------------- images (§10.4)


@dataclass
class _Ref:
    side: str
    role: str
    ordinal: int  # per role, in the side's own order
    resolved: str | None
    written: str | None
    sha256: str | None = None
    paired: bool = False


def _resolve(document_url: str, reference: str) -> str | None:
    try:
        joined = urljoin(document_url, reference.strip())
        parts = urlsplit(joined)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return joined


def _canonical_refs(
    candidates: Sequence[ImageCandidate], images: Sequence[ImageReference]
) -> list[_Ref]:
    by_order = {candidate.order: candidate for candidate in candidates}
    ordinals: Counter[str] = Counter()
    refs: list[_Ref] = []
    for image in sorted(images, key=lambda i: i.ordinal):
        candidate = by_order.get(image.ordinal)
        role = image.role.value
        refs.append(
            _Ref(
                "canonical",
                role,
                ordinals[role],
                # A refused target is no resolvable target (M2 applies).
                None if candidate is None or image.target_refusal is not None else candidate.url,
                None if candidate is None else candidate.source,
                image.sha256,
            )
        )
        ordinals[role] += 1
    return refs


def _adaptive_refs(document_url: str, extraction: Extraction) -> list[_Ref]:
    return [
        _Ref("adaptive", i.role, i.ordinal, _resolve(document_url, i.reference), i.reference)
        for i in extraction.images
    ]


def _pair_group(adaptive: list[_Ref], canonical: list[_Ref], out: list[ImageComparison]) -> None:
    """Equal references paired by occurrence index; an uneven group whose roles differ cannot be
    paired uniquely and is UNMATCHABLE (§10.4)."""
    roles = {ref.role for ref in (*adaptive, *canonical)}
    if len(adaptive) != len(canonical) and len(roles) > 1:
        for ref in (*adaptive, *canonical):
            ref.paired = True
            out.append(_unmatchable(ref))
        return
    for left, right in zip(canonical, adaptive, strict=False):
        left.paired = right.paired = True
        agrees = (left.role, left.ordinal) == (right.role, right.ordinal)
        out.append(
            ImageComparison(
                ImageOutcome.MATCHED if left.sha256 else ImageOutcome.MATCHED_NO_BYTES,
                (left.role, left.ordinal),
                (right.role, right.ordinal),
                left.sha256,
                agrees,
                None if agrees else Severity.CONFIDENT_DISAGREEMENT,
            )
        )


def _unmatchable(ref: _Ref) -> ImageComparison:
    here = (ref.role, ref.ordinal)
    return ImageComparison(
        ImageOutcome.UNMATCHABLE,
        here if ref.side == "canonical" else None,
        here if ref.side == "adaptive" else None,
        ref.sha256 if ref.side == "canonical" else None,
        False,
        None,
    )


def compare_images(
    document_url: str,
    candidates: Sequence[ImageCandidate],
    images: Sequence[ImageReference],
    extraction: Extraction,
) -> tuple[ImageComparison, ...]:
    canonical = _canonical_refs(candidates, images)
    adaptive = _adaptive_refs(document_url, extraction)
    out: list[ImageComparison] = []
    # A reference neither side can state as resolved or written can never be paired.
    for ref in (*canonical, *adaptive):
        if ref.resolved is None and not ref.written:
            ref.paired = True
            out.append(_unmatchable(ref))
    # M1: the exact resolved reference, both sides resolvable.
    groups: dict[str, tuple[list[_Ref], list[_Ref]]] = defaultdict(lambda: ([], []))
    for ref in adaptive:
        if not ref.paired and ref.resolved is not None:
            groups[ref.resolved][0].append(ref)
    for ref in canonical:
        if not ref.paired and ref.resolved is not None:
            groups[ref.resolved][1].append(ref)
    for key in sorted(groups):
        left, right = groups[key]
        if left and right:
            _pair_group(left, right, out)
    # M2: the exact written reference, where either side has no resolvable target.
    written: dict[str, tuple[list[_Ref], list[_Ref]]] = defaultdict(lambda: ([], []))
    for ref in adaptive:
        if not ref.paired and ref.written:
            written[ref.written][0].append(ref)
    for ref in canonical:
        if not ref.paired and ref.written:
            written[ref.written][1].append(ref)
    for key in sorted(written):
        left, right = written[key]
        if not (left and right):
            continue
        if all(r.resolved is not None for r in (*left, *right)):
            continue  # both resolvable and not paired by M1: M2 does not apply
        _pair_group(left, right, out)
    for ref in canonical:
        if not ref.paired:
            out.append(
                ImageComparison(
                    ImageOutcome.MISSED,
                    (ref.role, ref.ordinal),
                    None,
                    ref.sha256,
                    False,
                    Severity.ADAPTIVE_CONSERVATIVE,
                )
            )
    for ref in adaptive:
        if not ref.paired:
            out.append(
                ImageComparison(
                    ImageOutcome.UNOBSERVED,
                    None,
                    (ref.role, ref.ordinal),
                    None,
                    False,
                    Severity.ADAPTIVE_OVERCONFIDENT,
                )
            )
    return tuple(out)


# ---------------------------------------------------------------- the run


def _identity(
    canonical: SourceIdentityResult, extraction: Extraction
) -> tuple[dict[str, Any], Severity | None]:
    ours = canonical.source_product_id if isinstance(canonical, SourceIdentity) else None
    theirs = extraction.source_product_id
    agrees = ours == theirs
    severity = (
        None
        if agrees
        else Severity.CONFIDENT_DISAGREEMENT
        if ours is not None and theirs is not None
        else Severity.ADAPTIVE_OVERCONFIDENT
        if theirs is not None
        else Severity.ADAPTIVE_CONSERVATIVE
    )
    return (
        {
            "canonical": "RESOLVED" if ours is not None else "UNRESOLVED",
            "adaptive": "RESOLVED" if theirs is not None else "UNRESOLVED",
            "agrees": agrees,
        },
        severity,
    )


def _observed(extraction: Extraction) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    for fact in extraction.fields.values():
        for evidence in fact.evidence:
            kinds[evidence.kind.value] += 1
    return {
        "evidence_kinds": dict(sorted(kinds.items())),
        "evidence_per_field": {
            key: len(fact.evidence) for key, fact in sorted(extraction.fields.items())
        },
        "hook_calls": dict(sorted(extraction.hook_calls.items())),
        "signals": len(extraction.signals),
    }


def compare(
    *,
    source_url: str,
    identity: SourceIdentityResult,
    collected: CollectedFacts | None,
    url_policy: UrlPolicy,
    candidates: Sequence[ImageCandidate],
    images: Sequence[ImageReference],
    extraction: Extraction,
) -> Comparison:
    template = {
        "outcome": extraction.outcome.value,
        "matched": extraction.template_key is not None,
    }
    if extraction.outcome is not TemplateOutcome.MATCHED:
        verdict = (
            RunVerdict.TEMPLATE_AMBIGUOUS
            if extraction.outcome is TemplateOutcome.TEMPLATE_AMBIGUOUS
            else RunVerdict.TEMPLATE_UNMATCHED
        )
        found, _ = _identity(identity, extraction)
        return Comparison(verdict, Severity.ADAPTIVE_CONSERVATIVE, found, template)
    found, identity_severity = _identity(identity, extraction)
    fields: tuple[FieldComparison, ...] = ()
    pictures: tuple[ImageComparison, ...] = ()
    statuses: dict[str, str | None] = {"canonical": None, "adaptive": None}
    if collected is not None:
        canonical_eval = evaluate(collected, url_policy)
        try:
            adaptive_eval = evaluate(replace(collected, fields=dict(extraction.fields)), url_policy)
        except Exception as refused:
            raise ShadowEvaluationFailed(type(refused).__name__) from None
        fields = compare_fields(canonical_eval, adaptive_eval)
        pictures = compare_images(source_url, candidates, images, extraction)
        statuses = {
            "canonical": canonical_eval.facts_status.value,
            "adaptive": adaptive_eval.facts_status.value,
        }
    if any(p.outcome is ImageOutcome.UNMATCHABLE for p in pictures):
        verdict = RunVerdict.IMAGE_UNMATCHABLE
    elif not found["agrees"]:
        verdict = RunVerdict.IDENTITY_MISMATCH
    elif any(f.verdict is not FieldVerdict.MATCH for f in fields) or any(
        not p.agrees for p in pictures
    ):
        verdict = RunVerdict.MISMATCH
    else:
        verdict = RunVerdict.MATCH
    severity = most_severe(
        [identity_severity, *(f.severity for f in fields), *(p.severity for p in pictures)]
    )
    return Comparison(
        verdict,
        None if verdict is RunVerdict.MATCH else severity,
        found,
        template,
        fields,
        pictures,
        statuses,
        _observed(extraction),
    )
