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
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from app.collect.urls import (
    STRICT_URL_POLICY,
    UrlPolicy,
    check_persisted_text,
    require_sanitized,
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


class LocatorForm(StrEnum):
    """How the source page wrote an image reference, before it was resolved against the page.

    The written text itself is kept in memory only: it may carry a token, a credential or a scheme
    the persisted-URL rule refuses (ADR-0010 §9), so what the revision keeps is its form.
    """

    ABSOLUTE = "ABSOLUTE"  # it names its own scheme
    PROTOCOL_RELATIVE = "PROTOCOL_RELATIVE"  # ``//host/path``: the page's scheme is borrowed
    RELATIVE = "RELATIVE"  # a path, resolved against the page's own URL


class FetchTargetRefusal(StrEnum):
    """Why the COLLECT transport's target check refused a URL before anything was reserved or sent.

    Closed on purpose: every refusal the target check can make has exactly one member, and there is
    no catch-all. A failure that is not one of these is not a target refusal at all and keeps its
    own classification (Issue #52 ruling 5716978033 §4).
    """

    UNPARSEABLE = "UNPARSEABLE"  # no parseable URL: bad bracket, bad port, or no host
    WHITESPACE = "WHITESPACE"  # whitespace anywhere in the URL
    FRAGMENT = "FRAGMENT"  # a ``#fragment``
    NOT_ABSOLUTE = "NOT_ABSOLUTE"  # no scheme: a relative or protocol-relative URL left unresolved
    NON_HTTPS = "NON_HTTPS"  # ``http``
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"  # any other scheme
    CREDENTIALS_PRESENT = "CREDENTIALS_PRESENT"  # a user or password in the authority
    NON_STANDARD_PORT = "NON_STANDARD_PORT"  # a port other than 443
    HOST_NOT_ALLOWLISTED = "HOST_NOT_ALLOWLISTED"  # not a host the profile allows for this kind
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"  # not a path the profile allows for this kind
    QUERY_NOT_ALLOWED = "QUERY_NOT_ALLOWED"  # a query this kind of read may not carry


class ImageCertainty(StrEnum):
    """Whether looking at a source image reference again could change its outcome (Issue #52 ruling
    5723016554 R1). Kept apart from what acceptance then does with the reference."""

    DETERMINATE = "DETERMINATE"  # the outcome is settled by what was recorded
    INDETERMINATE = "INDETERMINATE"  # ICBM did not finish observing it, or cannot say whose it is


class ImageDisposition(StrEnum):
    """What acceptance does with one source image reference (ruling 5723016554 R1).

    ``UNRESOLVED`` is every reference that is neither observed nor proven to be the source's own
    defect — a failed request, a refusal the closed table does not name, a budget ICBM spent. It is
    never folded into ``EXCLUDED``, and one of them keeps the images field under review (R7).
    """

    INCLUDED = "INCLUDED"  # its bytes were observed: it is product evidence
    EXCLUDED = "EXCLUDED"  # the source authored a reference no fetch can honour (R2)
    UNRESOLVED = "UNRESOLVED"  # not observed, and not proven to be the source's own defect


class ImageExclusion(StrEnum):
    """Why a reference is ``EXCLUDED``. Closed (ruling 5723016554 R11): exactly one member per row
    of :data:`EXCLUSION_TABLE`, and no catch-all. A case no row names stays ``UNRESOLVED``."""

    SOURCE_AUTHORED_NON_HTTPS = "SOURCE_AUTHORED_NON_HTTPS"


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


# The acceptance model a revision's image decisions were made under. A revision recorded before
# the model existed carries none, and its references were never classified: nothing reads one of
# them as if the table had been applied (ruling 5723016554, history is immutable).
ImageAcceptanceModel = Literal["image-acceptance/v1"]
IMAGE_ACCEPTANCE_MODEL: ImageAcceptanceModel = "image-acceptance/v1"

# What each decision may look like: the reference's own observation beside the two axes. Anything
# else is refused, so a stored decision is always one the table could have produced.
_DECISIONS: Mapping[tuple[ImageCertainty, ImageDisposition], FieldStatus] = MappingProxyType(
    {
        (ImageCertainty.DETERMINATE, ImageDisposition.INCLUDED): FieldStatus.CONFIRMED,
        (ImageCertainty.DETERMINATE, ImageDisposition.EXCLUDED): FieldStatus.REVIEW_REQUIRED,
        (ImageCertainty.INDETERMINATE, ImageDisposition.UNRESOLVED): FieldStatus.REVIEW_REQUIRED,
    }
)


class ImageSummary(FactValue):
    """One reference as the images field records it: what was observed, and what acceptance did.

    ``status`` is the observation — CONFIRMED only when its bytes were stored. ``certainty``,
    ``disposition`` and ``exclusion`` are the acceptance decision (ruling 5723016554 R1); they are
    ``None`` together on a revision recorded before the acceptance model existed.
    """

    role: ImageRole
    ordinal: int = Field(ge=0)
    sha256: str | None
    status: FieldStatus
    certainty: ImageCertainty | None = None
    disposition: ImageDisposition | None = None
    exclusion: ImageExclusion | None = None

    @model_validator(mode="after")
    def _one_decision(self) -> Self:
        if self.certainty is None and self.disposition is None:
            if self.exclusion is not None:
                raise ValueError("an exclusion is recorded only with its decision")
            return self
        if self.certainty is None or self.disposition is None:
            raise ValueError("certainty and disposition are recorded together")
        observed = _DECISIONS.get((self.certainty, self.disposition))
        if observed is None:
            raise ValueError("not a decision the acceptance table can make")
        if self.status is not observed or (self.sha256 is not None) != (
            self.disposition is ImageDisposition.INCLUDED
        ):
            raise ValueError("a decision agrees with what was observed of the reference")
        if (self.exclusion is not None) != (self.disposition is ImageDisposition.EXCLUDED):
            raise ValueError("an EXCLUDED reference names its closed reason, and only it does")
        return self


class ImagesValue(FactValue):
    """The ordered image references of a revision; derived, never supplied."""

    references: tuple[ImageSummary, ...] = Field(min_length=1)
    acceptance: ImageAcceptanceModel | None = None

    @model_validator(mode="after")
    def _decided_under_one_model(self) -> Self:
        decided = {reference.disposition is not None for reference in self.references}
        if decided == {True} and self.acceptance is None:
            raise ValueError("classified references name the acceptance model that decided them")
        if decided != {True} and self.acceptance is not None:
            raise ValueError("an acceptance model classifies every reference, or none was applied")
        return self


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
    source) and ``host`` identify where it came from. No secret-bearing URL is ever held here.

    What the source wrote and what could be fetched are kept apart (Issue #52 ruling 5716978033):

    * ``source_form`` and ``source_trimmed`` say how the page wrote the reference — absolute,
      protocol-relative or relative, and whether surrounding whitespace was trimmed before it was
      resolved. ``None`` when the supplier did not report what the page wrote.
    * ``locator`` is the canonical fetch target, as the transport's target check computes it, when
      that target is fetchable and carries no query; whether or not its bytes then arrived.
    * ``target_refusal`` is why the transport's target check refused the resolved reference before
      anything was reserved or sent. A refused reference has no locator and no bytes.
    """

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
    source_form: LocatorForm | None = None
    source_trimmed: bool | None = None
    target_refusal: FetchTargetRefusal | None = None


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


def _check_identifier(value: str, what: str, maximum: int = IDENTIFIER_MAX_LENGTH) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise _invalid(f"{what} must be a non-empty identifier of at most {maximum} characters")


def _check_provenance(collected: CollectedFacts, policy: UrlPolicy) -> None:
    if not SUPPLIER_KEY.fullmatch(collected.supplier_key):
        raise _invalid("supplier_key is not a supplier key")
    _check_identifier(
        collected.source_product_id, "source_product_id", SOURCE_PRODUCT_ID_MAX_LENGTH
    )
    require_sanitized(collected.source_url, policy, "source_url")
    if collected.captured_at.tzinfo is None:
        raise _invalid("captured_at must be timezone-aware")
    _check_identifier(collected.extractor_revision, "extractor_revision")
    if not _HEX64.fullmatch(collected.extractor_fingerprint):
        raise _invalid("extractor_fingerprint must be a lowercase SHA-256 hex digest")
    _check_identifier(collected.collection_run_id, "collection_run_id")
    _check_identifier(collected.correlation_id, "correlation_id")
    for what in ("source_product_id", "extractor_revision", "collection_run_id", "correlation_id"):
        check_persisted_text(getattr(collected, what), policy, what)


def _check_evidence(key: str, evidence: Evidence, policy: UrlPolicy) -> None:
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
    what = f"{key}: evidence"
    for text in (evidence.locator, evidence.observed, evidence.normalized):
        if text is not None:
            check_persisted_text(text, policy, what)
    if evidence.kind is EvidenceKind.URL:
        for url in (evidence.observed, evidence.normalized):
            if url is not None:
                require_sanitized(url, policy, what)


def _check_coherence(key: str, status: FieldStatus, evidence: Iterable[Evidence]) -> None:
    """A field status never hides the status of its evidence (PR #57 review 5213637642 §2):

    CONFIRMED        at least one CONFIRMED entry and no REVIEW_REQUIRED entry
    ABSENT           every entry ABSENT
    REVIEW_REQUIRED  at least one REVIEW_REQUIRED entry

    REVIEW_REQUIRED evidence therefore always makes its field REVIEW_REQUIRED, and with it the
    revision's ``facts_status``: unresolved evidence cannot disappear behind a stronger status.
    """
    statuses = {entry.status for entry in evidence}
    if status is FieldStatus.CONFIRMED:
        coherent = FieldStatus.CONFIRMED in statuses and FieldStatus.REVIEW_REQUIRED not in statuses
    elif status is FieldStatus.ABSENT:
        coherent = statuses <= {FieldStatus.ABSENT}
    else:
        coherent = FieldStatus.REVIEW_REQUIRED in statuses
    if not coherent:
        raise _invalid(f"{key}: a {status.value} field must agree with the status of its evidence")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for k, v in value.items() for text in (k, *_strings(v))]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _check_field(key: str, spec: FieldSpec, fact: FieldFact, policy: UrlPolicy) -> str | None:
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
        _check_evidence(key, evidence, policy)
    _check_coherence(key, fact.status, fact.evidence)
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
    value_json = canonical_json(json.loads(raw))
    for text in _strings(json.loads(value_json)):
        check_persisted_text(text, policy, f"{key}: value")
    return value_json


_IMAGE_STATUSES = (FieldStatus.CONFIRMED, FieldStatus.REVIEW_REQUIRED)
_ROLE_ORDER = {ImageRole.REPRESENTATIVE: 0, ImageRole.DETAIL: 1}
# The derived evidence entries that stand for an images guard that failed. Each is REVIEW_REQUIRED
# evidence of its own, so the derived field stays coherent with its evidence like every other.
MISSING_REPRESENTATIVE_LOCATOR = "images:representative"
MISSING_DETAIL_LOCATOR = "images:detail"
EXCLUDED_SHARE_LOCATOR = "images:excluded"


# ---------------------------------------------------------------- image acceptance

# The whole of what may be EXCLUDED (ruling 5723016554 R2, R3, R11, R13). A row is what the page
# wrote beside what the transport's own target check refused before anything was reserved or sent;
# a pair that is not a row stays UNRESOLVED. Nothing here parses, resolves or repairs a URL: the
# transport judged the target once, and this reads only what it recorded.
#
# Each row must prove the defect is the source's own and could not be ICBM's. That is why a row
# is keyed on the written form and not on the refusal alone: under the parser contract (PR #74) an
# ABSOLUTE reference keeps the scheme the page gave it, while a relative or protocol-relative one
# is resolved by ICBM — so ``NOT_ABSOLUTE``, or ``NON_HTTPS`` after a resolution, could be a parser
# regression and is never a row (R3). Profile-scoped refusals (host, path, query) can mean ICBM's
# configuration is behind the source, and are not rows either.
EXCLUSION_TABLE: Mapping[tuple[LocatorForm, FetchTargetRefusal], ImageExclusion] = MappingProxyType(
    {
        # The page wrote an absolute ``http://`` reference; the parser keeps an absolute
        # scheme and the transport serves https only (ADR-0010 §9). Looking again can change
        # nothing, and nothing was requested for it.
        (
            LocatorForm.ABSOLUTE,
            FetchTargetRefusal.NON_HTTPS,
        ): ImageExclusion.SOURCE_AUTHORED_NON_HTTPS,
    }
)
# The drift guard (R9): images stay under review when excluded references are MORE than this share
# of the references the source exposed. Exactly this share does not fire it.
EXCLUDED_SHARE_LIMIT = Fraction(1, 3)


@dataclass(frozen=True)
class ImageDecision:
    certainty: ImageCertainty
    disposition: ImageDisposition
    exclusion: ImageExclusion | None = None


_INCLUDED = ImageDecision(ImageCertainty.DETERMINATE, ImageDisposition.INCLUDED)
_UNRESOLVED = ImageDecision(ImageCertainty.INDETERMINATE, ImageDisposition.UNRESOLVED)


def decide_image(ref: ImageReference) -> ImageDecision:
    """What acceptance does with one reference, from its persisted diagnostics alone (R1–R5).

    Observed bytes are included. A reference is excluded only when a row of the closed table
    names exactly what the page wrote and what the transport refused before sending. Everything
    else — budget, network, server, content the validator refused, a refusal no row names — stays
    unresolved, however often it repeats.
    """
    if ref.status is FieldStatus.CONFIRMED:
        return _INCLUDED
    if ref.issue is ImageIssue.BUDGET_EXHAUSTED:
        # R4: ICBM chose not to finish looking. That is never the source's defect, whatever the
        # reference would have been refused for had it been reached.
        return _UNRESOLVED
    if (
        ref.issue is ImageIssue.FETCH_FAILED
        and ref.target_refusal is not None
        and ref.source_form is not None
        and ref.source_trimmed is not None
        and ref.sha256 is None
        and ref.locator is None
    ):
        exclusion = EXCLUSION_TABLE.get((ref.source_form, ref.target_refusal))
        if exclusion is not None:
            return ImageDecision(ImageCertainty.DETERMINATE, ImageDisposition.EXCLUDED, exclusion)
    return _UNRESOLVED


# What one reference contributes to the images field's evidence: an excluded reference is no usable
# image, which is ABSENT evidence — neither confirmation nor something still to resolve.
_EVIDENCE_STATUS: Mapping[ImageDisposition, FieldStatus] = MappingProxyType(
    {
        ImageDisposition.INCLUDED: FieldStatus.CONFIRMED,
        ImageDisposition.EXCLUDED: FieldStatus.ABSENT,
        ImageDisposition.UNRESOLVED: FieldStatus.REVIEW_REQUIRED,
    }
)


def image_order(ref: ImageReference) -> tuple[int, int]:
    """Representative images first, then each role in source order."""
    return _ROLE_ORDER[ref.role], ref.ordinal


def _check_images(
    references: Iterable[ImageReference], policy: UrlPolicy
) -> tuple[ImageReference, ...]:
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
            require_sanitized(ref.locator, policy, "images: locator")
            if urlsplit(ref.locator).hostname != ref.host:
                raise _invalid("images: a locator is on its reference host")
        if ref.source_form is not None and not isinstance(ref.source_form, LocatorForm):
            raise _invalid("images: source_form must be a LocatorForm")
        if ref.source_trimmed is not None and (
            not isinstance(ref.source_trimmed, bool) or ref.source_form is None
        ):
            raise _invalid("images: source_trimmed is a bool reported with the source form")
        if ref.target_refusal is not None:
            if not isinstance(ref.target_refusal, FetchTargetRefusal):
                raise _invalid("images: target_refusal must be a FetchTargetRefusal")
            if (
                ref.status is not FieldStatus.REVIEW_REQUIRED
                or ref.sha256 is not None
                or ref.locator is not None
            ):
                raise _invalid(
                    "images: a refused fetch target is REVIEW_REQUIRED with no bytes and no locator"
                )
        for text in (ref.provenance, ref.etag, ref.last_modified):
            if text is not None:
                check_persisted_text(text, policy, "images: reference")
        checked.append(ref)
    return tuple(sorted(checked, key=image_order))


def _guard(locator: str, normalized: str) -> Evidence:
    return Evidence(
        kind=EvidenceKind.IMAGE,
        locator=locator,
        status=FieldStatus.REVIEW_REQUIRED,
        normalized=normalized,
    )


def _images_field(
    references: tuple[ImageReference, ...],
) -> tuple[FieldStatus, ImagesValue | None, tuple[Evidence, ...]]:
    """Derive the images field from the references the source exposed (ruling 5723016554).

    ABSENT when the source shows no image at all. Otherwise every reference is decided by
    :func:`decide_image`, and the field is CONFIRMED only when all of these hold:

    * no reference is UNRESOLVED (R7) — one is enough to keep the field under review;
    * a REPRESENTATIVE reference is INCLUDED, and — when the source exposed any DETAIL reference at
      all — so is a DETAIL one (R6). What the source exposed is read from the references
      themselves, never from which requests succeeded (R12): a reference that was refused or
      excluded still proves the source had it;
    * excluded references are not more than one third of all of them (R9), counting every excluded
      position on its own even when several share one reason (R10).

    With those met, excluded references do not by themselves lower the field (R8). Each guard that
    fails is REVIEW_REQUIRED evidence of its own, so the field stays coherent with its evidence.
    """
    if not references:
        return FieldStatus.ABSENT, None, ()
    decided = [(ref, decide_image(ref)) for ref in references]
    value = ImagesValue(
        acceptance=IMAGE_ACCEPTANCE_MODEL,
        references=tuple(
            ImageSummary(
                role=ref.role,
                ordinal=ref.ordinal,
                sha256=ref.sha256,
                status=ref.status,
                certainty=decision.certainty,
                disposition=decision.disposition,
                exclusion=decision.exclusion,
            )
            for ref, decision in decided
        ),
    )
    evidence = [
        Evidence(
            kind=EvidenceKind.IMAGE,
            locator=ref.provenance,
            status=_EVIDENCE_STATUS[decision.disposition],
            observed=ref.sha256,
            normalized=f"{ref.role.value}:{ref.ordinal}"
            if decision.exclusion is None
            else f"{ref.role.value}:{ref.ordinal}:EXCLUDED:{decision.exclusion.value}",
        )
        for ref, decision in decided
    ]
    included = Counter(
        ref.role for ref, decision in decided if decision.disposition is ImageDisposition.INCLUDED
    )
    dispositions = [decision.disposition for _, decision in decided]
    unresolved = ImageDisposition.UNRESOLVED in dispositions
    # R10: every excluded position counts, however many share one reason.
    excluded = dispositions.count(ImageDisposition.EXCLUDED)
    exposes_detail = any(ref.role is ImageRole.DETAIL for ref in references)

    guards = []
    if not included[ImageRole.REPRESENTATIVE]:
        guards.append(
            _guard(MISSING_REPRESENTATIVE_LOCATOR, f"{ImageRole.REPRESENTATIVE.value}:missing")
        )
    if exposes_detail and not included[ImageRole.DETAIL]:
        guards.append(_guard(MISSING_DETAIL_LOCATOR, f"{ImageRole.DETAIL.value}:missing"))
    if Fraction(excluded, len(references)) > EXCLUDED_SHARE_LIMIT:
        guards.append(_guard(EXCLUDED_SHARE_LOCATOR, f"EXCLUDED:{excluded}/{len(references)}"))
    if not unresolved and not guards:
        return FieldStatus.CONFIRMED, value, tuple(evidence)
    return FieldStatus.REVIEW_REQUIRED, value, (*evidence, *guards)


def evaluate(
    collected: CollectedFacts, url_policy: UrlPolicy = STRICT_URL_POLICY
) -> EvaluatedFacts:
    """Validate one collection and compute everything the revision store persists.

    ``url_policy`` names the explicitly safe query keys per host (the supplier's collection
    profile, PR-C). The default keeps none: every URL with a query fails closed.
    """
    _check_provenance(collected, url_policy)
    supplied = set(collected.fields)
    if missing := SUPPLIED_FIELDS - supplied:
        raise _invalid(f"every field reports a status; missing: {sorted(missing)}")
    if unknown := supplied - SUPPLIED_FIELDS:
        raise _invalid(f"unknown or derived fields supplied: {sorted(unknown)}")
    images = _check_images(collected.images, url_policy)
    fields = []
    for key, spec in FIELD_REGISTRY.items():
        if key == IMAGES_FIELD:
            status, value, evidence = _images_field(images)
            _check_coherence(key, status, evidence)
            value_json = None if value is None else canonical_json(value.model_dump(mode="json"))
        else:
            fact = collected.fields[key]
            value_json = _check_field(key, spec, fact, url_policy)
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
