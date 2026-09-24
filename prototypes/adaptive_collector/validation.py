"""Profile validation: V1–V8, the freshness tuple and derived VALIDATED (ADR-0017 §7).

Deterministic and offline. A `ValidationRun` ends PASS, FAIL or INCOMPLETE; only PASS counts, and
VALIDATED is derived from a PASS run for the exact freshness tuple — never stored.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.collect.facts import FIELD_REGISTRY, SUPPLIED_FIELDS, FieldLevel, FieldStatus
from prototypes.adaptive_collector.capture import (
    CAPTURE_REVISION,
    ValidationSample,
    secret_findings,
)
from prototypes.adaptive_collector.dom import Node, from_snapshot, parse_html, to_snapshot
from prototypes.adaptive_collector.engine import (
    ENGINE_REVISION,
    Extraction,
    ImageCoverage,
    TemplateVerdict,
    engine_fingerprint,
    extract,
    match_template,
    value_json,
)
from prototypes.adaptive_collector.hooks import (
    HookManifest,
    HookRefused,
    g6_exceeded,
    promotion_key,
    promotion_review,
    resolve_hooks,
)
from prototypes.adaptive_collector.locators import select
from prototypes.adaptive_collector.profile import (
    SCHEMA_VERSION,
    AttributeRule,
    Bundle,
    EmbeddedRule,
    ExtractionProfileRevision,
    LabelRowRule,
    PageTemplateRevision,
    TextRule,
    canonical,
)

EVIDENCE_MAX_BYTES = 4096
# The five mutation classes ADR-0017 §7.2 V4 names; each must run at least once.
REQUIRED_MUTATIONS = (
    "remove_required_anchor",
    "duplicate_price_row",
    "inject_hidden_sold_out",
    "add_conflicting_identity",
    "remove_image_region",
)
GENERIC_PRICE_LABELS = frozenset(
    {"판매가", "가격", "소비자가", "정가", "공급가", "할인가", "price"}
)
GENERIC_CONTROL_WORDS = ("구매", "장바구니", "품절", "sold", "buy", "cart")


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


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
        return next(c for c in self.checks if c.name == name)


def sample_set_digest(samples: Sequence[ValidationSample]) -> str:
    return hashlib.sha256("\n".join(s.digest for s in samples).encode("ascii")).hexdigest()


def freshness_tuple(
    bundle: Bundle, samples: Sequence[ValidationSample], manifest: HookManifest | None
) -> tuple[str, ...]:
    """Implementation fingerprints are deliberately included here, and deliberately excluded from
    the semantic tuple (ADR-0017 §5.2, §7.1)."""
    return (
        bundle.epr_digest,
        SCHEMA_VERSION,
        ENGINE_REVISION,
        engine_fingerprint(),
        manifest.hook_fingerprint if manifest is not None else "",
        sample_set_digest(samples),
        CAPTURE_REVISION,
    )


def is_validated(runs: Iterable[ValidationRun], current: tuple[str, ...]) -> bool:
    return any(run.verdict is Verdict.PASS and run.freshness == current for run in runs)


# ---------------------------------------------------------------- V2


def _v2(bundle: Bundle, samples: Sequence[ValidationSample]) -> Check:
    problems: list[str] = []
    lint: list[str] = []
    epr = bundle.epr
    representative = [r for r in epr.image_roles if r.role == "REPRESENTATIVE"]
    if not representative:
        problems.append("the EPR has no image-role rule that assigns the representative role")
    parser_core = {
        key for key in SUPPLIED_FIELDS if FIELD_REGISTRY[key].level is FieldLevel.CORE
    } - {"stock", "options"}
    for template in bundle.templates.values():
        missing = sorted(parser_core - set(template.fields))
        if missing:
            problems.append(f"{template.template_key}: CORE fields without a rule: {missing}")
        regions = {region.name for region in template.image_regions}
        if not regions:
            problems.append(f"{template.template_key}: no image region")
        for rule in representative:
            if rule.region not in regions:
                problems.append(f"{template.template_key}: no region {rule.region!r} for images")
        coverage = {k for k in SUPPLIED_FIELDS if FIELD_REGISTRY[k].level is FieldLevel.COVERAGE}
        if unruled := sorted(coverage - set(template.fields)):
            lint.append(f"{template.template_key}: COVERAGE without a rule (lint): {unruled}")
    for sample in samples:
        images = sample.expected.get("images", [])
        if not any(image[0] == "REPRESENTATIVE" for image in images):
            problems.append(f"sample {sample.digest[:12]} states no expected representative image")
    return Check("V2", Verdict.FAIL if problems else Verdict.PASS, (*problems, *lint))


# ---------------------------------------------------------------- V3 and V3a


def _agreement(extraction: Extraction, expected: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if extraction.verdict is not TemplateVerdict.MATCHED:
        return [f"template {extraction.verdict.value}"]
    if extraction.identity != expected["identity"]:
        problems.append(f"identity {extraction.identity} != {expected['identity']}")
    for key in sorted(SUPPLIED_FIELDS):
        want = expected["fields"][key]
        fact = extraction.fields[key]
        if fact.status.value != want["status"]:
            problems.append(f"{key}: status {fact.status.value} != {want['status']}")
        elif value_json(fact) != want.get("value"):
            problems.append(f"{key}: value differs")
    got = [[i.role, i.ordinal, i.reference] for i in extraction.images]
    if got != expected["images"]:
        problems.append("images: ordered (role, ordinal, reference) differ")
    return problems


def conflict_statements(root: Node) -> list[tuple[str, Node]]:
    """Profile-independent detectors over the whole snapshot (V3a)."""
    found: list[tuple[str, Node]] = []
    for node in root.descendants():
        if node.tag == "tr":
            cells = node.own_cells()
            if len(cells) >= 2 and cells[0].text().lower() in GENERIC_PRICE_LABELS:
                found.append(("PRICE_ROW", node))
        elif node.tag in {"button", "a", "input"}:
            words = f"{node.text()} {' '.join(node.classes)}".lower()
            if any(word in words for word in GENERIC_CONTROL_WORDS):
                found.append(("CONTROL", node))
        elif node.tag == "select":
            found.append(("OPTION_SELECT", node))
        elif node.tag == "meta" and "product" in node.attrs.get("name", "").lower():
            found.append(("IDENTITY_DECLARATION", node))
        elif node.tag == "script" and isinstance(node.data, Mapping):
            if {"offers", "sku", "price", "stock"} & set(node.data):
                found.append(("EMBEDDED_PRODUCT_DATA", node))
    return found


def _v3a(
    bundle: Bundle,
    root: Node,
    extraction: Extraction,
    resolved: frozenset[str],
    label: str,
) -> list[str]:
    disposed: set[int] = set()
    for selector in bundle.epr.dispositions:
        for node in select(root, selector):
            disposed.update(n.index for n in node.iter())
    findings = []
    for kind, node in conflict_statements(root):
        finding = f"{label}:{kind}@{node.index}"
        if node.index in extraction.read_nodes or node.index in disposed or finding in resolved:
            continue
        findings.append(finding)
    return findings


# ---------------------------------------------------------------- V4 mutations


def _clone(root: Node) -> Node:
    return from_snapshot(to_snapshot(root))


def _remove(node: Node) -> None:
    if node.parent is not None:
        node.parent.children.remove(node)


def _mutations(
    bundle: Bundle, template: PageTemplateRevision
) -> list[tuple[str, Callable[[Node], Node | None], Callable[[Extraction, Extraction], bool]]]:
    epr = bundle.epr

    def drop_anchor(root: Node) -> Node | None:
        required = template.signature.required
        nodes = select(root, required[1] if len(required) > 1 else required[0])
        for node in nodes:
            _remove(node)
        return _clone(root) if nodes else None

    def duplicate_price_row(root: Node) -> Node | None:
        rule = template.fields.get("prices")
        if rule is None or not isinstance(rule.primary, LabelRowRule):
            return None
        vocabulary = set(epr.vocabularies.get(rule.primary.vocabulary, ()))
        for container in select(root, rule.primary.container):
            for row in container.descendants():
                cells = row.own_cells() if row.tag == "tr" else []
                if len(cells) >= 2 and cells[0].text() in vocabulary and row.parent is not None:
                    twin = from_snapshot(to_snapshot(row))
                    twin.parent = row.parent
                    row.parent.children.append(twin)
                    return _clone(root)
        return None

    def hidden_sold_out(root: Node) -> Node | None:
        scopes = select(root, template.stock.scope)
        if not scopes:
            return None
        injected = Node("span", {"class": "hidden"}, -1, scopes[0])
        injected.children.append(epr.stock.sold_out_words[0])
        scopes[0].children.append(injected)
        return _clone(root)

    def conflicting_identity(root: Node) -> Node | None:
        """A second, different identity statement for the first source that can carry one."""
        for source in epr.identity.sources:
            if isinstance(source, AttributeRule | TextRule):
                nodes = select(root, source.selector)
                if nodes and nodes[0].parent is not None:
                    twin = from_snapshot(to_snapshot(nodes[0]))
                    twin.parent = nodes[0].parent
                    if isinstance(source, AttributeRule):
                        twin.attrs[source.attribute] = "CONFLICT-0000"
                    else:
                        twin.children = ["CONFLICT-0000"]
                    nodes[0].parent.children.append(twin)
                    return _clone(root)
            elif isinstance(source, EmbeddedRule):
                for node in root.iter():
                    if node.tag == "script" and isinstance(node.data, dict) and node.parent:
                        target: Any = node.data
                        for step in source.path[:-1]:
                            target = target.get(step) if isinstance(target, dict) else None
                        if isinstance(target, dict) and source.path[-1] in target:
                            target[source.path[-1]] = "CONFLICT-0000"
                            return _clone(root)
        return None

    def remove_image_region(root: Node) -> Node | None:
        regions = {r.name: r for r in template.image_regions}
        removed = False
        for rule in epr.image_roles:
            region = regions.get(rule.region)
            if rule.role == "REPRESENTATIVE" and region is not None:
                for node in select(root, region.selector):
                    _remove(node)
                    removed = True
        return _clone(root) if removed else None

    def unmatched(_: Extraction, got: Extraction) -> bool:
        return got.verdict is not TemplateVerdict.MATCHED

    def prices_review(_: Extraction, got: Extraction) -> bool:
        return unmatched(_, got) or got.fields["prices"].status is FieldStatus.REVIEW_REQUIRED

    def never_confident_sold_out(base: Extraction, got: Extraction) -> bool:
        if unmatched(base, got):
            return True
        before, after = base.fields["stock"], got.fields["stock"]
        return (
            value_json(after) == value_json(before) or after.status is FieldStatus.REVIEW_REQUIRED
        )

    def identity_unresolved(_: Extraction, got: Extraction) -> bool:
        return unmatched(_, got) or got.identity is None

    def images_review(_: Extraction, got: Extraction) -> bool:
        return unmatched(_, got) or got.images_status is ImageCoverage.REVIEW_REQUIRED

    return [
        ("remove_required_anchor", drop_anchor, unmatched),
        ("duplicate_price_row", duplicate_price_row, prices_review),
        ("inject_hidden_sold_out", hidden_sold_out, never_confident_sold_out),
        ("add_conflicting_identity", conflicting_identity, identity_unresolved),
        ("remove_image_region", remove_image_region, images_review),
    ]


# ---------------------------------------------------------------- the run


def validate(
    bundle: Bundle,
    samples: Sequence[ValidationSample],
    *,
    negatives: Mapping[str, str],
    manifest: HookManifest | None = None,
    all_eprs: Sequence[ExtractionProfileRevision] = (),
    architecture_review_recorded: bool = False,
    resolved_findings: frozenset[str] = frozenset(),
) -> ValidationRun:
    for sample in samples:
        if "profile" in json.dumps(sample.provenance).lower():
            raise SampleRefused("a sample whose provenance names a profile is never proof material")
    checks: list[Check] = []

    # V1 referential: the bundle was loaded with recomputed digests; hooks resolve by revision.
    try:
        resolve_hooks(bundle.epr, manifest)
        checks.append(Check("V1", Verdict.PASS))
    except HookRefused as refused:
        checks.append(Check("V1", Verdict.FAIL, (str(refused),)))
        return ValidationRun(
            Verdict.FAIL, tuple(checks), freshness_tuple(bundle, samples, manifest)
        )

    checks.append(_v2(bundle, samples))

    complete = [s for s in samples if not s.truncated]
    truncated = [s.digest[:12] for s in samples if s.truncated]
    v3: list[str] = []
    v3a: list[str] = []
    v5: list[str] = []
    v6: list[str] = []
    calls: Counter[str] = Counter()
    per_template: Counter[str] = Counter()
    extractions: list[tuple[ValidationSample, Node, Extraction]] = []
    for sample in complete:
        root = from_snapshot(sample.snapshot)
        got = extract(bundle, root, manifest)
        again = extract(bundle, from_snapshot(sample.snapshot), manifest)
        if got.digest() != again.digest():
            v5.append(f"{sample.digest[:12]}: two evaluations differ")
        label = sample.digest[:12]
        v3 += [f"{label}: {p}" for p in _agreement(got, sample.expected)]
        v3a += _v3a(bundle, root, got, resolved_findings, label)
        for fact in got.fields.values():
            for evidence in fact.evidence:
                for text in (evidence.locator, evidence.observed or "", evidence.normalized or ""):
                    if len(text.encode("utf-8")) > EVIDENCE_MAX_BYTES or secret_findings(text):
                        v6.append(f"{label}: unsafe evidence at {evidence.locator[:40]}")
        calls.update(got.hook_calls)
        if got.template_key is not None:
            per_template[got.template_key] += 1
        extractions.append((sample, root, got))
    for template in bundle.templates.values():
        if not per_template[template.template_key]:
            v3.append(f"no complete sample for template {template.template_key}")
    if len(complete) < 2:
        v3.append("at least two complete samples are required")
    checks.append(Check("V3", Verdict.FAIL if v3 else Verdict.PASS, tuple(v3)))
    checks.append(Check("V3a", Verdict.INCOMPLETE if v3a else Verdict.PASS, tuple(v3a)))

    # V4: negative pages and the deterministic mutation suite fail closed.
    v4: list[str] = []
    v4_incomplete: list[str] = []
    exercised: Counter[str] = Counter()
    for name, html in negatives.items():
        negative, _ = match_template(bundle, parse_html(html))
        if negative is TemplateVerdict.MATCHED:
            v4.append(f"negative page {name} matched a template")
    for sample, root, base in extractions:
        _, matched = match_template(bundle, root)
        if matched is None:
            continue
        for name, mutate, holds in _mutations(bundle, matched):
            mutated = mutate(_clone(root))
            if mutated is None:
                continue
            exercised[name] += 1
            if not holds(base, extract(bundle, mutated, manifest)):
                v4.append(f"{sample.digest[:12]}: {name} did not fail closed")
    # Coverage: every required mutation class must run at least once across the samples. A class
    # that could not be constructed is unproven, never silently passed.
    for name in REQUIRED_MUTATIONS:
        if not exercised[name]:
            v4_incomplete.append(f"MUTATION_NOT_EXERCISED:{name}")
    coverage = tuple(f"EXERCISED:{name}x{exercised[name]}" for name in REQUIRED_MUTATIONS)
    v4_outcome = Verdict.FAIL if v4 else Verdict.INCOMPLETE if v4_incomplete else Verdict.PASS
    checks.append(Check("V4", v4_outcome, (*v4, *v4_incomplete, *coverage)))
    checks.append(Check("V5", Verdict.FAIL if v5 else Verdict.PASS, tuple(v5)))
    checks.append(Check("V6", Verdict.FAIL if v6 else Verdict.PASS, tuple(v6)))

    # V7: G6 cap, G7 proven paths; G5 is reported (it blocks ACTIVE, which is out of scope).
    v7: list[str] = []
    outcome = Verdict.PASS
    if g6_exceeded(bundle.epr) and not architecture_review_recorded:
        v7.append("G6: more than two (hook_point, target) bindings; architecture review required")
        outcome = Verdict.INCOMPLETE
    for binding in bundle.epr.hooks:
        key = ":".join(promotion_key(binding))
        if not calls[key]:
            v7.append(f"G7: bound hook {key} is exercised by no sample")
            outcome = Verdict.FAIL
    shared = promotion_review([bundle.epr, *all_eprs])
    v7 += [f"G5: {':'.join(k)} shared by {sorted(v)}" for k, v in sorted(shared.items())]
    checks.append(Check("V7", outcome, tuple(v7)))

    # V8: a truncated sample never supports a PASS, and nothing treats its omitted bytes as seen.
    checks.append(
        Check(
            "V8",
            Verdict.INCOMPLETE if truncated else Verdict.PASS,
            tuple(f"SAMPLE_TRUNCATED: {d}" for d in truncated),
        )
    )

    outcomes = {check.outcome for check in checks}
    verdict = (
        Verdict.FAIL
        if Verdict.FAIL in outcomes
        else Verdict.INCOMPLETE
        if Verdict.INCOMPLETE in outcomes
        else Verdict.PASS
    )
    return ValidationRun(verdict, tuple(checks), freshness_tuple(bundle, samples, manifest))


def run_digest(run: ValidationRun) -> str:
    body = {
        "verdict": run.verdict.value,
        "checks": [[c.name, c.outcome.value, list(c.details)] for c in run.checks],
        "freshness": list(run.freshness),
    }
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
