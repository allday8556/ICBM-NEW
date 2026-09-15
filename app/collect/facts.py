"""COLLECT source-truth model (ADR-0010 §6–§9, Issue #52 §4–§8).

Pure and deterministic: no clock, I/O or database. It owns the M3 field registry (core vs source
coverage, ADR-0010 §7), the value shape of every field, the evidence digests, the field and source
fingerprints (§6) and the revision-level ``facts_status`` (ruling on Q3). The revision store
persists exactly what :func:`evaluate` returns, so no write path can store a fingerprint or a
status that this module did not compute.

Values are strict: money is an ``int`` of whole KRW and a float is never coerced into one, a
minimum sale price exists only as an observed value, and quantity tiers keep their original
``(quantity, total_price)`` pairs.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from app.core.errors import InputValidationError
from integrations.suppliers.base import SUPPLIER_KEY

CURRENCY = "KRW"
FINGERPRINT_SCHEME = "icbm-facts/v1"
# ADR-0010 §8 (ruling on Q7): a stored observed value is bounded; larger evidence is kept by digest.
EVIDENCE_OBSERVED_MAX_BYTES = 4096
IDENTIFIER_MAX_LENGTH = 64
SOURCE_PRODUCT_ID_MAX_LENGTH = 200

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class FieldStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    ABSENT = "ABSENT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class FactsStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class FieldLevel(StrEnum):
    CORE = "CORE"
    COVERAGE = "COVERAGE"


class EvidenceKind(StrEnum):
    DOM_TEXT = "DOM_TEXT"
    ATTRIBUTE = "ATTRIBUTE"
    EMBEDDED_JSON = "EMBEDDED_JSON"
    JSON_LD = "JSON_LD"
    CONTROL_STATE = "CONTROL_STATE"
    URL = "URL"
    PRODUCT_HTML_FRAGMENT = "PRODUCT_HTML_FRAGMENT"
    IMAGE = "IMAGE"


class ImageRole(StrEnum):
    REPRESENTATIVE = "REPRESENTATIVE"
    DETAIL = "DETAIL"


class ImageIssue(StrEnum):
    """Why an image reference is REVIEW_REQUIRED (ADR-0010 §9, failures)."""

    BAD_HOST = "BAD_HOST"
    BAD_CONTENT_TYPE = "BAD_CONTENT_TYPE"
    OVERSIZE = "OVERSIZE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FETCH_FAILED = "FETCH_FAILED"


class Availability(StrEnum):
    ON_SALE = "ON_SALE"
    SOLD_OUT = "SOLD_OUT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ShippingKind(StrEnum):
    FREE = "FREE"
    FIXED = "FIXED"
    CONDITIONAL = "CONDITIONAL"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------- field values

NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Krw = Annotated[int, Field(ge=0)]


class FactValue(BaseModel):
    """Base of every field value: strict (no coercion, so a float is never money), frozen and
    closed to unknown keys."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class TextValue(FactValue):
    text: NonEmpty


class SourcePrice(FactValue):
    """One source price with the exact source label that names it (ADR-0010 §7)."""

    label: NonEmpty
    amount_krw: Krw


class PricesValue(FactValue):
    prices: tuple[SourcePrice, ...] = Field(min_length=1)


class MoneyValue(FactValue):
    """An explicitly observed amount with its source label (``minimum_sale_price``)."""

    label: NonEmpty
    amount_krw: Krw


class QuantityTier(FactValue):
    """An original ``(quantity, total_price)`` pair; never divided into a unit price."""

    quantity: int = Field(ge=1)
    total_price_krw: Krw
    label: NonEmpty | None = None


class QuantityTiersValue(FactValue):
    tiers: tuple[QuantityTier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_quantities(self) -> Self:
        quantities = [tier.quantity for tier in self.tiers]
        if len(set(quantities)) != len(quantities):
            raise ValueError("quantity tiers must have distinct quantities")
        return self


class ShippingValue(FactValue):
    """The structured reading plus the exact source policy text. A conditional or unknown policy
    is never flattened into a guessed fixed fee (ADR-0010 §7)."""

    kind: ShippingKind
    policy_text: NonEmpty
    fee_krw: Krw | None = None
    free_over_krw: Krw | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind is ShippingKind.FREE and (
            self.fee_krw is not None or self.free_over_krw is not None
        ):
            raise ValueError("a FREE policy carries no fee and no threshold")
        if self.kind is ShippingKind.FIXED and (
            self.fee_krw is None or self.free_over_krw is not None
        ):
            raise ValueError("a FIXED policy carries a fee and no threshold")
        if self.kind is ShippingKind.UNKNOWN and (
            self.fee_krw is not None or self.free_over_krw is not None
        ):
            raise ValueError("an UNKNOWN policy carries no guessed fee")
        return self


class OptionAxis(FactValue):
    name: NonEmpty
    values: tuple[NonEmpty, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_values(self) -> Self:
        if len(set(self.values)) != len(self.values):
            raise ValueError("an option axis lists each value once")
        return self


class OptionConfiguration(FactValue):
    """One atomic source configuration: the exact ordered selections, one per axis."""

    selections: tuple[NonEmpty, ...]
    # Only when the source exposes one; never fabricated (ADR-0010 §7).
    supplier_sku_id: NonEmpty | None = None
    # The source's option surcharge as shown; it may be negative.
    additional_price_krw: int | None = None
    # Option-level sold-out evidence, where observable.
    sold_out: bool | None = None


class OptionsValue(FactValue):
    """Source option axes and configurations in source order. Count, grade and weight stay
    distinct values; a product whose page proves it has no option control has zero axes."""

    axes: tuple[OptionAxis, ...]
    configurations: tuple[OptionConfiguration, ...] = ()

    @model_validator(mode="after")
    def _configurations_match_axes(self) -> Self:
        names = [axis.name for axis in self.axes]
        if len(set(names)) != len(names):
            raise ValueError("option axis names are distinct")
        if not self.axes and self.configurations:
            raise ValueError("a product without option axes has no option configurations")
        seen: set[tuple[str, ...]] = set()
        for configuration in self.configurations:
            if len(configuration.selections) != len(self.axes):
                raise ValueError("a configuration selects exactly one value per axis")
            for axis, selection in zip(self.axes, configuration.selections, strict=True):
                if selection not in axis.values:
                    raise ValueError("a configuration selects only values its axis lists")
            if configuration.selections in seen:
                raise ValueError("each configuration is listed once")
            seen.add(configuration.selections)
        return self


class StockValue(FactValue):
    availability: Availability


class NoticeItem(FactValue):
    label: NonEmpty
    text: NonEmpty


class NoticeValue(FactValue):
    items: tuple[NoticeItem, ...] = Field(min_length=1)


class ImageSummary(FactValue):
    role: ImageRole
    ordinal: int = Field(ge=0)
    sha256: str | None
    status: FieldStatus


class ImagesValue(FactValue):
    """The ordered image references of a revision; derived, never supplied."""

    references: tuple[ImageSummary, ...] = Field(min_length=1)


@dataclass(frozen=True)
class FieldSpec:
    level: FieldLevel
    value_type: type[FactValue]


# ADR-0010 §7: the M3 field registry. Source identity and URL are revision columns, because a
# revision cannot exist without them; every other fact is one field. ``images`` is derived from the
# ordered image references, never supplied by a parser.
IMAGES_FIELD = "images"
FIELD_REGISTRY: Mapping[str, FieldSpec] = MappingProxyType(
    {
        "original_name": FieldSpec(FieldLevel.CORE, TextValue),
        "prices": FieldSpec(FieldLevel.CORE, PricesValue),
        "options": FieldSpec(FieldLevel.CORE, OptionsValue),
        IMAGES_FIELD: FieldSpec(FieldLevel.CORE, ImagesValue),
        "stock": FieldSpec(FieldLevel.CORE, StockValue),
        "shipping": FieldSpec(FieldLevel.COVERAGE, ShippingValue),
        "minimum_sale_price": FieldSpec(FieldLevel.COVERAGE, MoneyValue),
        "quantity_tiers": FieldSpec(FieldLevel.COVERAGE, QuantityTiersValue),
        "brand": FieldSpec(FieldLevel.COVERAGE, TextValue),
        "manufacturer": FieldSpec(FieldLevel.COVERAGE, TextValue),
        "origin": FieldSpec(FieldLevel.COVERAGE, TextValue),
        "notice": FieldSpec(FieldLevel.COVERAGE, NoticeValue),
        "detail_description": FieldSpec(FieldLevel.COVERAGE, TextValue),
    }
)
SUPPLIED_FIELDS = frozenset(FIELD_REGISTRY) - {IMAGES_FIELD}


# ---------------------------------------------------------------- collected input


@dataclass(frozen=True)
class Evidence:
    """One product-scoped evidence entry (ADR-0010 §8). ``observed`` is sanitized and bounded."""

    kind: EvidenceKind
    locator: str
    status: FieldStatus
    observed: str | None = None
    normalized: str | None = None


@dataclass(frozen=True)
class FieldFact:
    status: FieldStatus
    value: FactValue | None
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ImageReference:
    """One ordered image reference (ADR-0010 §9). ``locator`` is a sanitized stable locator when
    one is proven; otherwise only ``provenance`` (the reference's evidence locator in the product
    source) and ``host`` identify where it came from. No secret-bearing URL is ever held here."""

    role: ImageRole
    ordinal: int
    host: str
    provenance: str
    status: FieldStatus
    sha256: str | None = None
    locator: str | None = None
    issue: ImageIssue | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class CollectedFacts:
    """What one successful collection observed, before evaluation."""

    supplier_key: str
    source_product_id: str
    source_url: str
    captured_at: datetime
    extractor_revision: str
    extractor_fingerprint: str
    collection_run_id: str
    correlation_id: str
    fields: Mapping[str, FieldFact]
    images: tuple[ImageReference, ...] = ()


# ---------------------------------------------------------------- evaluated output


@dataclass(frozen=True)
class EvaluatedEvidence:
    evidence: Evidence
    digest: str


@dataclass(frozen=True)
class EvaluatedField:
    key: str
    level: FieldLevel
    status: FieldStatus
    value_json: str | None
    evidence: tuple[EvaluatedEvidence, ...]
    fingerprint: str


@dataclass(frozen=True)
class EvaluatedFacts:
    collected: CollectedFacts
    fields: tuple[EvaluatedField, ...]  # registry order
    images: tuple[ImageReference, ...]  # representative first, then source order
    source_fingerprint: str
    facts_status: FactsStatus


# ---------------------------------------------------------------- digests and fingerprints


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(payload: Mapping[str, Any]) -> str:
    text = canonical_json({"scheme": FINGERPRINT_SCHEME, **payload})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_digest(evidence: Evidence) -> str:
    return _digest(
        {
            "kind": evidence.kind.value,
            "locator": evidence.locator,
            "observed": evidence.observed,
            "normalized": evidence.normalized,
            "status": evidence.status.value,
        }
    )


def field_fingerprint(
    key: str, status: FieldStatus, value_json: str | None, evidence_digests: Iterable[str]
) -> str:
    """Content only (ADR-0010 §6): no capture time, run, session or signed-URL material."""
    value = None if value_json is None else json.loads(value_json)
    return _digest(
        {"field": key, "status": status.value, "value": value, "evidence": list(evidence_digests)}
    )


def source_fingerprint(
    fields: Iterable[tuple[str, str]], images: Iterable[tuple[ImageRole, int, str | None]]
) -> str:
    return _digest(
        {
            "fields": sorted([key, fingerprint] for key, fingerprint in fields),
            "images": [[role.value, ordinal, sha256] for role, ordinal, sha256 in images],
        }
    )


def facts_status(statuses: Mapping[str, FieldStatus]) -> FactsStatus:
    """Ruling on Q3: CONFIRMED iff every core field is CONFIRMED and no field anywhere is
    REVIEW_REQUIRED. ABSENT is legitimate only for a source-coverage field."""
    core_confirmed = all(
        statuses.get(key) is FieldStatus.CONFIRMED
        for key, spec in FIELD_REGISTRY.items()
        if spec.level is FieldLevel.CORE
    )
    if core_confirmed and FieldStatus.REVIEW_REQUIRED not in statuses.values():
        return FactsStatus.CONFIRMED
    return FactsStatus.REVIEW_REQUIRED


def atomic_configuration_key(source_product_id: str, selections: Iterable[str]) -> str:
    """ICBM-derived key of one atomic configuration: the source identity plus the exact ordered
    selections (ADR-0010 §7). It is never a supplier SKU ID."""
    return _digest(
        {"atomic_configuration": [source_product_id, *selections]},
    )


def value_from_json(key: str, value_json: str | None) -> FactValue | None:
    """Rebuild a stored field value, validated against the registry again."""
    if value_json is None:
        return None
    return FIELD_REGISTRY[key].value_type.model_validate_json(value_json)


# ---------------------------------------------------------------- evaluation


def _invalid(message: str) -> InputValidationError:
    return InputValidationError("COLLECT_FACTS_INVALID", message)


def _check_https_url(url: str, what: str, *, host: str | None = None) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "#" in url
    ):
        raise _invalid(f"{what} must be an https URL without credentials or fragment")
    if host is not None and parts.hostname != host:
        raise _invalid(f"{what} must be on its reference host")


def _check_identifier(value: str, what: str, maximum: int = IDENTIFIER_MAX_LENGTH) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise _invalid(f"{what} must be a non-empty identifier of at most {maximum} characters")


def _check_provenance(collected: CollectedFacts) -> None:
    if not SUPPLIER_KEY.fullmatch(collected.supplier_key):
        raise _invalid("supplier_key is not a supplier key")
    _check_identifier(
        collected.source_product_id, "source_product_id", SOURCE_PRODUCT_ID_MAX_LENGTH
    )
    _check_https_url(collected.source_url, "source_url")
    if collected.captured_at.tzinfo is None:
        raise _invalid("captured_at must be timezone-aware")
    _check_identifier(collected.extractor_revision, "extractor_revision")
    if not _HEX64.fullmatch(collected.extractor_fingerprint):
        raise _invalid("extractor_fingerprint must be a lowercase SHA-256 hex digest")
    _check_identifier(collected.collection_run_id, "collection_run_id")
    _check_identifier(collected.correlation_id, "correlation_id")


def _check_evidence(key: str, evidence: Evidence) -> None:
    if not isinstance(evidence.kind, EvidenceKind) or not isinstance(evidence.status, FieldStatus):
        raise _invalid(f"{key}: evidence kind and status must be members of their vocabularies")
    if not evidence.locator.strip():
        raise _invalid(f"{key}: every evidence entry has a locator")
    observed = evidence.observed
    if observed is not None and len(observed.encode("utf-8")) > EVIDENCE_OBSERVED_MAX_BYTES:
        raise _invalid(
            f"{key}: observed evidence exceeds {EVIDENCE_OBSERVED_MAX_BYTES} bytes; keep larger "
            "evidence by digest only"
        )


def _check_field(key: str, spec: FieldSpec, fact: FieldFact) -> str | None:
    """Validate one supplied field and return its canonical value JSON."""
    if not isinstance(fact.status, FieldStatus):
        raise _invalid(f"{key}: status must be a FieldStatus")
    value = fact.value
    if value is not None and type(value) is not spec.value_type:
        raise _invalid(f"{key}: value must be {spec.value_type.__name__}")
    if fact.status is FieldStatus.ABSENT and value is not None:
        raise _invalid(f"{key}: an ABSENT field has no value")
    if fact.status is FieldStatus.CONFIRMED and value is None:
        raise _invalid(f"{key}: a CONFIRMED field has a value")
    if not fact.evidence:
        raise _invalid(f"{key}: every field keeps the evidence that explains it")
    for evidence in fact.evidence:
        _check_evidence(key, evidence)
    if value is None:
        return None
    raw = value.model_dump_json()
    try:  # again, so a value built without validation (model_construct) is still refused
        spec.value_type.model_validate_json(raw)
    except ValidationError as exc:
        raise _invalid(f"{key}: value failed validation ({exc.error_count()} errors)") from None
    if isinstance(value, StockValue):
        decided = value.availability is not Availability.REVIEW_REQUIRED
        if decided != (fact.status is FieldStatus.CONFIRMED):
            raise _invalid("stock: a decided availability is CONFIRMED, and only a decided one")
    if (
        isinstance(value, ShippingValue)
        and value.kind is ShippingKind.UNKNOWN
        and fact.status is not FieldStatus.REVIEW_REQUIRED
    ):
        raise _invalid("shipping: an UNKNOWN policy is REVIEW_REQUIRED")
    return canonical_json(json.loads(raw))


_IMAGE_STATUSES = (FieldStatus.CONFIRMED, FieldStatus.REVIEW_REQUIRED)
_ROLE_ORDER = {ImageRole.REPRESENTATIVE: 0, ImageRole.DETAIL: 1}


def image_order(ref: ImageReference) -> tuple[int, int]:
    """Representative images first, then each role in source order."""
    return _ROLE_ORDER[ref.role], ref.ordinal


def _check_images(references: Iterable[ImageReference]) -> tuple[ImageReference, ...]:
    seen: set[tuple[ImageRole, int]] = set()
    checked = []
    for ref in references:
        if not isinstance(ref.role, ImageRole) or ref.status not in _IMAGE_STATUSES:
            raise _invalid("images: a reference has a role and is CONFIRMED or REVIEW_REQUIRED")
        if ref.issue is not None and not isinstance(ref.issue, ImageIssue):
            raise _invalid("images: issue must be an ImageIssue")
        if not isinstance(ref.ordinal, int) or ref.ordinal < 0 or (ref.role, ref.ordinal) in seen:
            raise _invalid("images: each role keeps distinct non-negative source positions")
        seen.add((ref.role, ref.ordinal))
        if not _HOST.fullmatch(ref.host):
            raise _invalid("images: host must be a lowercase host name")
        if not ref.provenance.strip():
            raise _invalid("images: every reference keeps its provenance")
        if ref.sha256 is not None and not _HEX64.fullmatch(ref.sha256):
            raise _invalid("images: sha256 must be a lowercase SHA-256 hex digest")
        if ref.status is FieldStatus.CONFIRMED and (ref.sha256 is None or ref.issue is not None):
            raise _invalid("images: a CONFIRMED reference has observed bytes and no issue")
        if ref.status is FieldStatus.REVIEW_REQUIRED and ref.issue is None:
            raise _invalid("images: a REVIEW_REQUIRED reference names its issue")
        if ref.locator is not None:
            _check_https_url(ref.locator, "images: locator", host=ref.host)
        checked.append(ref)
    return tuple(sorted(checked, key=lambda ref: (_ROLE_ORDER[ref.role], ref.ordinal)))


def _images_field(
    references: tuple[ImageReference, ...],
) -> tuple[FieldStatus, ImagesValue | None, tuple[Evidence, ...]]:
    """Derive the images field: CONFIRMED only with a confirmed representative image and no
    reference under review; ABSENT when the source shows no image at all."""
    if not references:
        return FieldStatus.ABSENT, None, ()
    value = ImagesValue(
        references=tuple(
            ImageSummary(role=ref.role, ordinal=ref.ordinal, sha256=ref.sha256, status=ref.status)
            for ref in references
        )
    )
    evidence = tuple(
        Evidence(
            kind=EvidenceKind.IMAGE,
            locator=ref.provenance,
            status=ref.status,
            observed=ref.sha256,
            normalized=f"{ref.role.value}:{ref.ordinal}",
        )
        for ref in references
    )
    representative = any(
        ref.role is ImageRole.REPRESENTATIVE and ref.status is FieldStatus.CONFIRMED
        for ref in references
    )
    if representative and all(ref.status is FieldStatus.CONFIRMED for ref in references):
        return FieldStatus.CONFIRMED, value, evidence
    return FieldStatus.REVIEW_REQUIRED, value, evidence


def evaluate(collected: CollectedFacts) -> EvaluatedFacts:
    """Validate one collection and compute everything the revision store persists."""
    _check_provenance(collected)
    supplied = set(collected.fields)
    if missing := SUPPLIED_FIELDS - supplied:
        raise _invalid(f"every field reports a status; missing: {sorted(missing)}")
    if unknown := supplied - SUPPLIED_FIELDS:
        raise _invalid(f"unknown or derived fields supplied: {sorted(unknown)}")
    images = _check_images(collected.images)
    fields = []
    for key, spec in FIELD_REGISTRY.items():
        if key == IMAGES_FIELD:
            status, value, evidence = _images_field(images)
            value_json = None if value is None else canonical_json(value.model_dump(mode="json"))
        else:
            fact = collected.fields[key]
            value_json = _check_field(key, spec, fact)
            status, evidence = fact.status, fact.evidence
        evaluated = tuple(EvaluatedEvidence(e, evidence_digest(e)) for e in evidence)
        fingerprint = field_fingerprint(key, status, value_json, (e.digest for e in evaluated))
        fields.append(EvaluatedField(key, spec.level, status, value_json, evaluated, fingerprint))
    return EvaluatedFacts(
        collected=collected,
        fields=tuple(fields),
        images=images,
        source_fingerprint=source_fingerprint(
            ((field.key, field.fingerprint) for field in fields),
            ((ref.role, ref.ordinal, ref.sha256) for ref in images),
        ),
        facts_status=facts_status({field.key: field.status for field in fields}),
    )
