"""Synthetic inputs for the M5 acceptance run (Issue #89 PR-F §A).

Everything here is invented: the supplier key, product identities, names, amounts, image bytes,
the category and policy revisions, and the provider asset and duplicate-lookup evidence. No real
supplier page, URL, marketplace account, price or business datum is used, and none of it enters
the report.

Source truth is written only through COLLECT's accepted owners and the M4 owners above them, and
the registration rows only through the M5 store — exactly as production does.

**One row has no offline production path**: the committed M2 account binding is produced by a real
SmartStore call, which this run may not make. It is therefore written directly, as the run's one
declared synthetic CONNECT fact, and the report says so (`synthetic_connect_binding`). Everything
scoped by it — Drafts, Snapshots, Intents, Attempts, registrations — is written by its owner.
"""

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.collect.facts import (
    CollectedFacts,
    FieldFact,
    ImageReference,
    ImageRole,
)
from app.collect.facts import FieldStatus as CollectFieldStatus
from app.collect.revisions import StoredRevision
from app.connect.marketplace.capability import (
    AuthEvidence,
    EvidenceStrength,
    Generations,
    IdentityProof,
    WriteScope,
    WriteScopeStatus,
)
from app.products.image_model import ImageRole as ProductImageRole
from app.products.image_model import QaVerdict, SelectedOutput, SourceDecision, SourceDecisionKind
from app.products.model import ReadinessStatus
from app.products.pricing import PricingContextInput, Rounding
from app.register.model import ListingShape
from app.register.policy import (
    AssetPolicy,
    CategoryMetadata,
    DuplicateKeyKind,
    FieldRule,
    NoticePolicy,
    OptionPolicy,
    StaticRegistrationMetadata,
    StaticRegistrationPolicy,
    TargetPolicy,
)
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
    Provenance,
    UnitRequest,
)
from scripts.m4accept.synthetic import Facts, fields, png
from scripts.m5accept.owners import MARKETPLACE, Owners

SUPPLIER = "m5-synthetic"
SOURCE_URL = "https://m5-acceptance.example/products/{product}"
IMAGE_HOST = "img.m5-acceptance.example"
EXTRACTOR_REVISION = "m5-acceptance-extractor-v1"
EXTRACTOR_FINGERPRINT = "5" * 64
CAPTURED = datetime(2026, 9, 20, tzinfo=UTC)
AT = "2026-09-20 00:00:00"

OPERATOR = "m5-acceptance-operator"
QA = "m5-acceptance-qa"
CID = "m5-acceptance"
TAXONOMY = "m5-acceptance-taxonomy-1"
CATEGORY = "m5-acceptance-category-1"
POLICY_REVISION = "m5-acceptance-policy-1"
ASSET_PROFILE = "m5-acceptance-asset-profile-1"
LOOKUP_VERSION = "m5-acceptance-lookup-1"
CONTEXT = PricingContextInput(
    marketplace_key=MARKETPLACE,
    account_id=None,
    fee_table_version="m5-acceptance-fee-1",
    pricing_policy_version="m5-acceptance-pricing-1",
    fee_rate="0.1",
    fee_fixed_krw=0,
    other_cost_rate="0",
    other_cost_fixed_krw=0,
    cost_rounding=Rounding.CEIL_KRW_1,
    price_rounding=Rounding.CEIL_KRW_1,
)
BASE_FACTS = Facts(prices=(19900,), shipping_fee=3000, minimum=None, tiers=None)


# ---------------------------------------------------------------- source truth and M4 Items


@dataclass(frozen=True)
class Recorded:
    run_id: str
    revision: StoredRevision


@dataclass(frozen=True)
class ReadyItem:
    """An M4 Item that is base- and pricing-READY in this run's pricing context."""

    item_id: str
    group: str
    pricing_snapshot_id: str
    revision: StoredRevision


def record_revision(
    owners: Owners, *, product: str, facts: Facts = BASE_FACTS, sequence: int = 0
) -> Recorded:
    """Append one durably RECORDED synthetic revision through COLLECT's own stores."""
    stored = owners.source_assets.put(png(f"{product}-{sequence}"))
    reference = ImageReference(
        role=ImageRole.REPRESENTATIVE,
        ordinal=0,
        host=IMAGE_HOST,
        provenance=".m5-representative img:nth-of-type(1)",
        status=CollectFieldStatus.CONFIRMED,
        sha256=stored.sha256,
    )
    source_url = SOURCE_URL.format(product=product)
    correlation = f"{CID}-{product}-{sequence}"
    with owners.db.write() as session:
        run_id = owners.runs.open(
            session,
            job_id=str(uuid.uuid4()),
            correlation_id=correlation,
            supplier_key=SUPPLIER,
            source_url=source_url,
        )
    owners.runs.note_identity(run_id, source_product_id=product)
    revision = owners.revisions.append(
        CollectedFacts(
            supplier_key=SUPPLIER,
            source_product_id=product,
            source_url=source_url,
            captured_at=CAPTURED + timedelta(hours=sequence),
            extractor_revision=EXTRACTOR_REVISION,
            extractor_fingerprint=EXTRACTOR_FINGERPRINT,
            collection_run_id=run_id,
            correlation_id=correlation,
            fields=_fields(facts),
            images=(reference,),
        )
    )
    owners.runs.recorded(
        run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
    )
    return Recorded(run_id, revision)


def _fields(facts: Facts) -> dict[str, FieldFact]:
    return fields(facts)


def ready_item(
    owners: Owners, *, product: str, facts: Facts = BASE_FACTS, sequence: int = 0
) -> ReadyItem:
    """Synthetic source truth carried through the real M4 owners to a READY Item."""
    recorded = record_revision(owners, product=product, facts=facts, sequence=sequence)
    result = owners.materializer.materialize_run(recorded.run_id)
    assert result.item_id is not None and result.product_group_id is not None, result
    select_and_pass(owners, result.item_id, recorded.revision)
    priced = owners.pricing.price(result.item_id, CONTEXT)
    assert priced.snapshot is not None, priced
    return ReadyItem(
        result.item_id,
        result.product_group_id,
        priced.snapshot.pricing_snapshot_id,
        recorded.revision,
    )


def select_and_pass(owners: Owners, item_id: str, revision: StoredRevision) -> None:
    """The operator's image selection and a PASS for its exact binary (M4 image owner)."""
    image = revision.images[0]
    owners.images.record_operator_selection(
        item_id,
        source_revision_id=revision.revision_id,
        decisions=[
            SourceDecision(image.role, image.ordinal, image.sha256, SourceDecisionKind.USE_SOURCE)
        ],
        outputs=[SelectedOutput(ProductImageRole.REPRESENTATIVE, image.role, image.ordinal)],
        decided_by=OPERATOR,
    )
    selection = owners.images.current_selection(item_id)
    assert selection is not None
    for output in selection.outputs:
        owners.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=selection.source_revision_id,
            verdict=QaVerdict.PASS,
            decided_by=QA,
        )


# ---------------------------------------------------------------- CONNECT scope


def bind_account(owners: Owners, uid: str = "m5-acceptance-uid-1") -> str:
    """The canonical account this run registers for.

    The committed binding row is this run's one synthetic CONNECT fact (no provider call may be
    made); the seller entity and the canonical account are then established by their own owner,
    which refuses an account whose connection does not name the same identity.
    """
    with contextlib.closing(sqlite3.connect(owners.database_file)) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, ?, NULL, 1, 1, ?, ?, ?, ?)"
            " ON CONFLICT (marketplace_key) DO UPDATE SET"
            " provider_account_uid = excluded.provider_account_uid",
            (MARKETPLACE, uid, AT, OPERATOR, AT, AT),
        )
        connection.commit()
    seller = owners.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID)
    account = owners.accounts.establish(
        MARKETPLACE, seller, established_by=OPERATOR, correlation_id=CID
    )
    return account.marketplace_account_id


def authenticate(owners: Owners, uid: str = "m5-acceptance-uid-1") -> None:
    """Typed CONNECT evidence: this account authenticated, and its write scope is attested.

    The capability owner decides what the evidence means; this hands it exactly what a real
    CONNECT run would hand it — a committed binding and one protected identity proof — with no
    provider call anywhere.
    """
    generations = Generations(credential=1, session=1)
    owners.capability.observe_auth(
        MARKETPLACE,
        AuthEvidence(
            binding_committed=True,
            expected_account_uid=uid,
            current=generations,
            proof=IdentityProof(generations, uid, owners.clock.now()),
        ),
    )
    owners.capability.observe_permission(
        MARKETPLACE,
        WriteScope(
            status=WriteScopeStatus.READY,
            evidence_strength=EvidenceStrength.OPERATOR_ATTESTED,
        ),
    )


# ---------------------------------------------------------------- registration inputs


def policy_sources(account: str) -> tuple[StaticRegistrationPolicy, StaticRegistrationMetadata]:
    """This run's target policy and category metadata: invented, reviewed, and explicit."""
    target = TargetPolicy(
        marketplace_key=MARKETPLACE,
        marketplace_account_id=account,
        policy_revision=POLICY_REVISION,
        taxonomy_revision=TAXONOMY,
        pricing_context=CONTEXT,
        sanitizer_profile_version="m5-acceptance-sanitizer-1",
        asset_policy=AssetPolicy(profile=ASSET_PROFILE),
        templates={"shipping": "m5-shipping-template", "returns": "m5-returns-template"},
    )
    metadata = CategoryMetadata(
        taxonomy_revision=TAXONOMY,
        category_id=CATEGORY,
        metadata_revision="m5-acceptance-metadata-1",
        reviewed=True,
        attributes=(FieldRule("brand", required=True), FieldRule("color", required=False)),
        notice=NoticePolicy(
            "m5-acceptance-notice-1",
            (
                FieldRule("manufacturer", required=True),
                FieldRule("origin", required=True, detail_page_reference_allowed=True),
            ),
        ),
        options=OptionPolicy(options_supported=True, max_options=5),
        required_templates=frozenset({"shipping", "returns"}),
    )
    return StaticRegistrationPolicy((target,)), StaticRegistrationMetadata((metadata,))


def draft(
    owners: Owners,
    account: str,
    items: list[ReadyItem],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
) -> str:
    with owners.registrations.transaction() as unit:
        created = unit.create_draft(
            MARKETPLACE, account, shape, created_by=OPERATOR, correlation_id=CID
        )
        for item in items:
            unit.add_draft_item(
                created.draft_id,
                item.item_id,
                item.pricing_snapshot_id,
                added_by=OPERATOR,
                correlation_id=CID,
            )
        return created.draft_id


def request(
    owners: Owners, draft_id: str, items: list[ReadyItem], **overrides: Any
) -> PreflightRequest:
    current = owners.registrations.draft(draft_id)
    assert current is not None
    values: dict[str, Any] = {
        "unit": UnitRequest(draft_id, current.draft_revision),
        "category": CategorySelection(
            CATEGORY, "m5-acceptance-mapping-1", TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        "listing": ListingValues(
            name=FieldValue("invented acceptance listing"),
            tags=frozenset({"invented-tag"}),
            attributes={"brand": FieldValue("invented brand")},
            notices={
                "manufacturer": FieldValue("invented maker", Provenance.SOURCE_FACT),
                "origin": FieldValue(detail_page_reference=True),
            },
            options=(
                {}
                if len(items) <= 1
                else {item.item_id: {"size": f"size-{n}"} for n, item in enumerate(items)}
            ),
        ),
        "detail": DetailComposition("m5-acceptance-detail-1", "invented body text"),
        "duplicate_evidence": None,
    }
    values.update(overrides)
    return PreflightRequest(**values)


def no_match(result: PreflightResult) -> DuplicateEvidence:
    """Invented provider-lookup evidence of no match, for exactly the resolved unit."""
    unit = result.resolved
    return DuplicateEvidence(
        marketplace_key=unit.marketplace_key,
        marketplace_account_id=unit.marketplace_account_id,
        listing_identity=unit.listing_identity,
        lookup_contract_version=LOOKUP_VERSION,
        evidence_digest="e" * 64,
        verdict=DuplicateVerdict.NO_MATCH,
        keys_checked=frozenset({DuplicateKeyKind.SELLER_CODE}),
    )


def prepared_assets(result: PreflightResult) -> tuple[PreparedAsset, ...]:
    """Provider assets as PR-D would prepare them for this candidate; every reference is
    invented."""
    images = {image.key: image for item in result.resolved.items for image in item.images}
    return tuple(
        PreparedAsset(
            asset_kind=image.asset_kind,
            sha256=image.sha256,
            derivation_id=image.derivation_id,
            asset_profile=ASSET_PROFILE,
            candidate_fingerprint=result.candidate_fingerprint,
            provider_asset_ref=f"provider-asset-{image.sha256[:12]}",
        )
        for _key, image in sorted(images.items())
    )


def ready_final(owners: Owners, req: PreflightRequest) -> tuple[PreflightRequest, PreflightResult]:
    """One unit's path to a final READY, exactly as the operator surface would drive it."""
    first = owners.preflight.candidate(req)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = owners.preflight.candidate(req)
    assert candidate.status is ReadinessStatus.READY, [r.code for r in candidate.reasons]
    assert candidate.upload_permitted
    final = owners.preflight.final(req, prepared_assets(candidate))
    assert final.status is ReadinessStatus.READY, [r.code for r in final.reasons]
    return req, final
