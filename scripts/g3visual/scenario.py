"""The populated first-vertical scenario of the visual acceptance (ADR-0018 §9; Gate 3 area 3).

Everything here is invented and provider-zero. The state is written through the application's own
owners and routes, exactly as production does, over a dedicated root the run owns:

- **수집관리**: two durably RECORDED collection runs — one whose facts are CONFIRMED, one whose
  shipping fact is REVIEW_REQUIRED, so a COLLECT review item exists;
- **통합DB**: their Products and Items, the first with its image selection, a QA PASS and a price;
- **Settings**: the account's durable target policy (G1-A) and one reviewed category's metadata
  (G1-B);
- **등록관리**: a Draft of the first Item, priced under that policy, and its authored preparation —
  its candidate preflight reports what the durable policy still lacks, so a REGISTER review item
  exists; the account's CREATE execution scope PAUSED for AUTH (§26); the protected-write brake
  ENGAGED; an evidence-retention proof;
- **dashboard / 품절**: the review counts the producers derive from all of it.

**Declared seams**, each written by its owner's own store and named in the report:
- ``connect_binding``: the committed M2 account binding — a real SmartStore call produces it, which
  this run may not make (as in the M5 harness);
- ``live_grants``: an ACTIVE and a REVOKED ASSET grant of the preparation's current revision. The
  grant service issues one only for a READY candidate, and under a durable policy no candidate can
  be READY at this main (its authoring revisions have no owner, decision 5800619183), so the rows
  are recorded directly by the LIVE owner's store. They are display state for this acceptance only;
  every readiness they show is still the server's own derivation, and it is BLOCKED.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final

from fastapi.testclient import TestClient

from app.collect.facts import (
    CollectedFacts,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    ImageReference,
    ImageRole,
)
from app.container import Container
from app.core.errors import ErrorClass
from app.live.store import ArtifactRef, LiveAuthorityStore
from app.register.execution import CREATE_ENDPOINT_GROUP
from app.register.model import ListingShape, ScopePauseReason
from scripts.m4accept.synthetic import fields, png
from scripts.m5accept import synthetic

MARKET: Final = "smartstore"
OPERATOR: Final = "g3-visual-operator"
CID: Final = "g3-visual-scenario"
CLIENT: Final = {"X-ICBM-Client": "icbm-g3-visual"}
TAXONOMY: Final = "g3-visual-taxonomy-1"
CATEGORY: Final = "g3-visual-category-1"
REVIEW_REF: Final = "5843380581"
DECLARED_SEAMS: Final = ("connect_binding", "live_grants")


@dataclass(frozen=True)
class Populated:
    account: str
    product: str
    draft_id: str
    preparation_id: str
    confirmed_run: str
    review_run: str

    def routes(self) -> dict[str, str]:
        """The route of every required surface, named by ``app.live.visual.REQUIRED_TARGETS``."""
        return {
            "collect": f"#/collect?run={self.confirmed_run}",
            "db": f"#/db?product={self.product}",
            "register": f"#/register?draft={self.draft_id}",
            "dashboard": "#/dashboard",
            "soldout": "#/soldout",
            "settings-policy": "#/settings?tab=smartstore&sub=policy",
            "settings-metadata": "#/settings?tab=smartstore&sub=product",
        }


def _owners(container: Container) -> Any:
    """The owners the M5 synthetic helpers write through, taken from the composed application."""
    return SimpleNamespace(
        db=container.db,
        clock=container.clock,
        runs=container.collection._runs,
        revisions=container.revisions,
        source_assets=container.source_assets,
        materializer=container.materializer,
        images=container.images,
        pricing=container.pricing,
        accounts=container.accounts,
        capability=container.marketplace_capability,
        registrations=container.registrations,
        database_file=container.config.database_path,
    )


def _record(owners: Any, product: str, *, sequence: int, shipping_needs_review: bool) -> Any:
    """Append one durably RECORDED revision through COLLECT's own stores (as the M5 helper does),
    optionally with its shipping fact REVIEW_REQUIRED."""
    found = fields(synthetic.BASE_FACTS)
    if shipping_needs_review:
        evidence = (
            Evidence(
                kind=EvidenceKind.DOM_TEXT,
                locator=".m4-delivery",
                status=FieldStatus.REVIEW_REQUIRED,
            ),
        )
        found["shipping"] = FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)
    stored = owners.source_assets.put(png(f"{product}-{sequence}"))
    source_url = synthetic.SOURCE_URL.format(product=product)
    correlation = f"{CID}-{product}"
    with owners.db.write() as session:
        run_id = owners.runs.open(
            session,
            job_id=str(uuid.uuid4()),
            correlation_id=correlation,
            supplier_key=synthetic.SUPPLIER,
            source_url=source_url,
        )
    owners.runs.note_identity(run_id, source_product_id=product)
    revision = owners.revisions.append(
        CollectedFacts(
            supplier_key=synthetic.SUPPLIER,
            source_product_id=product,
            source_url=source_url,
            captured_at=synthetic.CAPTURED + timedelta(hours=sequence),
            extractor_revision=synthetic.EXTRACTOR_REVISION,
            extractor_fingerprint=synthetic.EXTRACTOR_FINGERPRINT,
            collection_run_id=run_id,
            correlation_id=correlation,
            fields=found,
            images=(
                ImageReference(
                    role=ImageRole.REPRESENTATIVE,
                    ordinal=0,
                    host=synthetic.IMAGE_HOST,
                    provenance=".m5-representative img:nth-of-type(1)",
                    status=FieldStatus.CONFIRMED,
                    sha256=stored.sha256,
                ),
            ),
        )
    )
    owners.runs.recorded(
        run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
    )
    return run_id, revision


def _rule(key: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": key,
        "required": False,
        "detail_page_reference_allowed": False,
        "missing_status": "REVIEW_REQUIRED",
        "max_length": None,
    }
    values.update(overrides)
    return values


def _post(api: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = api.post(path, json=body, headers=CLIENT)
    if response.status_code != 200:
        raise RuntimeError(f"scenario step refused: {path} {response.status_code}")
    found: dict[str, Any] = response.json()
    return found


def _policy(api: TestClient, account: str) -> None:
    _post(
        api,
        f"/api/v1/settings/target-policies/{MARKET}/{account}/revisions",
        {
            "actor": OPERATOR,
            "expected_current_revision": None,
            "inputs": {
                "taxonomy_revision": TAXONOMY,
                "pricing_context": {
                    "marketplace_key": MARKET,
                    "account_id": None,
                    "fee_table_version": "g3-visual-fee-1",
                    "pricing_policy_version": "g3-visual-pricing-1",
                    "fee_rate": "0.1",
                    "fee_fixed_krw": 0,
                    "other_cost_rate": "0",
                    "other_cost_fixed_krw": 0,
                    "cost_rounding": "CEIL_KRW_1",
                    "price_rounding": "CEIL_KRW_1",
                },
                "sanitizer_profile_version": "g3-visual-sanitizer-1",
                "asset_policy": {
                    "profile": "g3-visual-asset-profile-1",
                    "min_images": 1,
                    "max_images": 10,
                    "requires_representative": True,
                    "provider_asset_identity_required": True,
                },
                "templates": {"shipping": "g3-visual-shipping", "returns": "g3-visual-returns"},
                "duplicate_proof_required": True,
                "duplicate_lookup_keys": ["SELLER_CODE"],
                "category_mapping_revision": None,
                "detail_composition_revision": None,
            },
        },
    )


def _metadata(api: TestClient) -> None:
    _post(
        api,
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}/revisions",
        {
            "actor": OPERATOR,
            "expected_current_revision": None,
            "content_provenance": "OPERATOR_CONFIRMED",
            "evidence_reference": "g3-visual/category-1/invented",
            "reviewed": True,
            "content": {
                "leaf": True,
                "registrable": True,
                "name_max_length": 100,
                "attributes": [_rule("brand", required=True), _rule("color")],
                "notice": {
                    "notice_type": "g3-visual-notice-1",
                    "fields": [
                        _rule("manufacturer", required=True),
                        _rule("origin", required=True, detail_page_reference_allowed=True),
                    ],
                },
                "options": {"options_supported": True, "max_options": 5, "max_dimensions": 1},
                "required_templates": ["returns", "shipping"],
            },
        },
    )


def _grants(container: Container, account: str, preparation_id: str) -> None:
    """The declared ``live_grants`` seam: one ACTIVE and one REVOKED ASSET grant."""
    candidate = container.registration_preparations.evaluate(preparation_id)
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    artifacts = [
        ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
        for item in candidate.resolved.items
        for image in item.images
    ]
    store = LiveAuthorityStore(container.db, container.clock, container.audit)
    now = container.clock.now()
    issued = []
    for budget in (2, 1):
        with store.transaction() as unit:
            issued.append(
                unit.issue_asset_grant(
                    marketplace_key=MARKET,
                    marketplace_account_id=account,
                    preparation_revision_id=preparation.current.preparation_revision_id,
                    candidate_fingerprint=candidate.candidate_fingerprint or "0" * 64,
                    artifacts=artifacts,
                    asset_profile=candidate.resolved.target.asset_policy.profile,
                    budget=budget,
                    not_before=now,
                    expires_at=now + timedelta(hours=1),
                    approved_by=OPERATOR,
                    authorization_ref=REVIEW_REF,
                    correlation_id=CID,
                )
            )
    container.live_authority.revoke(
        issued[1].grant_id, actor=OPERATOR, reason_code="G3_VISUAL_REVOKED", correlation_id=CID
    )


def populate(container: Container, api: TestClient) -> Populated:
    """Write the whole scenario through the owners of the application ``api`` serves."""
    owners = _owners(container)
    account = synthetic.bind_account(owners, uid="g3-visual-uid-1")
    synthetic.authenticate(owners, uid="g3-visual-uid-1")
    _policy(api, account)
    _metadata(api)
    confirmed_run, confirmed = _record(
        owners, "g3-visual-product-1", sequence=0, shipping_needs_review=False
    )
    materialized = container.materializer.materialize_run(confirmed_run)
    if materialized.item_id is None or materialized.product_group_id is None:
        raise RuntimeError("scenario step refused: materialize the first product")
    synthetic.select_and_pass(owners, materialized.item_id, confirmed)
    review_run, _ = _record(owners, "g3-visual-product-2", sequence=1, shipping_needs_review=True)
    container.materializer.materialize_run(review_run)
    policy = container.registration_preflight.target_policy(MARKET, account)
    if policy is None:
        raise RuntimeError("scenario step refused: the durable target policy")
    pin = container.pricing.price(materialized.item_id, policy.pricing_context).snapshot
    if pin is None:
        raise RuntimeError("scenario step refused: price the first Item")
    with container.registrations.transaction() as unit:
        draft = unit.create_draft(
            MARKET,
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        unit.add_draft_item(
            draft.draft_id,
            materialized.item_id,
            pin.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    prepared = _post(
        api,
        "/api/v1/register/preparations",
        {
            "draft_id": draft.draft_id,
            "item_ids": [materialized.item_id],
            "actor": OPERATOR,
            "inputs": {
                "category": {
                    "category_id": CATEGORY,
                    "mapping_revision": None,
                    "taxonomy_revision": TAXONOMY,
                    "confirmation": "OPERATOR_CONFIRMED",
                },
                "name": {"value": "invented listing", "provenance": "OPERATOR_CONFIRMED"},
                "tags": [],
                "attributes": {"brand": {"value": "invented brand"}},
                "notices": {
                    "manufacturer": {"value": "invented maker"},
                    "origin": {"detail_page_reference": True},
                },
                "options": {},
                "detail_composition_revision": None,
                "detail_body": "invented body text",
                "detail_sections": ["BODY"],
            },
        },
    )
    preparation_id = str(prepared["preparation_id"])
    with container.registrations.transaction() as unit:
        unit.pause_scope(
            MARKET,
            account,
            CREATE_ENDPOINT_GROUP,
            reason=ScopePauseReason.AUTH,
            policy_version="registration-execution-policy/v1",
            error_class=ErrorClass.AUTH,
            actor=OPERATOR,
            correlation_id=CID,
        )
    _grants(container, account, preparation_id)
    container.live_authority.engage_brake(
        actor=OPERATOR, reason_code="G3_VISUAL_SCENARIO", correlation_id=CID
    )
    container.retention.prove(actor=OPERATOR, correlation_id=CID)
    return Populated(
        account=account,
        product=materialized.product_group_id,
        draft_id=draft.draft_id,
        preparation_id=preparation_id,
        confirmed_run=confirmed_run,
        review_run=review_run,
    )


__all__ = ["DECLARED_SEAMS", "Populated", "populate"]
