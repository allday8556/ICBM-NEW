"""The generic engine: one bundle interpreted over one element tree (ADR-0017 §1, §8).

It answers the three questions of the parser seam — identity, fields, image references — and
nothing else. It fetches nothing (image references are candidates; no bytes are ever read), writes
nothing, and decides every status itself: a hook only proposes a candidate.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.collect.facts import (
    FIELD_REGISTRY,
    SUPPLIED_FIELDS,
    Availability,
    Evidence,
    EvidenceKind,
    FactValue,
    FieldFact,
    FieldLevel,
    FieldStatus,
    ImageRole,
    MoneyValue,
    NoticeItem,
    NoticeValue,
    OptionsValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)
from prototypes.adaptive_collector.dom import Node
from prototypes.adaptive_collector.hooks import (
    CANNOT_PARSE,
    Hook,
    HookManifest,
    fingerprint_files,
    resolve_hooks,
)
from prototypes.adaptive_collector.locators import select
from prototypes.adaptive_collector.profile import (
    AttributeRule,
    Bundle,
    FieldRule,
    HookPoint,
    LabelRowRule,
    PageTemplateRevision,
    Rule,
    TextRule,
    canonical,
)

ENGINE_REVISION = "adaptive-engine-proto-1"
_HERE = Path(__file__).resolve().parent
ENGINE_FILES = ("dom.py", "locators.py", "profile.py", "hooks.py", "engine.py")
_MONEY = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+)\s*원?\s*$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OBSERVED_MAX = 200
_TEXT_FIELDS = frozenset({"original_name", "brand", "manufacturer", "origin", "detail_description"})


def engine_fingerprint() -> str:
    """Implementation identity only; it never enters the semantic tuple (ADR-0017 §5.2)."""
    return fingerprint_files([_HERE / name for name in ENGINE_FILES], _HERE)


class TemplateVerdict(StrEnum):
    MATCHED = "MATCHED"
    TEMPLATE_UNMATCHED = "TEMPLATE_UNMATCHED"
    TEMPLATE_AMBIGUOUS = "TEMPLATE_AMBIGUOUS"


class ImageCoverage(StrEnum):
    """Whether the image *references* are complete. No bytes are fetched, so this is never a
    claim about image content."""

    COMPLETE = "COMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class ImageOut:
    role: str
    ordinal: int
    reference: str


@dataclass(frozen=True)
class Hit:
    text: str
    kind: EvidenceKind
    locator: str
    node: Node | None = field(default=None, compare=False, repr=False)
    label: str | None = None


@dataclass(frozen=True)
class Extraction:
    verdict: TemplateVerdict
    template_key: str | None
    identity: str | None
    identity_reason: str | None
    fields: Mapping[str, FieldFact]
    images: tuple[ImageOut, ...]
    images_status: ImageCoverage
    signals: tuple[str, ...]
    read_nodes: frozenset[int]
    hook_calls: Mapping[str, int]

    def normalized(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "template": self.template_key,
            "identity": self.identity,
            "identity_reason": self.identity_reason,
            "fields": {key: _fact_json(fact) for key, fact in sorted(self.fields.items())},
            "images": [[i.role, i.ordinal, i.reference] for i in self.images],
            "images_status": self.images_status.value,
            "signals": list(self.signals),
            "hook_calls": dict(sorted(self.hook_calls.items())),
        }

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.normalized()).encode("utf-8")).hexdigest()


def _fact_json(fact: FieldFact) -> dict[str, Any]:
    return {
        "status": fact.status.value,
        "value": None if fact.value is None else fact.value.model_dump(mode="json"),
        "evidence": [
            [e.kind.value, e.locator, e.status.value, e.observed, e.normalized]
            for e in fact.evidence
        ],
    }


def value_json(fact: FieldFact) -> Any:
    return None if fact.value is None else fact.value.model_dump(mode="json")


# ---------------------------------------------------------------- the run


@dataclass
class _Run:
    bundle: Bundle
    template: PageTemplateRevision
    root: Node
    hooks: Mapping[tuple[HookPoint, str], Hook]
    signals: list[str] = field(default_factory=list)
    read: set[int] = field(default_factory=set)
    hook_calls: Counter[str] = field(default_factory=Counter)

    def mark(self, nodes: Sequence[Node | None]) -> None:
        for node in nodes:
            if node is not None:
                self.read.add(node.index)
                self.read.update(child.index for child in node.descendants())

    def hook(self, point: HookPoint, target: str) -> Hook | None:
        return self.hooks.get((point, target))

    def called(self, point: HookPoint, target: str) -> None:
        self.hook_calls[f"{point.value}:{target}"] += 1


def _visible(nodes: Sequence[Node]) -> list[Node]:
    return [node for node in nodes if not node.hidden]


def _walk(data: Any, path: Sequence[str]) -> Any:
    for step in path:
        if isinstance(data, Mapping) and step in data:
            data = data[step]
        else:
            return None
    return data


def _evaluate_rule(run: _Run, rule: Rule, where: str) -> list[Hit]:
    root = run.root
    if isinstance(rule, TextRule):
        return [
            Hit(node.text(), EvidenceKind.DOM_TEXT, f"{where}:text:{rule.selector}", node)
            for node in _visible(select(root, rule.selector))
            if node.text()
        ]
    if isinstance(rule, AttributeRule):
        return [
            Hit(
                node.attrs[rule.attribute],
                EvidenceKind.ATTRIBUTE,
                f"{where}:attr:{rule.selector}",
                node,
            )
            for node in select(root, rule.selector)
            if node.attrs.get(rule.attribute)
        ]
    if isinstance(rule, LabelRowRule):
        vocabulary = set(run.bundle.epr.vocabularies.get(rule.vocabulary, ()))
        hits: list[Hit] = []
        for container in _visible(select(root, rule.container)):
            for row in container.descendants():
                cells = row.own_cells() if row.tag == "tr" else []
                if len(cells) >= 2 and cells[0].text() in vocabulary and not row.hidden:
                    hits.append(
                        Hit(
                            cells[1].text(),
                            EvidenceKind.DOM_TEXT,
                            f"{where}:row:{rule.container}:{cells[0].text()}",
                            row,
                            label=cells[0].text(),
                        )
                    )
        return hits
    embedded: list[Hit] = []
    for node in root.iter():
        if node.tag != "script" or node.data is None:
            continue
        if rule.block == "JSON_LD":
            if node.attrs.get("type", "").lower() != "application/ld+json":
                continue
            if not (isinstance(node.data, Mapping) and node.data.get("@type") == rule.ld_type):
                continue
            kind = EvidenceKind.JSON_LD
        else:
            if node.assignment != rule.assignment:
                continue
            kind = EvidenceKind.EMBEDDED_JSON
        found = _walk(node.data, rule.path)
        if isinstance(found, str | int | float) and not isinstance(found, bool):
            embedded.append(
                Hit(str(found), kind, f"{where}:{kind.value}:{'.'.join(rule.path)}", node)
            )
    return embedded


def _evidence(hit: Hit, status: FieldStatus, normalized: str | None = None) -> Evidence:
    return Evidence(
        kind=hit.kind,
        locator=hit.locator[:_OBSERVED_MAX],
        status=status,
        observed=hit.text[:_OBSERVED_MAX],
        normalized=normalized,
    )


def _review(locator: str, kind: EvidenceKind = EvidenceKind.DOM_TEXT) -> FieldFact:
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (Evidence(kind=kind, locator=locator[:_OBSERVED_MAX], status=FieldStatus.REVIEW_REQUIRED),),
    )


def _money(text: str) -> int | None:
    match = _MONEY.fullmatch(text)
    return int(match.group(1).replace(",", "")) if match else None


def _validated(key: str, candidate: Any) -> FactValue | None:
    """A hook candidate is only a candidate: the field's own value model decides it."""
    try:
        return FIELD_REGISTRY[key].value_type.model_validate_json(json.dumps(candidate))
    except (ValidationError, TypeError, ValueError):
        return None


def _single_value(run: _Run, key: str, hit: Hit) -> tuple[FactValue | None, FieldStatus]:
    hook = run.hook(HookPoint.VALUE_PARSE, key)
    if hook is not None:
        run.signals.append(f"HOOK:value_parse:{key}")
        run.called(HookPoint.VALUE_PARSE, key)
        candidate = hook(hit.text)
        if candidate is CANNOT_PARSE:
            return None, FieldStatus.REVIEW_REQUIRED
        value = _validated(key, candidate)
        return (
            (value, FieldStatus.CONFIRMED)
            if value is not None
            else (None, FieldStatus.REVIEW_REQUIRED)
        )
    if key in _TEXT_FIELDS:
        return TextValue(text=hit.text), FieldStatus.CONFIRMED
    if key == "minimum_sale_price":
        amount = _money(hit.text)
        if amount is None or hit.label is None:
            return None, FieldStatus.REVIEW_REQUIRED
        return MoneyValue(label=hit.label, amount_krw=amount), FieldStatus.CONFIRMED
    if key == "shipping":
        text = hit.text
        if "무료" in text and not any(ch.isdigit() for ch in text):
            return ShippingValue(kind=ShippingKind.FREE, policy_text=text), FieldStatus.CONFIRMED
        fee = _money(text)
        if fee is not None:
            return (
                ShippingValue(kind=ShippingKind.FIXED, policy_text=text, fee_krw=fee),
                FieldStatus.CONFIRMED,
            )
        # A conditional or unreadable policy is never flattened into a guessed fee.
        return ShippingValue(
            kind=ShippingKind.UNKNOWN, policy_text=text
        ), FieldStatus.REVIEW_REQUIRED
    return None, FieldStatus.REVIEW_REQUIRED


def _absent(run: _Run, key: str, rule: FieldRule, where: str) -> FieldFact:
    """No rule hit. ABSENT only when the rule's own absence condition is observed (§8.2)."""
    level = FIELD_REGISTRY[key].level
    anchor = rule.absent_when_present
    if level is FieldLevel.COVERAGE and anchor is not None and _visible(select(run.root, anchor)):
        return FieldFact(
            FieldStatus.ABSENT,
            None,
            (
                Evidence(
                    kind=EvidenceKind.DOM_TEXT,
                    locator=f"{where}:absent:{anchor}",
                    status=FieldStatus.ABSENT,
                ),
            ),
        )
    run.signals.append(f"MISSING_ANCHOR:{key}")
    return _review(f"{where}:missing")


def _field(run: _Run, key: str) -> FieldFact:
    where = f"{run.template.template_key}/{key}"
    rule = run.template.fields.get(key)
    if rule is None:
        # A missing rule is a lint finding for validation (V2), never an ABSENT fact.
        run.signals.append(f"NO_RULE:{key}")
        return _review(f"{where}:no-rule")
    primary = _evaluate_rule(run, rule.primary, where)
    alternatives = [
        _evaluate_rule(run, alt, f"{where}/alt{i}") for i, alt in enumerate(rule.alternatives)
    ]
    run.mark([hit.node for hit in primary])
    for hits in alternatives:
        run.mark([hit.node for hit in hits])
    if key in {"prices", "notice"}:
        return _rows(run, key, primary, where) if primary else _absent(run, key, rule, where)
    if key == "quantity_tiers":
        if primary:
            # The inherited M3 boundary: positive tier values are not accepted (M3.md §2.3).
            run.signals.append("M3_BOUNDARY:quantity_tiers")
            return FieldFact(
                FieldStatus.REVIEW_REQUIRED,
                None,
                tuple(_evidence(hit, FieldStatus.REVIEW_REQUIRED) for hit in primary),
            )
        return _absent(run, key, rule, where)
    chosen = primary
    if not chosen:
        chosen = next((hits for hits in alternatives if hits), [])
        if chosen:
            run.signals.append(f"ALTERNATIVE_USED:{key}")
    if not chosen:
        return _absent(run, key, rule, where)
    if rule.cardinality == "ONE" and len({hit.text for hit in chosen}) > 1:
        run.signals.append(f"CARDINALITY:{key}")
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(_evidence(hit, FieldStatus.REVIEW_REQUIRED) for hit in chosen),
        )
    hit = chosen[0]
    others = [hits[0] for hits in alternatives if hits and hits is not chosen]
    if primary and any(" ".join(o.text.split()) != " ".join(hit.text.split()) for o in others):
        # Two locators that disagree: the engine never picks one.
        run.signals.append(f"CONFLICT:{key}")
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(_evidence(h, FieldStatus.REVIEW_REQUIRED) for h in (hit, *others)),
        )
    value, status = _single_value(run, key, hit)
    if status is not FieldStatus.CONFIRMED:
        run.signals.append(f"PARSE:{key}")
        return FieldFact(status, value, (_evidence(hit, FieldStatus.REVIEW_REQUIRED),))
    evidence = [_evidence(hit, FieldStatus.CONFIRMED)]
    evidence += [_evidence(o, FieldStatus.CONFIRMED) for o in others]
    return FieldFact(FieldStatus.CONFIRMED, value, tuple(evidence))


def _rows(run: _Run, key: str, hits: list[Hit], where: str) -> FieldFact:
    labels = [hit.label for hit in hits]
    if len(set(labels)) != len(labels):
        # The same label twice is two statements of one fact: ambiguous, never merged.
        run.signals.append(f"CONFLICT:{key}")
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(_evidence(hit, FieldStatus.REVIEW_REQUIRED) for hit in hits),
        )
    value: FactValue
    if key == "prices":
        prices = []
        for hit in hits:
            amount = _money(hit.text)
            if amount is None or hit.label is None:
                run.signals.append(f"PARSE:{key}")
                return FieldFact(
                    FieldStatus.REVIEW_REQUIRED,
                    None,
                    (_evidence(hit, FieldStatus.REVIEW_REQUIRED),),
                )
            prices.append(SourcePrice(label=hit.label, amount_krw=amount))
        value = PricesValue(prices=tuple(prices))
    else:
        value = NoticeValue(
            items=tuple(NoticeItem(label=hit.label or "", text=hit.text) for hit in hits)
        )
    return FieldFact(
        FieldStatus.CONFIRMED, value, tuple(_evidence(hit, FieldStatus.CONFIRMED) for hit in hits)
    )


def _stock(run: _Run) -> FieldFact:
    where = f"{run.template.template_key}/stock"
    scopes = _visible(select(run.root, run.template.stock.scope))
    if not scopes:
        run.signals.append("MISSING_ANCHOR:stock")
        return _review(f"{where}:missing", EvidenceKind.CONTROL_STATE)
    vocabulary = run.bundle.epr.stock
    purchase: list[Node] = []
    sold: list[Node] = []
    for scope in scopes:
        for selector in vocabulary.purchase_controls:
            purchase += _visible(select(scope, selector))
        for node in scope.iter():
            own = " ".join(c for c in node.children if isinstance(c, str))
            if not node.hidden and any(word in own for word in vocabulary.sold_out_words):
                sold.append(node)
    run.mark(purchase + sold)
    active = [node for node in purchase if "disabled" not in node.attrs]
    evidence = [
        Evidence(
            kind=EvidenceKind.CONTROL_STATE,
            locator=f"{where}:control:{node.tag}.{'.'.join(sorted(node.classes))}"[:_OBSERVED_MAX],
            status=FieldStatus.CONFIRMED,
            observed="disabled" if "disabled" in node.attrs else "active",
        )
        for node in purchase
    ]
    evidence += [
        Evidence(
            kind=EvidenceKind.DOM_TEXT,
            locator=f"{where}:sold-out:{node.tag}"[:_OBSERVED_MAX],
            status=FieldStatus.CONFIRMED,
            observed=node.text()[:_OBSERVED_MAX],
        )
        for node in sold
    ]
    if active:
        availability = Availability.ON_SALE
    elif sold:
        availability = Availability.SOLD_OUT
    else:
        run.signals.append("STOCK_UNDECIDED")
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            StockValue(availability=Availability.REVIEW_REQUIRED),
            (
                *evidence,
                Evidence(
                    kind=EvidenceKind.CONTROL_STATE,
                    locator=f"{where}:undecided",
                    status=FieldStatus.REVIEW_REQUIRED,
                ),
            ),
        )
    return FieldFact(FieldStatus.CONFIRMED, StockValue(availability=availability), tuple(evidence))


def _options(run: _Run) -> FieldFact:
    where = f"{run.template.template_key}/options"
    containers = _visible(select(run.root, run.template.options.container))
    if not containers:
        run.signals.append("MISSING_ANCHOR:options")
        return _review(f"{where}:missing", EvidenceKind.CONTROL_STATE)
    controls = [
        node
        for container in containers
        for node in container.descendants()
        if node.tag == "select" and not node.hidden
    ]
    run.mark(containers)
    if not controls:
        # The page proves it has no option control: zero axes, CONFIRMED (ADR-0010 §7).
        return FieldFact(
            FieldStatus.CONFIRMED,
            OptionsValue(axes=()),
            (
                Evidence(
                    kind=EvidenceKind.CONTROL_STATE,
                    locator=f"{where}:no-control",
                    status=FieldStatus.CONFIRMED,
                ),
            ),
        )
    # The inherited M3 boundary: positive option axes are not accepted (M3.md §2.2).
    run.signals.append("M3_BOUNDARY:options")
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        tuple(
            Evidence(
                kind=EvidenceKind.CONTROL_STATE,
                locator=f"{where}:select:{node.attrs.get('name', '')}"[:_OBSERVED_MAX],
                status=FieldStatus.REVIEW_REQUIRED,
                observed=" | ".join(o.text() for o in node.descendants() if o.tag == "option")[
                    :_OBSERVED_MAX
                ],
            )
            for node in controls
        ),
    )


def _images(run: _Run) -> tuple[tuple[ImageOut, ...], ImageCoverage]:
    regions = {region.name: region for region in run.template.image_regions}
    images: list[ImageOut] = []
    counters: Counter[str] = Counter()
    complete = True
    for rule in run.bundle.epr.image_roles:
        region = regions.get(rule.region)
        if region is None:
            complete = False
            continue
        nodes = [
            node
            for node in _visible(select(run.root, region.selector))
            if node.tag == "img" and node.attrs.get("src")
        ]
        if not nodes:
            if rule.role == ImageRole.REPRESENTATIVE.value:
                # No representative reference: never a complete image set.
                run.signals.append(f"MISSING_ANCHOR:images:{rule.region}")
                complete = False
            continue
        run.mark(nodes)
        for node in nodes[:1] if rule.take == "FIRST" else nodes:
            images.append(ImageOut(rule.role, counters[rule.role], node.attrs["src"].strip()))
            counters[rule.role] += 1
    if not counters[ImageRole.REPRESENTATIVE.value]:
        complete = False
    return tuple(images), ImageCoverage.COMPLETE if complete else ImageCoverage.REVIEW_REQUIRED


def _identity(run: _Run) -> tuple[str | None, str | None]:
    values: list[set[str]] = []
    hook = run.hook(HookPoint.IDENTITY_DECODE, "identity")
    for index, rule in enumerate(run.bundle.epr.identity.sources):
        hits = _evaluate_rule(run, rule, f"identity/{index}")
        run.mark([hit.node for hit in hits])
        decoded: set[str] = set()
        for hit in hits:
            text = hit.text.strip()
            if hook is not None:
                run.called(HookPoint.IDENTITY_DECODE, "identity")
                candidate = hook(text)
                if candidate is CANNOT_PARSE or not isinstance(candidate, str):
                    return None, "IDENTITY_UNPARSEABLE"
                text = candidate
            if not _IDENTITY.fullmatch(text):
                return None, "IDENTITY_UNPARSEABLE"
            decoded.add(text)
        values.append(decoded)
    if any(not found for found in values):
        return None, "IDENTITY_MISSING"
    if any(len(found) > 1 for found in values) or len(set().union(*values)) != 1:
        return None, "IDENTITY_CONTRADICTORY"
    return next(iter(values[0])), None


def match_template(
    bundle: Bundle, root: Node
) -> tuple[TemplateVerdict, PageTemplateRevision | None]:
    """Exactly one template, by predicate: every required anchor, no forbidden one. No score."""
    matched = [
        template
        for template in bundle.templates.values()
        if all(select(root, s) for s in template.signature.required)
        and not any(select(root, s) for s in template.signature.forbidden)
    ]
    if len(matched) == 1:
        return TemplateVerdict.MATCHED, matched[0]
    if not matched:
        return TemplateVerdict.TEMPLATE_UNMATCHED, None
    return TemplateVerdict.TEMPLATE_AMBIGUOUS, None


def extract(bundle: Bundle, root: Node, manifest: HookManifest | None = None) -> Extraction:
    hooks = resolve_hooks(bundle.epr, manifest)
    verdict, template = match_template(bundle, root)
    if template is None:
        return Extraction(
            verdict,
            None,
            None,
            None,
            {},
            (),
            ImageCoverage.REVIEW_REQUIRED,
            (verdict.value,),
            frozenset(),
            {},
        )
    run = _Run(bundle, template, root, hooks)
    identity, reason = _identity(run)
    fields: dict[str, FieldFact] = {}
    for key in sorted(SUPPLIED_FIELDS):
        if key == "stock":
            fields[key] = _stock(run)
        elif key == "options":
            fields[key] = _options(run)
        else:
            fields[key] = _field(run, key)
    images, coverage = _images(run)
    return Extraction(
        verdict,
        template.template_key,
        identity,
        reason,
        fields,
        images,
        coverage,
        tuple(run.signals),
        frozenset(run.read),
        dict(run.hook_calls),
    )
