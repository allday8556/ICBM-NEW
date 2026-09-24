"""The generic Adaptive engine: one bundle over one element tree (ADR-0017 §1, §8).

It answers the parser seam's three questions and nothing else:

* **which product** — the EPR identity rule, every source agreeing, or no identity;
* **what its fields say** — one ``FieldFact`` per supplied field of ``FIELD_REGISTRY``;
* **which images are product evidence** — ordered ``(role, ordinal, reference)`` candidates.

It fetches nothing (image references are candidates, never bytes), stores nothing, and decides
every status itself. Exactly one template must match; a missing anchor is never ``ABSENT``; two
locators that disagree are ``REVIEW_REQUIRED``; the M3 boundary on positive options and tiers is
inherited (``docs/acceptance/M3.md`` §2).
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from app.collect.adaptive.canonical import canonical_json, digest
from app.collect.adaptive.document import Element, from_structure
from app.collect.adaptive.hooks import CANNOT_PARSE, Hook, HookManifest, bind_hooks
from app.collect.adaptive.locator import find
from app.collect.adaptive.profiles import (
    AttributeLocation,
    Bundle,
    EmbeddedLocation,
    FieldRule,
    HookPoint,
    LabelledRowLocation,
    Location,
    PageTemplateRevision,
    TextLocation,
)
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

EXTRACTION_DIGEST_SCHEME = "icbm-adaptive-extraction/v1"
_WON = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+)\s*원?\s*$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OBSERVED_CHARS = 200
_TEXT_VALUE_FIELDS = frozenset(
    {"original_name", "brand", "manufacturer", "origin", "detail_description"}
)
_ROW_FIELDS = frozenset({"prices", "notice"})


class TemplateOutcome(StrEnum):
    MATCHED = "MATCHED"
    TEMPLATE_UNMATCHED = "TEMPLATE_UNMATCHED"
    TEMPLATE_AMBIGUOUS = "TEMPLATE_AMBIGUOUS"


class ImageCompleteness(StrEnum):
    """Whether the image *references* are complete. No bytes are read, so this never claims
    anything about image content."""

    COMPLETE = "COMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class ImageCandidateRef:
    role: str  # REPRESENTATIVE | DETAIL
    ordinal: int
    reference: str


@dataclass(frozen=True)
class Extraction:
    outcome: TemplateOutcome
    template_key: str | None
    source_product_id: str | None
    identity_reason: str | None
    fields: Mapping[str, FieldFact]
    images: tuple[ImageCandidateRef, ...]
    image_completeness: ImageCompleteness
    signals: tuple[str, ...]
    read_positions: frozenset[int]
    hook_calls: Mapping[str, int]

    def normalized(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "template": self.template_key,
            "source_product_id": self.source_product_id,
            "identity_reason": self.identity_reason,
            "fields": {key: fact_json(fact) for key, fact in sorted(self.fields.items())},
            "images": [[i.role, i.ordinal, i.reference] for i in self.images],
            "image_completeness": self.image_completeness.value,
            "signals": list(self.signals),
            "hook_calls": dict(sorted(self.hook_calls.items())),
        }

    def digest(self) -> str:
        return digest(EXTRACTION_DIGEST_SCHEME, self.normalized())


def fact_value(fact: FieldFact) -> Any:
    return None if fact.value is None else fact.value.model_dump(mode="json")


def fact_json(fact: FieldFact) -> dict[str, Any]:
    return {
        "status": fact.status.value,
        "value": fact_value(fact),
        "evidence": [
            [e.kind.value, e.locator, e.status.value, e.observed, e.normalized]
            for e in fact.evidence
        ],
    }


# ---------------------------------------------------------------- template matching


def match_template(
    bundle: Bundle, root: Element
) -> tuple[TemplateOutcome, PageTemplateRevision | None]:
    """A predicate per template — every required anchor, no forbidden one — and exactly one
    template must satisfy it. There is no score and no closest match."""
    satisfied = [
        template
        for _, template in bundle.templates
        if all(find(root, anchor) for anchor in template.signature.required)
        and not any(find(root, anchor) for anchor in template.signature.forbidden)
    ]
    if len(satisfied) == 1:
        return TemplateOutcome.MATCHED, satisfied[0]
    if satisfied:
        return TemplateOutcome.TEMPLATE_AMBIGUOUS, None
    return TemplateOutcome.TEMPLATE_UNMATCHED, None


# ---------------------------------------------------------------- one extraction pass


@dataclass(frozen=True)
class _Hit:
    text: str
    kind: EvidenceKind
    locator: str
    element: Element | None = field(default=None, compare=False, repr=False)
    label: str | None = None

    def evidence(self, status: FieldStatus) -> Evidence:
        return Evidence(
            kind=self.kind,
            locator=self.locator[:_OBSERVED_CHARS],
            status=status,
            observed=self.text[:_OBSERVED_CHARS],
        )


def _marker(kind: EvidenceKind, locator: str, status: FieldStatus) -> Evidence:
    return Evidence(kind=kind, locator=locator[:_OBSERVED_CHARS], status=status)


def _won(text: str) -> int | None:
    match = _WON.fullmatch(text)
    return None if match is None else int(match.group(1).replace(",", ""))


def _presented(elements: Sequence[Element]) -> list[Element]:
    return [element for element in elements if element.presented]


class _Pass:
    def __init__(
        self,
        bundle: Bundle,
        template: PageTemplateRevision,
        root: Element,
        hooks: Mapping[tuple[HookPoint, str], Hook],
    ) -> None:
        self.bundle = bundle
        self.epr = bundle.epr
        self.template = template
        self.root = root
        self.hooks = hooks
        self.signals: list[str] = []
        self.read: set[int] = set()
        self.hook_calls: Counter[str] = Counter()

    # -- reading ------------------------------------------------------------------------

    def _mark(self, hits: Sequence[_Hit]) -> None:
        for hit in hits:
            if hit.element is not None:
                self.read.update(element.position for element in hit.element.walk())

    def _mark_elements(self, elements: Sequence[Element]) -> None:
        for element in elements:
            self.read.update(inner.position for inner in element.walk())

    def locate(self, location: Location, where: str) -> list[_Hit]:
        if isinstance(location, TextLocation):
            hits = [
                _Hit(
                    element.text(),
                    EvidenceKind.DOM_TEXT,
                    f"{where}:text:{location.locator}",
                    element,
                )
                for element in _presented(find(self.root, location.locator))
                if element.text()
            ]
        elif isinstance(location, AttributeLocation):
            hits = [
                _Hit(
                    element.attributes[location.attribute],
                    EvidenceKind.ATTRIBUTE,
                    f"{where}:attribute:{location.locator}",
                    element,
                )
                for element in find(self.root, location.locator)
                if element.attributes.get(location.attribute)
            ]
        elif isinstance(location, LabelledRowLocation):
            labels = set(self.epr.vocabularies[location.vocabulary])
            hits = []
            for container in _presented(find(self.root, location.container)):
                for row in container.descendants():
                    cells = row.cells() if row.tag == "tr" else []
                    if len(cells) >= 2 and row.presented and cells[0].text() in labels:
                        label = cells[0].text()
                        hits.append(
                            _Hit(
                                cells[1].text(),
                                EvidenceKind.DOM_TEXT,
                                f"{where}:row:{location.container}:{label}",
                                row,
                                label,
                            )
                        )
        else:
            hits = self._embedded(location, where)
        self._mark(hits)
        return hits

    def _embedded(self, location: EmbeddedLocation, where: str) -> list[_Hit]:
        hits: list[_Hit] = []
        for element in self.root.walk():
            if element.tag != "script" or element.data is None:
                continue
            if location.source == "JSON_LD":
                declared = element.attributes.get("type", "").strip().lower()
                if declared != "application/ld+json" or not isinstance(element.data, dict):
                    continue
                if element.data.get("@type") != location.json_ld_type:
                    continue
                kind = EvidenceKind.JSON_LD
            else:
                if element.assignment != location.assignment:
                    continue
                kind = EvidenceKind.EMBEDDED_JSON
            value: Any = element.data
            for step in location.path:
                value = value.get(step) if isinstance(value, dict) else None
            if isinstance(value, str | int) and not isinstance(value, bool):
                hits.append(
                    _Hit(
                        str(value), kind, f"{where}:{kind.value}:{'.'.join(location.path)}", element
                    )
                )
        return hits

    # -- deciding a value ---------------------------------------------------------------

    def _hooked(self, key: str, hit: _Hit) -> tuple[FactValue | None, FieldStatus] | None:
        hook = self.hooks.get((HookPoint.VALUE_PARSE, key))
        if hook is None:
            return None
        self.hook_calls[f"{HookPoint.VALUE_PARSE.value}:{key}"] += 1
        self.signals.append(f"HOOK:{HookPoint.VALUE_PARSE.value}:{key}")
        candidate = hook(hit.text)
        if candidate is CANNOT_PARSE:
            return None, FieldStatus.REVIEW_REQUIRED
        try:
            # A candidate is only a candidate: the field's value model decides it, strictly.
            value = FIELD_REGISTRY[key].value_type.model_validate_json(canonical_json(candidate))
        except (ValidationError, ValueError, TypeError):
            return None, FieldStatus.REVIEW_REQUIRED
        return value, FieldStatus.CONFIRMED

    def _value(self, key: str, hit: _Hit) -> tuple[FactValue | None, FieldStatus]:
        hooked = self._hooked(key, hit)
        if hooked is not None:
            return hooked
        if key in _TEXT_VALUE_FIELDS:
            return TextValue(text=hit.text), FieldStatus.CONFIRMED
        if key == "minimum_sale_price":
            amount = _won(hit.text)
            if amount is None or hit.label is None:
                return None, FieldStatus.REVIEW_REQUIRED
            return MoneyValue(label=hit.label, amount_krw=amount), FieldStatus.CONFIRMED
        if key == "shipping":
            text = hit.text
            if "무료" in text and not any(ch.isdigit() for ch in text):
                return ShippingValue(
                    kind=ShippingKind.FREE, policy_text=text
                ), FieldStatus.CONFIRMED
            fee = _won(text)
            if fee is not None:
                return (
                    ShippingValue(kind=ShippingKind.FIXED, policy_text=text, fee_krw=fee),
                    FieldStatus.CONFIRMED,
                )
            # A conditional or unreadable policy is never flattened into a guessed fee.
            return (
                ShippingValue(kind=ShippingKind.UNKNOWN, policy_text=text),
                FieldStatus.REVIEW_REQUIRED,
            )
        return None, FieldStatus.REVIEW_REQUIRED

    # -- fields -------------------------------------------------------------------------

    def _review(self, locator: str, kind: EvidenceKind = EvidenceKind.DOM_TEXT) -> FieldFact:
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            (_marker(kind, locator, FieldStatus.REVIEW_REQUIRED),),
        )

    def _all_review(self, hits: Sequence[_Hit]) -> FieldFact:
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(hit.evidence(FieldStatus.REVIEW_REQUIRED) for hit in hits),
        )

    def _unstated(self, key: str, rule: FieldRule, where: str) -> FieldFact:
        """No location hit: ABSENT only when the rule's own absence condition is observed."""
        anchor = rule.absent_when
        coverage = FIELD_REGISTRY[key].level is FieldLevel.COVERAGE
        if coverage and anchor is not None and _presented(find(self.root, anchor)):
            return FieldFact(
                FieldStatus.ABSENT,
                None,
                (_marker(EvidenceKind.DOM_TEXT, f"{where}:absent:{anchor}", FieldStatus.ABSENT),),
            )
        self.signals.append(f"MISSING_ANCHOR:{key}")
        return self._review(f"{where}:missing")

    def field(self, key: str) -> FieldFact:
        """One deterministic decision over every declared location (ADR-0017 §8.2).

        The primary and every alternative are evaluated, and every hit of every location takes
        part: cardinality is counted in hits, any two hits that disagree make the field
        ``REVIEW_REQUIRED``, and no location is ever chosen over another. Using alternatives
        alone is signalled.
        """
        where = f"{self.template.template_key}/{key}"
        rule = self.template.fields.get(key)
        if rule is None:
            # A missing rule is a V2 lint finding, never an ABSENT fact.
            self.signals.append(f"NO_RULE:{key}")
            return self._review(f"{where}:no-rule")
        results = [
            self.locate(rule.primary, where),
            *(
                self.locate(location, f"{where}/alternative-{index}")
                for index, location in enumerate(rule.alternatives)
            ),
        ]
        hitting = [hits for hits in results if hits]
        if not hitting:
            return self._unstated(key, rule, where)
        every = [hit for hits in hitting for hit in hits]
        if not results[0]:
            self.signals.append(f"ALTERNATIVE_USED:{key}")
        if key == "quantity_tiers":
            # The inherited M3 boundary: positive tier values are not accepted (M3.md §2.3).
            self.signals.append("M3_BOUNDARY:quantity_tiers")
            return self._all_review(every)
        if rule.cardinality == "ONE" and any(len(hits) > 1 for hits in hitting):
            self.signals.append(f"CARDINALITY:{key}")
            return self._all_review(every)
        if key in _ROW_FIELDS:
            return self._rows(key, hitting, every)
        if len({" ".join(hit.text.split()) for hit in every}) > 1:
            # Locations, or hits of one location, that disagree: the engine never picks one.
            self.signals.append(f"CONFLICT:{key}")
            return self._all_review(every)
        value, status = self._value(key, every[0])
        if status is not FieldStatus.CONFIRMED:
            self.signals.append(f"UNREADABLE:{key}")
            return FieldFact(
                status, value, tuple(hit.evidence(FieldStatus.REVIEW_REQUIRED) for hit in every)
            )
        return FieldFact(
            FieldStatus.CONFIRMED,
            value,
            tuple(hit.evidence(FieldStatus.CONFIRMED) for hit in every),
        )

    def _row_value(self, key: str, hits: Sequence[_Hit]) -> FactValue | None:
        """One location's rows as a value, or ``None`` when they are ambiguous or unreadable."""
        labels = [hit.label for hit in hits]
        if None in labels or len(labels) != len(set(labels)):
            # A row without a label, or one label stated twice, is ambiguous, never merged.
            self.signals.append(f"CONFLICT:{key}")
            return None
        if key == "prices":
            prices: list[SourcePrice] = []
            for hit in hits:
                amount = _won(hit.text)
                if amount is None or hit.label is None:
                    self.signals.append(f"UNREADABLE:{key}")
                    return None
                prices.append(SourcePrice(label=hit.label, amount_krw=amount))
            return PricesValue(prices=tuple(prices))
        return NoticeValue(
            items=tuple(NoticeItem(label=hit.label or "", text=hit.text) for hit in hits)
        )

    def _rows(
        self, key: str, hitting: Sequence[Sequence[_Hit]], every: Sequence[_Hit]
    ) -> FieldFact:
        values = [self._row_value(key, hits) for hits in hitting]
        if any(value is None for value in values):
            return self._all_review(every)
        stated = {canonical_json(value.model_dump(mode="json")) for value in values if value}
        if len(stated) > 1:
            self.signals.append(f"CONFLICT:{key}")
            return self._all_review(every)
        return FieldFact(
            FieldStatus.CONFIRMED,
            values[0],
            tuple(hit.evidence(FieldStatus.CONFIRMED) for hit in every),
        )

    def stock(self) -> FieldFact:
        where = f"{self.template.template_key}/stock"
        scopes = _presented(find(self.root, self.template.stock_scope))
        if not scopes:
            self.signals.append("MISSING_ANCHOR:stock")
            return self._review(f"{where}:missing", EvidenceKind.CONTROL_STATE)
        controls: list[Element] = []
        sold_out: list[Element] = []
        for scope in scopes:
            for locator in self.epr.purchase_controls:
                controls += _presented(find(scope, locator))
            sold_out += [
                element
                for element in scope.walk()
                if element.presented
                and any(word in element.own_text() for word in self.epr.sold_out_words)
            ]
        self._mark_elements(controls + sold_out)
        evidence = [
            _marker(
                EvidenceKind.CONTROL_STATE,
                f"{where}:control:{element.tag}.{'.'.join(sorted(element.classes))}",
                FieldStatus.CONFIRMED,
            )
            for element in controls
        ] + [
            _marker(EvidenceKind.DOM_TEXT, f"{where}:sold-out:{element.tag}", FieldStatus.CONFIRMED)
            for element in sold_out
        ]
        if any(element.operable for element in controls):
            availability = Availability.ON_SALE
        elif sold_out:
            availability = Availability.SOLD_OUT
        else:
            self.signals.append("STOCK_UNDECIDED")
            return FieldFact(
                FieldStatus.REVIEW_REQUIRED,
                StockValue(availability=Availability.REVIEW_REQUIRED),
                (
                    *evidence,
                    _marker(
                        EvidenceKind.CONTROL_STATE,
                        f"{where}:undecided",
                        FieldStatus.REVIEW_REQUIRED,
                    ),
                ),
            )
        return FieldFact(
            FieldStatus.CONFIRMED, StockValue(availability=availability), tuple(evidence)
        )

    def options(self) -> FieldFact:
        where = f"{self.template.template_key}/options"
        containers = _presented(find(self.root, self.template.options_container))
        if not containers:
            self.signals.append("MISSING_ANCHOR:options")
            return self._review(f"{where}:missing", EvidenceKind.CONTROL_STATE)
        self._mark_elements(containers)
        selects = [
            element
            for container in containers
            for element in container.descendants()
            if element.tag == "select" and element.presented
        ]
        if not selects:
            # The page proves it has no option control: zero axes, CONFIRMED (ADR-0010 §7).
            return FieldFact(
                FieldStatus.CONFIRMED,
                OptionsValue(axes=()),
                (
                    _marker(
                        EvidenceKind.CONTROL_STATE, f"{where}:no-control", FieldStatus.CONFIRMED
                    ),
                ),
            )
        self.signals.append("M3_BOUNDARY:options")
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(
                Evidence(
                    kind=EvidenceKind.CONTROL_STATE,
                    locator=f"{where}:select:{element.attributes.get('name', '')}"[
                        :_OBSERVED_CHARS
                    ],
                    status=FieldStatus.REVIEW_REQUIRED,
                    observed=" | ".join(
                        o.text() for o in element.descendants() if o.tag == "option"
                    )[:_OBSERVED_CHARS],
                )
                for element in selects
            ),
        )

    def images(self) -> tuple[tuple[ImageCandidateRef, ...], ImageCompleteness]:
        regions = {region.name: region for region in self.template.image_regions}
        found: list[ImageCandidateRef] = []
        ordinals: Counter[str] = Counter()
        complete = True
        for rule in self.epr.image_roles:
            region = regions.get(rule.region)
            if region is None:
                complete = False
                continue
            images = [
                element
                for element in _presented(find(self.root, region.locator))
                if element.tag == "img" and element.attributes.get("src", "").strip()
            ]
            if not images and rule.role == "REPRESENTATIVE":
                self.signals.append(f"MISSING_ANCHOR:images:{rule.region}")
                complete = False
            self._mark_elements(images)
            for element in images[:1] if rule.take == "FIRST" else images:
                found.append(
                    ImageCandidateRef(
                        rule.role, ordinals[rule.role], element.attributes["src"].strip()
                    )
                )
                ordinals[rule.role] += 1
        if not ordinals["REPRESENTATIVE"]:
            complete = False
        return tuple(
            found
        ), ImageCompleteness.COMPLETE if complete else ImageCompleteness.REVIEW_REQUIRED

    def identity(self) -> tuple[str | None, str | None]:
        hook = self.hooks.get((HookPoint.IDENTITY_DECODE, "identity"))
        stated: list[set[str]] = []
        for index, location in enumerate(self.epr.identity.sources):
            values: set[str] = set()
            for hit in self.locate(location, f"identity/{index}"):
                text = hit.text.strip()
                if hook is not None:
                    self.hook_calls[f"{HookPoint.IDENTITY_DECODE.value}:identity"] += 1
                    decoded = hook(text)
                    if not isinstance(decoded, str):
                        return None, "IDENTITY_UNREADABLE"
                    text = decoded
                if not _SOURCE_ID.fullmatch(text):
                    return None, "IDENTITY_UNREADABLE"
                values.add(text)
            stated.append(values)
        if any(not values for values in stated):
            return None, "IDENTITY_MISSING"
        agreed = set().union(*stated)
        if any(len(values) != 1 for values in stated) or len(agreed) != 1:
            return None, "IDENTITY_CONTRADICTORY"
        return agreed.pop(), None


def extract(bundle: Bundle, root: Element, manifest: HookManifest | None = None) -> Extraction:
    hooks = bind_hooks(bundle.epr, manifest)
    outcome, template = match_template(bundle, root)
    if template is None:
        return Extraction(
            outcome,
            None,
            None,
            None,
            {},
            (),
            ImageCompleteness.REVIEW_REQUIRED,
            (outcome.value,),
            frozenset(),
            {},
        )
    run = _Pass(bundle, template, root, hooks)
    source_product_id, reason = run.identity()
    fields: dict[str, FieldFact] = {}
    for key in sorted(SUPPLIED_FIELDS):
        if key == "stock":
            fields[key] = run.stock()
        elif key == "options":
            fields[key] = run.options()
        else:
            fields[key] = run.field(key)
    images, completeness = run.images()
    return Extraction(
        outcome,
        template.template_key,
        source_product_id,
        reason,
        fields,
        images,
        completeness,
        tuple(run.signals),
        frozenset(run.read),
        dict(run.hook_calls),
    )


def replay(
    bundle: Bundle, structure: Mapping[str, Any], manifest: HookManifest | None = None
) -> Extraction:
    """Extract from a ``ValidationSample`` structure (a fresh tree every call)."""
    return extract(bundle, from_structure(structure), manifest)


__all__ = [
    "Extraction",
    "ImageCandidateRef",
    "ImageCompleteness",
    "TemplateOutcome",
    "extract",
    "fact_json",
    "fact_value",
    "match_template",
    "replay",
]
