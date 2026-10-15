"""ADR-0042 S3b: five paths of the duplicate check that must fail closed (PR #370 audit).

Synthetic data and fakes only; no provider is reached.

1. A **similar** listing name (the S2a rule: ≥ 60 % shared words, at least two) is a weak match —
   REVIEW_REQUIRED, the seller decides — never NO_MATCH; the seller's 「다른 상품」 dismisses it.
2. A COMPLETE snapshot is NO_MATCH evidence only after the seller confirmed the store's
   per-status counts (coverage); a later sync that loses a status class or jumps in total
   invalidates the confirmation. A match never needs it.
3. GTIN is claimed in ``keys_checked`` only when every listing's details were read since the
   snapshot's start; otherwise a policy requiring GTIN stays INCOMPLETE. A GTIN match is strong.
4. A CREATE send with no duplicate revalidation wired refuses and sends nothing
   (``test_s3b_send_time_duplicates``).
5. The first CREATE copy's positive spot check has no default, and both queue paths run it.
"""

import inspect
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.operate.catalog.coverage import (
    CONFIRMED,
    INVALIDATED,
    STATUS_CLASS_MISSING,
    TOTAL_JUMPED,
    UNCONFIRMED,
    CoverageConflict,
)
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring import RegistrationPreparationService
from app.stages.register.policy import DuplicateKeyKind
from app.stages.register.preparation import (
    DUPLICATE_EVIDENCE_INCOMPLETE,
    DUPLICATE_EVIDENCE_UNAVAILABLE,
    PROVIDER_DUPLICATE_FOUND,
    PROVIDER_DUPLICATE_WEAK_SIGNAL,
    DuplicateVerdict,
)
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    api,
    container,
)
from tests.integration.register.test_s3b_catalog_duplicate_source import (
    CID,
    FakeSpot,
    Store,
    _codes,
    _freeze,
    _row,
    authored,
)
from tests.support.gate1_support import CLIENT
from tests.support.register_support import OPERATOR

pytestmark = pytest.mark.integration

# A valid UPC-A and the same product as an EAN-13.
UPC_A = "036000291452"
EAN_13 = "0036000291452"


# ---------------------------------------------------------------- 1. similar names


def test_a_similar_name_is_a_weak_match_never_no_match(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    decided: set[tuple[str, str]] = set()
    unit = authored(api, container, config)
    store = Store(container, unit["account"], different=lambda key, account: set(decided))
    # The unit is named "authored listing name": three of four words in common, not equal.
    store.source.put("701", "Authored listing name 대용량", "OTHER-CODE-1")
    # One word in common is no signal at all.
    store.source.put("702", "listing 전혀 다른 물건", "OTHER-CODE-2")
    store.sync()
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_WEAK_SIGNAL in codes
    assert result.status is ReadinessStatus.REVIEW_REQUIRED
    evidence = result.request.duplicate_evidence
    assert evidence is not None and evidence.verdict is DuplicateVerdict.MATCH
    assert [m.provider_listing_ref for m in evidence.matches] == ["701"]
    row = _row(api, unit)["duplicate"]
    assert row["state"] == "MATCH"
    assert row["matches"][0]["key_kind"] == "NORMALIZED_NAME"
    # 「다른 상품」 for another product dismisses nothing; for this unit's product it does.
    decided.add(("701", "another-product"))
    assert PROVIDER_DUPLICATE_WEAK_SIGNAL in _codes(container, unit)[1]
    group = unit["item"].group
    decided.add(("701", group))
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_WEAK_SIGNAL not in codes
    assert result.status is ReadinessStatus.READY, codes


def test_dismissing_a_listing_by_its_product_number_reaches_the_links_owner(
    api: TestClient,  # noqa: F811
) -> None:
    # 「다른 상품」 from the duplicate row names the listing by store and product number.
    answer = api.post(
        "/api/v1/operate/store-links/reject",
        json={
            "marketplace_key": "smartstore",
            "marketplace_account_id": "mpa-none",
            "origin_product_no": "999",
            "product_group_id": "g-1",
            "actor": OPERATOR,
        },
        headers=CLIENT,
    )
    assert answer.status_code == 404
    assert answer.json()["error"]["code"] == "STORE_LINK_LISTING_UNKNOWN"
    missing = api.post(
        "/api/v1/operate/store-links/reject",
        json={"product_group_id": "g-1", "actor": OPERATOR},
        headers=CLIENT,
    )
    assert missing.status_code == 422


# ---------------------------------------------------------------- 2. coverage


def test_no_match_waits_for_the_sellers_coverage_confirmation(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"], confirm_coverage=False)
    store.source.put("801", "판매중 상품", "CODE-801")
    store.source.put("802", "품절 상품", "CODE-802")
    store.source.statuses["802"] = "OUTOFSTOCK"
    run = store.sync()
    result, codes = _codes(container, unit)
    assert DUPLICATE_EVIDENCE_UNAVAILABLE in codes and result.status is not ReadinessStatus.READY
    assert _row(api, unit)["duplicate"]["state"] == "COVERAGE_UNCONFIRMED"
    view = store.coverage.view()
    assert view.state == UNCONFIRMED and view.current is not None
    assert dict(view.current.counts) == {"ON_SALE": 1, "OUT_OF_STOCK": 1}
    # Only the sync the seller looked at can be confirmed.
    with pytest.raises(CoverageConflict):
        store.coverage.confirm(run_id="another-run", actor=OPERATOR, correlation_id=CID)
    store.coverage.confirm(run_id=run.run_id, actor=OPERATOR, correlation_id=CID)
    event = container.audit.latest(
        AuditEventType.STORE_CATALOG_COVERAGE_CONFIRMED.value,
        f"store-catalog:market_a:{unit['account']}",
    )
    assert event is not None and event.actor == OPERATOR
    assert event.details["run_id"] == run.run_id and event.details["total"] == 2
    assert event.details["counts"] == {"ON_SALE": 1, "OUT_OF_STOCK": 1}
    result, codes = _codes(container, unit)
    assert result.status is ReadinessStatus.READY, codes
    assert _row(api, unit)["duplicate"]["state"] == "NO_MATCH"
    # A status class the seller confirmed is gone: the confirmation no longer holds.
    store.source.statuses["802"] = "SALE"
    store.clock.advance(5)
    store.sync(trigger="OPERATOR")  # 「지금 동기화」 re-reads every listing
    assert store.coverage.view().state == INVALIDATED
    assert store.coverage.view().reason == STATUS_CLASS_MISSING
    assert DUPLICATE_EVIDENCE_UNAVAILABLE in _codes(container, unit)[1]


def test_an_unexpected_jump_in_the_total_asks_the_seller_again(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    store.source.put("811", "첫 상품", "CODE-811")
    store.sync()
    assert store.coverage.view().state == CONFIRMED
    for n in range(12):
        store.source.put(f"9{n:02d}", f"새 상품 {n}", f"CODE-9{n:02d}")
    store.clock.advance(5)
    store.catalog.sync(trigger="OPERATOR", correlation_id=CID)
    view = store.coverage.view()
    assert (view.state, view.reason) == (INVALIDATED, TOTAL_JUMPED)
    assert _row(api, unit)["duplicate"]["state"] == "COVERAGE_UNCONFIRMED"


def test_a_match_needs_no_coverage_confirmation(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    from integrations.marketplaces.smartstore.product import seller_management_code

    unit = authored(api, container, config)
    store = Store(container, unit["account"], confirm_coverage=False)
    identity = container.registration_preparations.evaluate(
        unit["preparation_id"]
    ).resolved.listing_identity
    store.source.put("821", "store product", seller_management_code(identity))
    store.sync()
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_FOUND in codes and result.status is ReadinessStatus.DUPLICATE


def test_the_coverage_routes_refuse_a_store_that_is_not_connected(
    api: TestClient,  # noqa: F811
) -> None:
    view = api.get("/api/v1/operate/store-catalog/coverage", headers=CLIENT)
    assert view.status_code == 200 and view.json()["state"] == "NO_SNAPSHOT"
    refused = api.post(
        "/api/v1/operate/store-catalog/coverage/confirm",
        json={"run_id": "r-1", "actor": OPERATOR},
        headers=CLIENT,
    )
    assert refused.status_code >= 400
    assert refused.json()["error"]["code"] == "STORE_CATALOG_STORE_NOT_CONNECTED"


# ---------------------------------------------------------------- 3. GTIN freshness


def test_gtin_is_claimed_only_on_details_read_since_the_snapshot_start(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    requires = frozenset({DuplicateKeyKind.SELLER_CODE, DuplicateKeyKind.GTIN})
    unit = authored(api, container, config, duplicate_lookup_keys=requires)
    store = Store(container, unit["account"], member_gtins=lambda groups: {UPC_A})
    for origin in ("831", "832", "833"):
        store.source.put(origin, f"다른 상품 {origin}", f"CODE-{origin}")
    store.sync()  # every listing is new, so every one is read by this sync
    result, codes = _codes(container, unit)
    evidence = result.request.duplicate_evidence
    assert evidence is not None and DuplicateKeyKind.GTIN in evidence.keys_checked
    assert result.status is ReadinessStatus.READY, codes
    # A periodic sync re-reads only its rolling budget: older details could hide a barcode.
    store.clock.advance(1800)
    store.sync(trigger="AUTO")
    result, codes = _codes(container, unit)
    evidence = result.request.duplicate_evidence
    assert evidence is not None and DuplicateKeyKind.GTIN not in evidence.keys_checked
    assert DUPLICATE_EVIDENCE_INCOMPLETE in codes


def test_a_listing_with_a_members_gtin_is_a_strong_match(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"], member_gtins=lambda groups: {UPC_A})
    store.source.put("841", "전혀 다른 이름", "SOMETHING-ELSE")
    store.source.barcodes["841"] = EAN_13
    store.sync()
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_FOUND in codes and result.status is ReadinessStatus.DUPLICATE
    assert _row(api, unit)["duplicate"]["matches"][0]["key_kind"] == "GTIN"


# ---------------------------------------------------------------- 5. the spot check


def test_the_first_create_copy_must_state_its_spot_check() -> None:
    parameter = inspect.signature(RegistrationPreparationService.execution_copy).parameters[
        "spot_check"
    ]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_both_queue_paths_run_the_spot_check(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    from tests.integration.register.test_s3b_catalog_duplicate_source import authored_again

    unit = authored(api, container, config)
    spot = FakeSpot({"status": "COMPLETE", "candidates": []})
    store = Store(container, unit["account"], spot=spot)
    store.sync()
    single = _freeze(container, unit)
    bulk = _freeze(container, authored_again(api, container, config, unit))
    authoring = container.registration_preparations
    original = authoring.execution_copy
    seen: list[tuple[str, bool]] = []

    def recording(snapshot_id: str, *, spot_check: bool) -> Any:
        seen.append((snapshot_id, spot_check))
        # Stop after the copy: nothing is queued, so no job runs behind the test.
        original(snapshot_id, spot_check=spot_check)
        raise AppError("TEST_STOP", "the copy was made; nothing is queued")

    authoring.execution_copy = recording  # type: ignore[method-assign]
    try:
        with pytest.raises(AppError):
            container.register.enqueue_create(single.intent.intent_id)
        with pytest.raises(AppError):
            container.register.enqueue_bulk_create([bulk.intent.intent_id], correlation_id=CID)
    finally:
        authoring.execution_copy = original  # type: ignore[method-assign]
    assert seen == [
        (single.snapshot.registration_snapshot_id, True),
        (bulk.snapshot.registration_snapshot_id, True),
    ]
    assert spot.calls == [single.snapshot.listing_identity, bulk.snapshot.listing_identity]
