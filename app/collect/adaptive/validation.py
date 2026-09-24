"""Profile validation: V1–V8, freshness and derived VALIDATED (ADR-0017 §7).

Deterministic and offline. A ``ValidationRun`` ends ``PASS``, ``FAIL`` or ``INCOMPLETE``; only
``PASS`` counts, and ``VALIDATED`` is derived from a ``PASS`` run for the exact freshness tuple —
never stored. P1 persists nothing: a run is a value returned to its caller.

* V1 referential — hooks bind to the running manifest by ``HOOK_REVISION``.
* V2 coverage — parser CORE fields ruled in every template; CORE ``images`` covered by
  representative image-role rules and an image region in every template; COVERAGE gaps are lint.
* V3 sample agreement — the engine equals operator expectations (no profile-owned value in them);
  one complete sample per template, two in total.
* V3a conflict scan — profile-independent detectors over each whole sample.
* V4 negative controls — typed LOGIN and NON_PRODUCT pages, and the five mutation classes, each of
  which must actually run.
* V5 determinism, V6 safety, V7 hook guards (G5–G7), V8 complete samples only.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.collect.adaptive.canonical import canonical_json, digest
from app.collect.adaptive.capture import CAPTURE_REVISION, ValidationSample, residual_findings
from app.collect.adaptive.document import Element, from_structure, read_html, to_structure
from app.collect.adaptive.engine import (
    Extraction,
    ImageCompleteness,
    TemplateOutcome,
    extract,
    fact_value,
    match_template,
)
from app.collect.adaptive.extraction_identity import EXTRACTOR_FINGERPRINT, EXTRACTOR_REVISION
from app.collect.adaptive.hooks import (
    HookManifest,
    bind_hooks,
    exceeds_binding_cap,
    promotion_key,
    shared_promotion_keys,
)
from app.collect.adaptive.locator import find
from app.collect.adaptive.profiles import (
    SCHEMA_VERSION,
    AttributeLocation,
    Bundle,
    BundleRefused,
    EmbeddedLocation,
    ExtractionProfileRevision,
    LabelledRowLocation,
    PageTemplateRevision,
    TextLocation,
)
from app.collect.facts import FIELD_REGISTRY, SUPPLIED_FIELDS, FieldLevel, FieldStatus

EVIDENCE_MAX_BYTES = 4096
RUN_DIGEST_SCHEME = "icbm-validation-run/v1"
REQUIRED_MUTATIONS = (
    "remove_required_anchor",
    "duplicate_price_row",
    "inject_hidden_sold_out",
    "add_conflicting_identity",
    "remove_image_region",
)
_PRICE_WORDS = frozenset({"판매가", "가격", "소비자가", "정가", "공급가", "할인가", "price"})
_CONTROL_WORDS = ("구매", "장바구니", "품절", "sold", "buy", "cart")
_PRODUCT_DATA_KEYS = frozenset({"offers", "sku", "price", "stock"})


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class NegativeClass(StrEnum):
    """Closed; V4 is never PASS without both (ADR-0017 §7.2)."""

    LOGIN = "LOGIN"
    NON_PRODUCT = "NON_PRODUCT"


class SampleRefused(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    outcome: Verdict
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationRun:
    verdict: Verdict
    checks: tuple[Check, ...]
    freshness: tuple[str, ...]

    def check(self, name: str) -> Check:
        return next(check for check in self.checks if check.name == name)

    def digest(self) -> str:
        return digest(
            RUN_DIGEST_SCHEME,
            {
                "verdict": self.verdict.value,
                "checks": [[c.name, c.outcome.value, list(c.details)] for c in self.checks],
                "freshness": list(self.freshness),
            },
        )


def sample_set_digest(samples: Sequence[ValidationSample]) -> str:
    return digest("icbm-sample-set/v1", [sample.digest for sample in samples])


def freshness_tuple(
    bundle: Bundle, samples: Sequence[ValidationSample], manifest: HookManifest | None
) -> tuple[str, ...]:
    """Implementation fingerprints are in here on purpose, and out of the semantic tuple on
    purpose (ADR-0017 §5.2, §7.1): an implementation-only change forces revalidation."""
    return (
        bundle.epr_digest,
        SCHEMA_VERSION,
        EXTRACTOR_REVISION,
        EXTRACTOR_FINGERPRINT,
        manifest.hook_fingerprint if manifest is not None else "",
        sample_set_digest(samples),
        CAPTURE_REVISION,
    )


def is_validated(runs: Iterable[ValidationRun], current: tuple[str, ...]) -> bool:
    """Derived, never stored: a PASS run for exactly the current freshness tuple."""
    return any(run.verdict is Verdict.PASS and run.freshness == current for run in runs)


def _combine(outcomes: Iterable[Verdict]) -> Verdict:
    found = set(outcomes)
    if Verdict.FAIL in found:
        return Verdict.FAIL
    if Verdict.INCOMPLETE in found:
        return Verdict.INCOMPLETE
    return Verdict.PASS


# ---------------------------------------------------------------- V2


def _coverage(bundle: Bundle, samples: Sequence[ValidationSample]) -> Check:
    problems: list[str] = []
    lint: list[str] = []
    epr = bundle.epr
    representative = [rule for rule in epr.image_roles if rule.role == "REPRESENTATIVE"]
    if not representative:
        problems.append("no image-role rule assigns the representative role")
    parser_core = {
        key for key in SUPPLIED_FIELDS if FIELD_REGISTRY[key].level is FieldLevel.CORE
    } - {"stock", "options"}
    coverage = {key for key in SUPPLIED_FIELDS if FIELD_REGISTRY[key].level is FieldLevel.COVERAGE}
    for _, template in bundle.templates:
        if missing := sorted(parser_core - set(template.fields)):
            problems.append(f"{template.template_key}: CORE fields without a rule {missing}")
        regions = {region.name for region in template.image_regions}
        if not regions:
            problems.append(f"{template.template_key}: no image region")
        for rule in representative:
            if rule.region not in regions:
                problems.append(f"{template.template_key}: no image region {rule.region!r}")
        if unruled := sorted(coverage - set(template.fields)):
            lint.append(f"LINT {template.template_key}: COVERAGE without a rule {unruled}")
    # Only complete samples are proof material: a SAMPLE_TRUNCATED sample's expectations never
    # satisfy the image requirement (V8).
    complete = [sample for sample in samples if not sample.truncated]
    for sample in complete:
        if not any(image[0] == "REPRESENTATIVE" for image in sample.expected.get("images", [])):
            problems.append(f"sample {sample.digest[:12]} expects no representative image")
    unproven = [] if complete else ["no complete sample states the expected images"]
    outcome = Verdict.FAIL if problems else Verdict.INCOMPLETE if unproven else Verdict.PASS
    return Check("V2", outcome, (*problems, *unproven, *lint))


# ---------------------------------------------------------------- V3 and V3a


def _disagreements(extraction: Extraction, expected: Mapping[str, Any]) -> list[str]:
    if extraction.outcome is not TemplateOutcome.MATCHED:
        return [f"template {extraction.outcome.value}"]
    problems: list[str] = []
    if extraction.source_product_id != expected["source_product_id"]:
        problems.append("source_product_id differs")
    for key in sorted(SUPPLIED_FIELDS):
        want = expected["fields"][key]
        fact = extraction.fields[key]
        if fact.status.value != want["status"]:
            problems.append(f"{key}: status {fact.status.value} != {want['status']}")
        elif fact_value(fact) != want.get("value"):
            problems.append(f"{key}: value differs")
    images = [[image.role, image.ordinal, image.reference] for image in extraction.images]
    if images != expected["images"]:
        problems.append("images: ordered (role, ordinal, reference) differ")
    return problems


def conflict_statements(root: Element) -> list[tuple[str, Element]]:
    """Profile-independent detectors over a whole sample (V3a)."""
    found: list[tuple[str, Element]] = []
    for element in root.descendants():
        if element.tag == "tr":
            cells = element.cells()
            if len(cells) >= 2 and cells[0].text().lower() in _PRICE_WORDS:
                found.append(("PRICE_ROW", element))
        elif element.tag in {"button", "a", "input"}:
            words = f"{element.text()} {' '.join(element.classes)}".lower()
            if any(word in words for word in _CONTROL_WORDS):
                found.append(("CONTROL", element))
        elif element.tag == "select":
            found.append(("OPTION_SELECT", element))
        elif element.tag == "meta" and "product" in element.attributes.get("name", "").lower():
            found.append(("IDENTITY_DECLARATION", element))
        elif element.tag == "script" and isinstance(element.data, dict):
            if _PRODUCT_DATA_KEYS & set(element.data):
                found.append(("EMBEDDED_PRODUCT_DATA", element))
    return found


def _unaddressed(
    bundle: Bundle, root: Element, extraction: Extraction, resolved: frozenset[str], label: str
) -> list[str]:
    disposed = {
        inner.position
        for locator in bundle.epr.dispositions
        for element in find(root, locator)
        for inner in element.walk()
    }
    findings = []
    for kind, element in conflict_statements(root):
        finding = f"{label}:{kind}@{element.position}"
        if element.position in extraction.read_positions | disposed or finding in resolved:
            continue
        findings.append(finding)
    return findings


# ---------------------------------------------------------------- V4 mutations


def _copy(root: Element) -> Element:
    return from_structure(to_structure(root))


def _detach(element: Element) -> None:
    if element.parent is not None:
        element.parent.children.remove(element)


def _append_copy(element: Element, change: Callable[[Element], None]) -> bool:
    parent = element.parent
    if parent is None:
        return False
    twin = from_structure(to_structure(element))
    twin.parent = parent
    change(twin)
    parent.children.append(twin)
    return True


def _set_attribute(name: str) -> Callable[[Element], None]:
    def change(twin: Element) -> None:
        twin.attributes[name] = "CONFLICT-0"

    return change


def _set_text(twin: Element) -> None:
    twin.children[:] = ["CONFLICT-0"]


Mutation = tuple[str, Callable[[Element], Element | None], Callable[[Extraction, Extraction], bool]]


def _mutations(epr: ExtractionProfileRevision, template: PageTemplateRevision) -> list[Mutation]:
    required = template.signature.required

    def remove_required_anchor(root: Element) -> Element | None:
        # A shared anchor, so no sibling template can absorb the mutated page.
        found = find(root, required[1] if len(required) > 1 else required[0])
        for element in found:
            _detach(element)
        return _copy(root) if found else None

    def duplicate_price_row(root: Element) -> Element | None:
        rule = template.fields.get("prices")
        if rule is None or not isinstance(rule.primary, LabelledRowLocation):
            return None
        labels = set(epr.vocabularies[rule.primary.vocabulary])
        for container in find(root, rule.primary.container):
            for row in container.descendants():
                cells = row.cells() if row.tag == "tr" else []
                if (
                    len(cells) >= 2
                    and cells[0].text() in labels
                    and _append_copy(row, lambda _: None)
                ):
                    return _copy(root)
        return None

    def inject_hidden_sold_out(root: Element) -> Element | None:
        scopes = find(root, template.stock_scope)
        if not scopes:
            return None
        hidden = Element("span", {"class": "hidden"}, -1, parent=scopes[0])
        hidden.children.append(epr.sold_out_words[0])
        scopes[0].children.append(hidden)
        return _copy(root)

    def add_conflicting_identity(root: Element) -> Element | None:
        for source in epr.identity.sources:
            if isinstance(source, AttributeLocation):
                for element in find(root, source.locator):
                    if _append_copy(element, _set_attribute(source.attribute)):
                        return _copy(root)
            elif isinstance(source, TextLocation):
                for element in find(root, source.locator):
                    if _append_copy(element, _set_text):
                        return _copy(root)
            elif isinstance(source, EmbeddedLocation):
                for element in root.walk():
                    value: Any = element.data if element.tag == "script" else None
                    for step in source.path[:-1]:
                        value = value.get(step) if isinstance(value, dict) else None
                    if isinstance(value, dict) and source.path[-1] in value:
                        value[source.path[-1]] = "CONFLICT-0"
                        return _copy(root)
        return None

    def remove_image_region(root: Element) -> Element | None:
        regions = {region.name: region for region in template.image_regions}
        removed = False
        for rule in epr.image_roles:
            region = regions.get(rule.region)
            if rule.role == "REPRESENTATIVE" and region is not None:
                for element in find(root, region.locator):
                    _detach(element)
                    removed = True
        return _copy(root) if removed else None

    def unmatched(_: Extraction, after: Extraction) -> bool:
        return after.outcome is not TemplateOutcome.MATCHED

    def prices_under_review(before: Extraction, after: Extraction) -> bool:
        return (
            unmatched(before, after) or after.fields["prices"].status is FieldStatus.REVIEW_REQUIRED
        )

    def no_confident_sold_out(before: Extraction, after: Extraction) -> bool:
        if unmatched(before, after):
            return True
        stock = after.fields["stock"]
        return fact_value(stock) == fact_value(before.fields["stock"]) or (
            stock.status is FieldStatus.REVIEW_REQUIRED
        )

    def identity_unresolved(before: Extraction, after: Extraction) -> bool:
        return unmatched(before, after) or after.source_product_id is None

    def images_under_review(before: Extraction, after: Extraction) -> bool:
        return (
            unmatched(before, after)
            or after.image_completeness is ImageCompleteness.REVIEW_REQUIRED
        )

    return [
        ("remove_required_anchor", remove_required_anchor, unmatched),
        ("duplicate_price_row", duplicate_price_row, prices_under_review),
        ("inject_hidden_sold_out", inject_hidden_sold_out, no_confident_sold_out),
        ("add_conflicting_identity", add_conflicting_identity, identity_unresolved),
        ("remove_image_region", remove_image_region, images_under_review),
    ]


# ---------------------------------------------------------------- the run


def validate(
    bundle: Bundle,
    samples: Sequence[ValidationSample],
    *,
    negatives: Mapping[NegativeClass, str],
    manifest: HookManifest | None = None,
    other_eprs: Sequence[ExtractionProfileRevision] = (),
    architecture_review_recorded: bool = False,
    resolved_findings: frozenset[str] = frozenset(),
) -> ValidationRun:
    for sample in samples:
        if "profile" in canonical_json(sample.provenance).lower():
            raise SampleRefused("a sample whose provenance names a profile is never proof material")
    if stray := [key for key in negatives if not isinstance(key, NegativeClass)]:
        raise ValueError(f"negative controls are keyed by NegativeClass, not {stray!r}")
    freshness = freshness_tuple(bundle, samples, manifest)
    checks: list[Check] = []

    try:
        bind_hooks(bundle.epr, manifest)
    except BundleRefused as refused:
        checks.append(Check("V1", Verdict.FAIL, (str(refused),)))
        return ValidationRun(Verdict.FAIL, tuple(checks), freshness)
    checks.append(Check("V1", Verdict.PASS))
    checks.append(_coverage(bundle, samples))

    complete = [sample for sample in samples if not sample.truncated]
    agreement: list[str] = []
    conflicts: list[str] = []
    determinism: list[str] = []
    safety: list[str] = []
    calls: Counter[str] = Counter()
    per_template: Counter[str] = Counter()
    replays: list[tuple[str, Element, Extraction]] = []
    for sample in complete:
        label = sample.digest[:12]
        root = from_structure(sample.structure)
        extraction = extract(bundle, root, manifest)
        if (
            extraction.digest()
            != extract(bundle, from_structure(sample.structure), manifest).digest()
        ):
            determinism.append(f"{label}: two evaluations differ")
        agreement += [
            f"{label}: {problem}" for problem in _disagreements(extraction, sample.expected)
        ]
        conflicts += _unaddressed(bundle, root, extraction, resolved_findings, label)
        for fact in extraction.fields.values():
            for evidence in fact.evidence:
                for text in (evidence.locator, evidence.observed or "", evidence.normalized or ""):
                    if len(text.encode("utf-8")) > EVIDENCE_MAX_BYTES or residual_findings(text):
                        safety.append(f"{label}: unsafe evidence at {evidence.locator[:40]}")
        calls.update(extraction.hook_calls)
        if extraction.template_key is not None:
            per_template[extraction.template_key] += 1
        replays.append((label, root, extraction))
    for _, template in bundle.templates:
        if not per_template[template.template_key]:
            agreement.append(f"no complete sample for template {template.template_key}")
    if len(complete) < 2:
        agreement.append("at least two complete samples are required")
    checks.append(Check("V3", Verdict.FAIL if agreement else Verdict.PASS, tuple(agreement)))
    checks.append(Check("V3a", Verdict.INCOMPLETE if conflicts else Verdict.PASS, tuple(conflicts)))

    failed: list[str] = []
    unproven: list[str] = []
    for negative_class in NegativeClass:
        page = negatives.get(negative_class, "")
        if not page.strip():
            unproven.append(f"NEGATIVE_CONTROL_MISSING:{negative_class.value}")
        elif match_template(bundle, read_html(page))[0] is TemplateOutcome.MATCHED:
            failed.append(f"negative page {negative_class.value} matched a template")
    exercised: Counter[str] = Counter()
    for label, root, before in replays:
        _, matched = match_template(bundle, root)
        if matched is None:
            continue
        for name, mutate, holds in _mutations(bundle.epr, matched):
            mutated = mutate(_copy(root))
            if mutated is None:
                continue
            exercised[name] += 1
            if not holds(before, extract(bundle, mutated, manifest)):
                failed.append(f"{label}: {name} did not fail closed")
    unproven += [f"MUTATION_NOT_EXERCISED:{n}" for n in REQUIRED_MUTATIONS if not exercised[n]]
    coverage = tuple(f"EXERCISED:{name}x{exercised[name]}" for name in REQUIRED_MUTATIONS)
    v4 = Verdict.FAIL if failed else Verdict.INCOMPLETE if unproven else Verdict.PASS
    checks.append(Check("V4", v4, (*failed, *unproven, *coverage)))
    checks.append(Check("V5", Verdict.FAIL if determinism else Verdict.PASS, tuple(determinism)))
    checks.append(Check("V6", Verdict.FAIL if safety else Verdict.PASS, tuple(safety)))

    guards: list[str] = []
    outcomes = [Verdict.PASS]
    if exceeds_binding_cap(bundle.epr) and not architecture_review_recorded:
        guards.append("G6: more than two (hook_point, target) bindings; architecture review due")
        outcomes.append(Verdict.INCOMPLETE)
    for binding in bundle.epr.hooks:
        key = ":".join(promotion_key(binding))
        if not calls[key]:
            guards.append(f"G7: bound hook {key} is exercised by no sample")
            outcomes.append(Verdict.FAIL)
    shared = shared_promotion_keys([bundle.epr, *other_eprs])
    guards += [
        f"G5: {':'.join(key)} shared by {sorted(owners)}" for key, owners in sorted(shared.items())
    ]
    checks.append(Check("V7", _combine(outcomes), tuple(guards)))

    truncated = [f"SAMPLE_TRUNCATED:{s.digest[:12]}" for s in samples if s.truncated]
    checks.append(Check("V8", Verdict.INCOMPLETE if truncated else Verdict.PASS, tuple(truncated)))
    return ValidationRun(_combine(check.outcome for check in checks), tuple(checks), freshness)
