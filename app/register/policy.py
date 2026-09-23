"""M5 PR-C: the target registration policy and the category metadata interfaces (ADR-0014 §3, §4,
§5, §21; Issue #89 kickoff 5742880432 §8).

Pure and provider-neutral: no database, no provider, no I/O. Nothing here is SmartStore truth.
- :class:`TargetPolicy` is the Settings/platform policy of one marketplace × canonical account:
  its revisions, the pricing context the M4 owner prices under, the shipping/returns/exchange
  template identities, the duplicate-proof rule, the sanitizer profile and the asset profile.
- :class:`CategoryMetadata` is reviewed metadata of one category under one taxonomy revision: which
  attributes and notice fields it **requires**, whether a field may be "상세페이지 참조", the option
  rule and the templates it needs. Required fields are driven by this versioned metadata only;
  nothing is required, filled or guessed per product.
- :class:`StaticRegistrationMetadata` is an offline, in-memory metadata source for tests and the
  offline harness. Production reads the durable operator-reviewed owner of
  ``app.register.category_metadata`` (ADR-0015 §3, Gate 1 G1-B).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.products.model import ReadinessStatus
from app.products.pricing import PricingContextInput


class Provenance(StrEnum):
    """Where an outbound value comes from. Only a source fact or an operator's confirmation may
    satisfy a required field; an AI suggestion never does (ADR-0014 §4, §18; ADR-0004)."""

    SOURCE_FACT = "SOURCE_FACT"
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    AI_SUGGESTION = "AI_SUGGESTION"


SATISFYING: frozenset[Provenance] = frozenset(
    {Provenance.SOURCE_FACT, Provenance.OPERATOR_CONFIRMED}
)


class DuplicateKeyKind(StrEnum):
    """The provider lookup keys of the duplicate preflight (ADR-0014 §13). A normalized name is
    only a weaker signal."""

    SELLER_CODE = "SELLER_CODE"
    GTIN = "GTIN"
    OFFICIAL_KEY = "OFFICIAL_KEY"
    NORMALIZED_NAME = "NORMALIZED_NAME"


STRONG_KEYS: frozenset[DuplicateKeyKind] = frozenset(
    {DuplicateKeyKind.SELLER_CODE, DuplicateKeyKind.GTIN, DuplicateKeyKind.OFFICIAL_KEY}
)


@dataclass(frozen=True)
class FieldRule:
    """One attribute or notice field the category metadata declares.

    ``missing_status`` is the versioned rule's own answer when a required value is absent:
    ``REVIEW_REQUIRED`` or ``BLOCKED``. ``detail_page_reference_allowed`` is true only where the
    reviewed metadata states the marketplace accepts "상세페이지 참조" for this field.
    """

    key: str
    required: bool
    detail_page_reference_allowed: bool = False
    missing_status: ReadinessStatus = ReadinessStatus.REVIEW_REQUIRED
    max_length: int | None = None

    def __post_init__(self) -> None:
        if self.missing_status not in (ReadinessStatus.REVIEW_REQUIRED, ReadinessStatus.BLOCKED):
            raise ValueError("a missing required field is REVIEW_REQUIRED or BLOCKED")


@dataclass(frozen=True)
class NoticePolicy:
    """The product-information disclosure type and its declared fields."""

    notice_type: str
    fields: tuple[FieldRule, ...] = ()


@dataclass(frozen=True)
class OptionPolicy:
    """Whether a listing of this category may carry several Items as options, and how many."""

    options_supported: bool
    max_options: int = 1
    max_dimensions: int = 1


@dataclass(frozen=True)
class AssetPolicy:
    """The marketplace publication-asset profile the target requests (ADR-0014 §5, R1).

    ``provider_asset_identity_required`` says whether CREATE needs a provider-issued image
    identity; only then is an upload prepared, and only after a READY non-asset candidate.
    """

    profile: str
    min_images: int = 1
    max_images: int = 10
    requires_representative: bool = True
    provider_asset_identity_required: bool = True


@dataclass(frozen=True)
class CategoryMetadata:
    """Reviewed metadata of one category under one taxonomy revision."""

    taxonomy_revision: str
    category_id: str
    metadata_revision: str
    reviewed: bool
    leaf: bool = True
    registrable: bool = True
    attributes: tuple[FieldRule, ...] = ()
    notice: NoticePolicy | None = None
    options: OptionPolicy = OptionPolicy(options_supported=False)
    name_max_length: int = 100
    required_templates: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TargetPolicy:
    """The Settings and platform policy of one marketplace × canonical account (ADR-0014 §21).

    It is never ProductFacts and never a global product hardcode: a concrete template identity is
    configuration of this account only. ``pricing_context`` is the context the M4 pricing owner
    prices this target under; M5 never prices.
    """

    marketplace_key: str
    marketplace_account_id: str
    policy_revision: str
    taxonomy_revision: str
    pricing_context: PricingContextInput
    sanitizer_profile_version: str
    asset_policy: AssetPolicy
    # Server-owned authoring revisions exposed to the operator form. A deployment without them
    # cannot author a category or detail by inventing a client-side revision label.
    category_mapping_revision: str | None = None
    detail_composition_revision: str | None = None
    templates: Mapping[str, str] = field(default_factory=dict)
    duplicate_proof_required: bool = True
    duplicate_lookup_keys: frozenset[DuplicateKeyKind] = frozenset({DuplicateKeyKind.SELLER_CODE})


class RegistrationMetadataSource(Protocol):
    """The current category metadata of one ``marketplace × taxonomy revision × category``
    (ADR-0015 §3). The marketplace is part of the key: a source never serves one marketplace's
    metadata for another, and another taxonomy revision never stands in."""

    def category(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> CategoryMetadata | None: ...

    def revision(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str, metadata_revision: str
    ) -> CategoryMetadata | None:
        """The exact historical revision, only within this key; ``None`` when this key holds no
        such revision. A frozen Snapshot is rendered with it, never with the current one."""
        ...


class RegistrationPolicySource(Protocol):
    """The current Settings/platform registration policy of one marketplace × canonical account.

    It is read on every evaluation, so a Settings change after a preflight changes the next
    evaluation's fingerprint and a Snapshot built from the older one is refused as stale."""

    def target(self, marketplace_key: str, marketplace_account_id: str) -> TargetPolicy | None: ...


class StaticRegistrationMetadata:
    """An offline metadata source holding exactly the entries its caller supplies, for one named
    marketplace. A test and offline fixture only: production reads the durable reviewed owner
    (ADR-0015 §3, G1-10)."""

    def __init__(
        self, entries: Iterable[CategoryMetadata] = (), *, marketplace_key: str | None = None
    ) -> None:
        self._marketplace_key = marketplace_key
        self._entries: dict[tuple[str, str], CategoryMetadata] = {}
        for entry in entries:
            self.put(entry)

    def put(self, entry: CategoryMetadata) -> None:
        if self._marketplace_key is None:
            raise ValueError("a metadata entry belongs to a named marketplace")
        self._entries[(entry.taxonomy_revision, entry.category_id)] = entry

    def category(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str
    ) -> CategoryMetadata | None:
        if marketplace_key != self._marketplace_key:
            return None
        return self._entries.get((taxonomy_revision, category_id))

    def revision(
        self, marketplace_key: str, taxonomy_revision: str, category_id: str, metadata_revision: str
    ) -> CategoryMetadata | None:
        entry = self.category(marketplace_key, taxonomy_revision, category_id)
        return entry if entry is not None and entry.metadata_revision == metadata_revision else None


class StaticRegistrationPolicy:
    """An offline policy source holding exactly the account policies its caller supplies. The
    production wiring starts it empty: no account has a registration policy until Settings owns
    one, so every preflight fails closed."""

    def __init__(self, policies: Iterable[TargetPolicy] = ()) -> None:
        self._policies = {(p.marketplace_key, p.marketplace_account_id): p for p in policies}

    def put(self, policy: TargetPolicy) -> None:
        self._policies[(policy.marketplace_key, policy.marketplace_account_id)] = policy

    def target(self, marketplace_key: str, marketplace_account_id: str) -> TargetPolicy | None:
        return self._policies.get((marketplace_key, marketplace_account_id))
