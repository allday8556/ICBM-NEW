"""Synthetic REGISTER fixtures for M5 tests (Issue #89).

Everything is invented: the marketplace key, the provider account identity, the category, its
reviewed metadata and every outbound value. No provider is reachable from here: a committed M2
binding unit is written as the state an explicit M2 binding would have left, and the CONNECT
capability is a fixed read model.
"""

import contextlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from app.collect.facts import FieldFact, ImageRole, QuantityTier, QuantityTiersValue
from app.collect.revisions import StoredRevision
from app.config import AppConfig
from app.connect.marketplace.capability import (
    AuthStatus,
    ContractFreshness,
    WorkflowScope,
    WorkflowState,
    WriteScopeStatus,
    WriteStatus,
)
from app.connect.marketplace.contracts import (
    MarketplaceCapabilityView,
    WorkflowOverlayView,
    WriteScopeView,
    WriteView,
)
from app.container import Container
from app.products.image_model import QaVerdict, SelectedOutput, SourceDecision, SourceDecisionKind
from app.products.model import ReadinessStatus
from app.products.pricing import PricingContextInput
from app.register.model import ListingShape
from app.register.policy import (
    AssetPolicy,
    CategoryMetadata,
    DuplicateKeyKind,
    FieldRule,
    NoticePolicy,
    OptionPolicy,
    Provenance,
    StaticRegistrationMetadata,
    StaticRegistrationPolicy,
    TargetPolicy,
)
from app.register.preflight import RegistrationPreflightService
from app.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    DuplicateEvidence,
    DuplicateVerdict,
    FieldValue,
    ListingValues,
    PreflightRequest,
    PreflightResult,
    PreparedAsset,
    UnitRequest,
)
from app.register.store import RegistrationStore
from tests.collect_support import confirmed
from tests.product_support import Collections, context, product, raw

MARKET = "market_a"
OPERATOR = "operator-1"
CID = "cid-register"
AT = "2026-09-20 00:00:00"
TAXONOMY = "taxonomy-test-1"
CATEGORY = "category-test-1"


def bind(config: AppConfig, marketplace_key: str, uid: str | None) -> None:
    """The committed M2 binding unit of ``marketplace_key`` (ACCOUNT_IDENTITY §5), a later explicit
    rebinding to another invented identity, or (``None``) a connection that was never bound."""
    bound = (1, 1, AT, OPERATOR) if uid is not None else (None, None, None, None)
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, ?, NULL, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (marketplace_key) DO UPDATE SET"
            " provider_account_uid = excluded.provider_account_uid",
            (marketplace_key, uid, *bound, AT, AT),
        )
        connection.commit()


def establish(container: Container, config: AppConfig, marketplace_key: str, uid: str) -> str:
    bind(config, marketplace_key, uid)
    seller = container.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID)
    return container.accounts.establish(
        marketplace_key, seller, established_by=OPERATOR, correlation_id=CID
    ).marketplace_account_id


class FakeCapability:
    """A fixed CONNECT capability read model (the port the preflight reads)."""

    def __init__(
        self,
        auth: AuthStatus = AuthStatus.READY,
        write_scope: WriteScopeStatus = WriteScopeStatus.UNKNOWN,
        overlays: tuple[tuple[WorkflowScope, WorkflowState], ...] = (),
        updated_at: datetime | None = None,
    ) -> None:
        self.auth = auth
        self.write_scope = write_scope
        self.overlays = overlays
        # What the capability owner last recorded. An audited operator resolution or a fresh
        # authentication moves it, which is how an execution scope resumes (M5 PR-E).
        self.updated_at = updated_at

    def capability(self, marketplace_key: str) -> MarketplaceCapabilityView:
        return MarketplaceCapabilityView(
            marketplace_key=marketplace_key,
            auth=self.auth,
            auth_verified_at=None,
            write_scope=WriteScopeView(
                status=self.write_scope, evidence_strength=None, evidence_grade=None
            ),
            write=WriteView(status=WriteStatus.UNVERIFIED),
            contract_freshness=ContractFreshness.CURRENT,
            contract_freshness_recorded_at=None,
            workflow=[
                WorkflowOverlayView(
                    workflow_state=state, workflow_scope=scope, reason_code=None, resolution=None
                )
                for scope, state in self.overlays
            ],
            error_class=None,
            remote_outcome=None,
            updated_at=self.updated_at,
        )


def metadata(**overrides: Any) -> CategoryMetadata:
    values: dict[str, Any] = {
        "taxonomy_revision": TAXONOMY,
        "category_id": CATEGORY,
        "metadata_revision": "metadata-test-1",
        "reviewed": True,
        "attributes": (FieldRule("brand", required=True), FieldRule("color", required=False)),
        "notice": NoticePolicy(
            "notice-test-1",
            (
                FieldRule("manufacturer", required=True),
                FieldRule("origin", required=True, detail_page_reference_allowed=True),
            ),
        ),
        "options": OptionPolicy(options_supported=True, max_options=5),
        "required_templates": frozenset({"shipping", "returns"}),
    }
    values.update(overrides)
    return CategoryMetadata(**values)


@dataclass
class Preparation:
    """A preflight service over the real owners, with its three replaceable sources."""

    service: RegistrationPreflightService
    capability: FakeCapability
    metadata: StaticRegistrationMetadata
    policies: StaticRegistrationPolicy


def preparation(container: Container, account: str) -> Preparation:
    capability = FakeCapability()
    entries = StaticRegistrationMetadata((metadata(),))
    policies = StaticRegistrationPolicy((target(account),))
    service = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=capability,
        metadata=entries,
        policies=policies,
    )
    return Preparation(service, capability, entries, policies)


def target(account: str, **overrides: Any) -> TargetPolicy:
    values: dict[str, Any] = {
        "marketplace_key": MARKET,
        "marketplace_account_id": account,
        "policy_revision": "policy-test-1",
        "taxonomy_revision": TAXONOMY,
        "pricing_context": context(),
        "sanitizer_profile_version": "sanitizer-test-1",
        "asset_policy": AssetPolicy(profile="asset-profile-test-1"),
        "templates": {"shipping": "shipping-template-test", "returns": "returns-template-test"},
    }
    values.update(overrides)
    return TargetPolicy(**values)


@dataclass(frozen=True)
class ReadyItem:
    """An M4 Item that is base- and pricing-READY for ``context()``."""

    item_id: str
    group: str
    pricing_snapshot_id: str
    revision: StoredRevision


def select_and_pass(container: Container, item_id: str, revision: StoredRevision) -> None:
    """The operator's image selection and a PASS for its exact binary (M4 image owner)."""
    image = revision.images[0]
    container.images.record_operator_selection(
        item_id,
        source_revision_id=revision.revision_id,
        decisions=[
            SourceDecision(image.role, image.ordinal, image.sha256, SourceDecisionKind.USE_SOURCE)
        ],
        outputs=[SelectedOutput(ImageRole.REPRESENTATIVE, image.role, image.ordinal)],
        decided_by=OPERATOR,
    )
    selection = container.images.current_selection(item_id)
    assert selection is not None
    for output in selection.outputs:
        container.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=selection.source_revision_id,
            verdict=QaVerdict.PASS,
            decided_by="qa-1",
        )


def ready_item(
    container: Container,
    sources: Collections,
    source_product_id: str = "1234",
    fields: dict[str, FieldFact] | None = None,
    pricing: PricingContextInput | None = None,
) -> ReadyItem:
    run_id, revision = sources.collect(fields or product(), source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None and result.product_group_id is not None, result
    select_and_pass(container, result.item_id, revision)
    snapshot = container.pricing.price(result.item_id, pricing or context()).snapshot
    assert snapshot is not None
    return ReadyItem(
        result.item_id, result.product_group_id, snapshot.pricing_snapshot_id, revision
    )


def ready_tiered(
    container: Container, sources: Collections, source_product_id: str
) -> dict[int, ReadyItem]:
    """One source product with CONFIRMED quantity tiers: one READY Item per quantity (PR-Q)."""
    tiers = confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=t, label=f"tier-{q}")
                for q, t in ((1, 19900), (2, 37900), (3, 53900))
            )
        ),
        ".tiers",
    )
    run_id, revision = sources.collect(
        product(quantity_tiers=tiers), source_product_id=source_product_id
    )
    result = container.materializer.materialize_run(run_id)
    group = str(result.product_group_id)
    ready = {}
    for entry in container.products.product(group).items:
        select_and_pass(container, entry.item_id, revision)
        snapshot = container.pricing.price(entry.item_id, context()).snapshot
        assert snapshot is not None
        ready[entry.composition.quantity] = ReadyItem(
            entry.item_id, group, snapshot.pricing_snapshot_id, revision
        )
    return ready


def draft(
    store: RegistrationStore,
    account: str,
    items: list[ReadyItem],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
) -> str:
    with store.transaction() as unit:
        created = unit.create_draft(MARKET, account, shape, created_by=OPERATOR, correlation_id=CID)
        for item in items:
            unit.add_draft_item(
                created.draft_id,
                item.item_id,
                item.pricing_snapshot_id,
                added_by=OPERATOR,
                correlation_id=CID,
            )
        return created.draft_id


def listing(items: list[ReadyItem], **overrides: Any) -> ListingValues:
    values: dict[str, Any] = {
        "name": FieldValue("invented listing name"),
        "tags": frozenset({"invented-tag"}),
        "attributes": {"brand": FieldValue("invented brand")},
        "notices": {
            "manufacturer": FieldValue("invented maker", Provenance.SOURCE_FACT),
            "origin": FieldValue(detail_page_reference=True),
        },
        "options": {}
        if len(items) <= 1
        else {item.item_id: {"size": f"size-{n}"} for n, item in enumerate(items)},
    }
    values.update(overrides)
    return ListingValues(**values)


def request(
    store: RegistrationStore,
    draft_id: str,
    account: str,
    items: list[ReadyItem],
    **overrides: Any,
) -> PreflightRequest:
    current = store.draft(draft_id)
    assert current is not None
    values: dict[str, Any] = {
        "unit": UnitRequest(draft_id, current.draft_revision),
        "category": CategorySelection(
            CATEGORY, "mapping-test-1", TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        "listing": listing(items),
        "detail": DetailComposition("detail-test-1", "invented body text"),
        "duplicate_evidence": None,
    }
    values.update(overrides)
    return PreflightRequest(**values)


def no_match(result: PreflightResult) -> DuplicateEvidence:
    """Provider lookup evidence of no match for exactly the unit the preflight resolved."""
    unit = result.resolved
    return DuplicateEvidence(
        marketplace_key=unit.marketplace_key,
        marketplace_account_id=unit.marketplace_account_id,
        listing_identity=unit.listing_identity,
        lookup_contract_version="lookup-test-1",
        evidence_digest="e" * 64,
        verdict=DuplicateVerdict.NO_MATCH,
        keys_checked=frozenset({DuplicateKeyKind.SELLER_CODE}),
    )


def ready_final(
    prep: Preparation, req: PreflightRequest
) -> tuple[PreflightRequest, PreflightResult]:
    """The path of one unit to a final READY: the candidate names the listing identity, the
    provider lookup (invented) finds no match for it, the candidate is READY, the provider assets
    are prepared under its fingerprint (invented), and the final preflight is READY."""
    first = prep.service.candidate(req)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep.service.candidate(req)
    assert candidate.status is ReadinessStatus.READY, candidate.reasons
    assert candidate.upload_permitted
    final = prep.service.final(req, prepared(candidate))
    assert final.status is ReadinessStatus.READY, final.reasons
    return req, final


def prepared(result: PreflightResult, **overrides: str) -> tuple[PreparedAsset, ...]:
    """Provider assets as PR-D would prepare them for exactly this candidate (invented refs): one
    per exact local artifact, however many Items of the unit use it."""
    images = {image.key: image for item in result.resolved.items for image in item.images}
    return tuple(
        PreparedAsset(
            asset_kind=image.asset_kind,
            sha256=image.sha256,
            derivation_id=image.derivation_id,
            asset_profile=overrides.get("asset_profile", "asset-profile-test-1"),
            candidate_fingerprint=overrides.get("fingerprint", result.candidate_fingerprint),
            provider_asset_ref=overrides.get("ref", f"provider-asset-{image.sha256[:12]}"),
        )
        for _key, image in sorted(images.items())
    )
