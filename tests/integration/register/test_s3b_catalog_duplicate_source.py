"""ADR-0042 S3b: the catalog duplicate source unblocks registration (§6; amends ADR-0014 §13).

The SmartStore duplicate evidence is the store's own COMPLETE catalog snapshot. Proven here, with
synthetic values and fakes only (no provider is reached):

- with no COMPLETE snapshot there is no evidence (``DUPLICATE_EVIDENCE_UNAVAILABLE``) and the
  automatic re-sync is asked for; the seller reads 「스토어 다시 확인 중…」 (``RESYNCING``);
- a COMPLETE, fresh snapshot without the unit is ``NO_MATCH``: the unit is READY, it freezes, and
  the first CREATE copy reproduces the frozen fingerprint;
- past the target policy's ``catalog_max_age`` (60 minutes, or a shorter revision) from the sync's
  **start**, or once an ICBM mutation of the store started, the snapshot is stale
  (``DUPLICATE_EVIDENCE_STALE``), never ``NO_MATCH``; a re-sync that finds the same thing keeps
  the same fingerprint (the stable digest);
- a store listing with the unit's projected seller code, or an owner-declared convention code, is
  a strong match and blocks (``DUPLICATE``); a listing with the same normalized name is a weak one
  (``REVIEW_REQUIRED``); 「의도적으로 한 번 더 등록」 records an audited, revocable
  DuplicateOverride, and only for a unit that shows a duplicate;
- the first CREATE copy's positive spot check: an exact hit the snapshot did not show refuses the
  copy; zero hits, a failed search or an error change nothing;
- a fresh ``NO_MATCH`` never frees an in-flight or UNKNOWN CREATE's conflict scope.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.operate.catalog.coverage import CatalogCoverage
from app.stages.operate.catalog.duplicates import CatalogDuplicateSource
from app.stages.operate.catalog.facts import (
    COMPLETE,
    OBSERVED,
    CatalogEntry,
    Enumeration,
    ListingFields,
    ListingRead,
)
from app.stages.operate.catalog.service import (
    CONNECT,
    PRE_REGISTRATION,
    StoreCatalogScheduler,
    StoreCatalogService,
)
from app.stages.products.model import ReadinessStatus
from app.stages.register.model import RegistrationConflictError
from app.stages.register.preparation import (
    DUPLICATE_EVIDENCE_STALE,
    DUPLICATE_EVIDENCE_UNAVAILABLE,
    PROVIDER_DUPLICATE_FOUND,
    PROVIDER_DUPLICATE_WEAK_SIGNAL,
    UNRESOLVED_CREATE_CONFLICT,
    DuplicateVerdict,
)
from integrations.marketplaces.smartstore.product import seller_management_code
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    api,
    container,
)
from tests.support.gate1_support import CLIENT
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections
from tests.support.register_support import MARKET, OPERATOR, establish, prepared

pytestmark = pytest.mark.integration

CID = "corr-s3b"
PREPARATIONS = "/api/v1/register/preparations"
CONTRACT = "catalog-duplicate-test/v1"
NAME = "authored listing name"


# ---------------------------------------------------------------- fakes


def _fields(
    name: str, code: str | None, status: str = "SALE", barcode: str | None = None
) -> ListingFields:
    canonical = {
        "name": name,
        "seller_keys": {"seller_management_code": code, "seller_barcode": barcode},
    }
    return ListingFields(
        fields_version="fixture-fields/v1",
        sale_status=status,
        display_status="ON",
        name=name,
        sale_price=10000,
        stock_quantity=5,
        seller_management_code=code,
        seller_barcode=barcode,
        leaf_category_id=None,
        canonical=canonical,
    )


class FakeCatalogSource:
    """The store's catalog as the test says it is: synthetic listings, no provider."""

    marketplace_key = MARKET
    contract_version = "fixture-catalog/v1"

    def __init__(self) -> None:
        self.listings: dict[str, tuple[str, str | None]] = {}
        self.outcome = COMPLETE
        # Optional per-listing sale status and barcode (default SALE, none).
        self.statuses: dict[str, str] = {}
        self.barcodes: dict[str, str] = {}

    def put(self, origin: str, name: str, code: str | None = None) -> None:
        self.listings[origin] = (name, code)

    def available(self) -> bool:
        return True

    def enumerate(self, *, pause: Callable[[], None], observe: Any = None) -> Enumeration:
        entries = tuple(
            CatalogEntry(origin, f"9{origin}", code, 1)
            for origin, (_, code) in sorted(self.listings.items())
        )
        if self.outcome != COMPLETE:
            return Enumeration(self.outcome, self.contract_version, error_code="FIXTURE_DOWN")
        return Enumeration(
            COMPLETE,
            self.contract_version,
            entries=entries,
            total_elements=len(entries),
            pages_read=1,
        )

    def read(self, origin_product_no: str, *, observe: Any = None) -> ListingRead:
        name, code = self.listings[origin_product_no]
        return ListingRead(
            OBSERVED,
            fields=_fields(
                name,
                code,
                self.statuses.get(origin_product_no, "SALE"),
                self.barcodes.get(origin_product_no),
            ),
        )


class FakeSpot:
    """The positive-only seller-code search of the first CREATE copy."""

    def __init__(self, answer: Mapping[str, Any] | Exception) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        self.calls.append(listing_identity)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def confirms_candidate(self, **_: Any) -> bool:  # pragma: no cover - never asked here
        return False


class Store:
    """One bound store: its catalog owner (on its own fake clock), and the source REGISTER reads."""

    def __init__(
        self,
        container: Container,  # noqa: F811
        account: str,
        *,
        conventions: Iterable[str] = (),
        spot: FakeSpot | None = None,
        mutation: Callable[[str, str], datetime | None] | None = None,
        confirm_coverage: bool = True,
        member_gtins: Callable[[Any], Iterable[str]] | None = None,
        different: Callable[[str, str], Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        self.clock = FakeClock(datetime.now(UTC))
        self.source = FakeCatalogSource()
        self.catalog = StoreCatalogService(
            db=container.db,
            clock=self.clock,
            source=self.source,
            account_of=lambda key: account if key == MARKET else None,
            interval_s=1800,
            pause_s=0.0,
        )
        self.refreshes = 0
        # ADR-0042 §5.2 tightening: the seller's per-status coverage confirmation.
        self.coverage = CatalogCoverage(catalog=self.catalog, audit=container.audit)
        self.confirm_coverage = confirm_coverage
        self.duplicates = CatalogDuplicateSource(
            marketplace_key=MARKET,
            contract_version=CONTRACT,
            catalog=self.catalog,
            seller_code=seller_management_code,
            convention_codes=lambda items: tuple(conventions),
            mutation_started_at=mutation or container.registrations.last_mutation_started,
            refresh=self._refresh,
            spot=spot,
            coverage=self.coverage,
            member_gtins=member_gtins,
            different=different,
        )
        container.registration_preparations._duplicate_lookup = self.duplicates

    def _refresh(self) -> bool:
        self.refreshes += 1
        return True

    def sync(self, trigger: str = CONNECT) -> Any:
        run = self.catalog.sync(trigger=trigger, correlation_id=CID)
        assert run.outcome == COMPLETE, run
        if self.confirm_coverage and self.coverage.view().state != "CONFIRMED":
            # The seller looked at 「스토어 상품」 and confirmed its counts.
            self.coverage.confirm(run_id=run.run_id, actor=OPERATOR, correlation_id=CID)
        return run


# ---------------------------------------------------------------- the unit


def authored(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    **policy: Any,
) -> dict[str, Any]:
    """One authored, otherwise-READY preparation under a duplicate-proof policy."""
    from app.stages.register.category_metadata import CategoryMetadataStore
    from app.stages.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
    from app.stages.register.target_policy import TargetPolicyStore
    from tests.integration.register.test_m5_register_api import _inputs
    from tests.support.register_support import FakeCapability, draft, metadata, ready_item, target

    account = establish(container, config, MARKET, "uid-market-a-1")
    revision = TargetPolicyStore(
        container.db, container.clock, container.audit, container.authoring_revisions
    ).append(
        MARKET, account,
        {"marketplace_key": MARKET, "marketplace_account_id": account, "source": "s3b"},
        expected_current_revision=None, authored_by=OPERATOR, correlation_id=CID,
    )  # fmt: skip
    reviewed = metadata()
    category = CategoryMetadataStore(container.db, container.clock, container.audit).append(
        MARKET, reviewed.taxonomy_revision, reviewed.category_id,
        {
            "marketplace_key": MARKET,
            "taxonomy_revision": reviewed.taxonomy_revision,
            "category_id": reviewed.category_id,
            "content_provenance": "OPERATOR_CONFIRMED",
        },
        reviewed=True,
        expected_current_revision=None, recorded_by=OPERATOR, correlation_id=CID,
    )  # fmt: skip
    served = container.registration_preflight
    served._policies = StaticRegistrationPolicy(
        (target(account, policy_revision=revision.policy_revision, **policy),)
    )
    served._metadata = StaticRegistrationMetadata(
        (metadata(metadata_revision=category.metadata_revision),), marketplace_key=MARKET
    )
    served._capability = FakeCapability()
    item = ready_item(container, Collections.of(container, config), "1234")
    draft_id = draft(container.registrations, account, [item])
    created = api.post(
        PREPARATIONS,
        json={"draft_id": draft_id, "item_ids": [item.item_id], "actor": OPERATOR,
              "inputs": _inputs(item)},
        headers=CLIENT,
    )  # fmt: skip
    assert created.status_code == 200, created.text
    return {
        "account": account,
        "draft_id": draft_id,
        "item": item,
        "preparation_id": created.json()["preparation_id"],
    }


def _identity(container: Container, unit: dict[str, Any]) -> str:  # noqa: F811
    return container.registration_preparations.evaluate(
        unit["preparation_id"]
    ).resolved.listing_identity


def _row(api: TestClient, unit: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    units = api.get(f"/api/v1/register/units/{unit['draft_id']}", headers=CLIENT).json()
    (found,) = [u for u in units if u["unit_ref"] == unit["preparation_id"]]
    return dict(found)


def _freeze(container: Container, unit: dict[str, Any]) -> Any:  # noqa: F811
    authoring = container.registration_preparations
    staged = authoring.stage_candidate(unit["preparation_id"])
    assert staged.status is ReadinessStatus.READY, staged.codes
    return authoring.freeze(
        unit["preparation_id"],
        actor=OPERATOR,
        duplicate_evidence=staged.request.duplicate_evidence,
        prepared_assets=prepared(staged),
    )


def _codes(container: Container, unit: dict[str, Any]) -> tuple[Any, set[str]]:  # noqa: F811
    result, _ = container.registration_preparations.evaluate_current(unit["preparation_id"])
    return result, set(result.codes)


# ---------------------------------------------------------------- tests


def test_without_a_complete_snapshot_there_is_no_evidence_and_the_store_is_checked_again(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    result, codes = _codes(container, unit)
    assert result.status is ReadinessStatus.REVIEW_REQUIRED
    assert DUPLICATE_EVIDENCE_UNAVAILABLE in codes
    assert store.refreshes >= 1
    # The seller's duplicate row: the automatic re-sync is running (「스토어 다시 확인 중…」).
    row = _row(api, unit)
    assert row["duplicate"]["state"] == "RESYNCING"
    assert row["duplicate"]["codes"] == [DUPLICATE_EVIDENCE_UNAVAILABLE]
    # The mutation stages stay closed and name why.
    with pytest.raises(RegistrationConflictError) as refused:
        container.registration_preparations.stage_candidate(unit["preparation_id"])
    assert refused.value.code == "REGISTER_DUPLICATE_EVIDENCE_UNAVAILABLE"
    assert refused.value.details["problem"] == DUPLICATE_EVIDENCE_UNAVAILABLE
    with pytest.raises(AppError) as frozen:
        container.registration_preparations.freeze_current(unit["preparation_id"], actor=OPERATOR)
    assert frozen.value.code == "REGISTER_PREFLIGHT_NOT_READY"


def test_an_empty_store_is_no_match_and_the_unit_registers_on_one_fingerprint(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    run = store.sync()
    result, codes = _codes(container, unit)
    assert result.status is ReadinessStatus.READY, codes
    evidence = result.request.duplicate_evidence
    assert evidence is not None and evidence.verdict is DuplicateVerdict.NO_MATCH
    assert evidence.lookup_contract_version == CONTRACT
    # The row: 「중복 없음 · <time> 동기화 기준」, with the sync's start as its time.
    row = _row(api, unit)
    assert row["duplicate"]["state"] == "NO_MATCH"
    assert row["duplicate"]["as_of"] is not None
    assert datetime.fromisoformat(row["duplicate"]["as_of"]) == run.as_of
    assert row["preflight"]["status"] == "READY"
    # Nothing to override when nothing matched.
    refused = api.post(
        f"{PREPARATIONS}/{unit['preparation_id']}/duplicate-override",
        json={"actor": OPERATOR, "reason": "again"},
        headers=CLIENT,
    )
    assert refused.status_code == 409
    assert "REGISTER_DUPLICATE_OVERRIDE_NOT_NEEDED" in refused.text
    # It freezes, and the first CREATE copy reproduces the frozen candidate.
    frozen = _freeze(container, unit)
    copy = container.registration_preparations.execution_copy(
        frozen.snapshot.registration_snapshot_id, spot_check=True
    )
    assert copy.final.dependency_fingerprint == frozen.snapshot.preflight_fingerprint


def test_a_stale_snapshot_is_never_no_match_and_a_resync_keeps_the_fingerprint(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    store.source.put("501", "another product", "OTHER-1")
    store.sync()
    frozen = _freeze(container, unit)
    snapshot_id = frozen.snapshot.registration_snapshot_id
    before = container.registration_preparations.stage_candidate(unit["preparation_id"])
    # 61 minutes after the sync's start: stale, whatever it found.
    store.clock.advance(61 * 60)
    other = authored_again(api, container, config, unit)
    result, codes = _codes(container, other)
    assert DUPLICATE_EVIDENCE_STALE in codes
    assert result.status is ReadinessStatus.STALE
    assert _row(api, other)["duplicate"]["state"] == "RESYNCING"
    # Send time checks freshness again: the first CREATE copy refuses until the store is re-read.
    with pytest.raises(RegistrationConflictError) as refused:
        container.registration_preparations.execution_copy(snapshot_id, spot_check=False)
    assert refused.value.code == "REGISTER_DUPLICATE_EVIDENCE_UNAVAILABLE"
    # A re-sync that finds the same thing: the same evidence digest, the same candidate.
    store.sync()
    after = container.registration_preparations.stage_candidate(unit["preparation_id"])
    assert after.request.duplicate_evidence == before.request.duplicate_evidence
    assert after.candidate_fingerprint == before.candidate_fingerprint
    copy = container.registration_preparations.execution_copy(snapshot_id, spot_check=False)
    assert copy.final.dependency_fingerprint == frozen.snapshot.preflight_fingerprint


def authored_again(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    unit: dict[str, Any],
) -> dict[str, Any]:
    """A second, unfrozen unit of the same account (another Draft of the same Item)."""
    from tests.integration.register.test_m5_register_api import _inputs
    from tests.support.register_support import draft

    item = unit["item"]
    draft_id = draft(container.registrations, unit["account"], [item])
    created = api.post(
        PREPARATIONS,
        json={"draft_id": draft_id, "item_ids": [item.item_id], "actor": OPERATOR,
              "inputs": _inputs(item)},
        headers=CLIENT,
    )  # fmt: skip
    assert created.status_code == 200, created.text
    return {**unit, "draft_id": draft_id, "preparation_id": created.json()["preparation_id"]}


def test_the_policy_bound_is_the_target_policys_and_measured_from_the_sync_start(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config, catalog_max_age=timedelta(minutes=10))
    store = Store(container, unit["account"])
    store.sync()
    store.clock.advance(10 * 60)
    assert _codes(container, unit)[0].status is ReadinessStatus.READY
    store.clock.advance(61)
    assert DUPLICATE_EVIDENCE_STALE in _codes(container, unit)[1]


def test_an_icbm_mutation_after_the_sync_start_makes_the_snapshot_stale(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    store.sync()
    frozen = _freeze(container, unit)
    assert container.registrations.last_mutation_started(MARKET, unit["account"]) is None
    other = authored_again(api, container, config, unit)
    assert DUPLICATE_EVIDENCE_STALE not in _codes(container, other)[1]
    # The CREATE of the first unit starts (one attempt opened, nothing sent here).
    with container.registrations.transaction() as work:
        work.start_attempt(
            frozen.intent.intent_id,
            sanitized_request={"test": "s3b"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        )
    assert container.registrations.last_mutation_started(MARKET, unit["account"]) is not None
    result, codes = _codes(container, other)
    assert DUPLICATE_EVIDENCE_STALE in codes
    # A fresh sync that does not show the first unit's listing is a NO_MATCH for the snapshot —
    # and never frees the in-flight CREATE's conflict scope (SCP-05).
    store.clock.advance(5)
    store.sync()
    result, codes = _codes(container, other)
    assert DUPLICATE_EVIDENCE_STALE not in codes
    evidence = result.request.duplicate_evidence
    assert evidence is not None and evidence.verdict is DuplicateVerdict.NO_MATCH
    assert UNRESOLVED_CREATE_CONFLICT in codes
    assert result.status is ReadinessStatus.BLOCKED


def test_a_seller_code_match_blocks_and_the_seller_may_list_it_again_on_purpose(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    code = seller_management_code(_identity(container, unit))
    store.source.put("777", "store product", f"  {code.upper()} ")
    store.sync()
    result, codes = _codes(container, unit)
    assert result.status is ReadinessStatus.DUPLICATE
    assert PROVIDER_DUPLICATE_FOUND in codes
    row = _row(api, unit)["duplicate"]
    assert row["state"] == "MATCH"
    assert row["matches"] == [
        {
            "key_kind": "SELLER_CODE",
            "marketplace_product_id": "777",
            "name": "store product",
            "sale_status": "SALE",
        }
    ]
    assert row["override_ids"] == []
    # 「의도적으로 한 번 더 등록」: explicit, with a reason, audited.
    path = f"{PREPARATIONS}/{unit['preparation_id']}/duplicate-override"
    assert api.post(path, json={"actor": OPERATOR}, headers=CLIENT).status_code == 422
    recorded = api.post(
        path, json={"actor": OPERATOR, "reason": "another bundle on purpose"}, headers=CLIENT
    )
    assert recorded.status_code == 200, recorded.text
    (override_id,) = recorded.json()["override_ids"]
    result, codes = _codes(container, unit)
    assert result.status is ReadinessStatus.READY, codes
    row = _row(api, unit)["duplicate"]
    assert row["state"] == "MATCH" and row["override_ids"] == [override_id]
    # Revocable: the duplicate blocks again.
    revoked = api.post(f"{path}/{override_id}/revoke", json={"actor": OPERATOR}, headers=CLIENT)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active"] is False
    assert _codes(container, unit)[0].status is ReadinessStatus.DUPLICATE


def test_an_owner_declared_convention_code_is_a_strong_match(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"], conventions=("KM1234",))
    store.source.put("778", "kept by hand", "km1234")
    store.sync()
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_FOUND in codes and result.status is ReadinessStatus.DUPLICATE


def test_a_same_name_listing_is_a_weak_signal_the_seller_decides(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    store.source.put("779", "Authored  Listing-Name", "SOMETHING-ELSE")
    store.sync()
    result, codes = _codes(container, unit)
    assert PROVIDER_DUPLICATE_WEAK_SIGNAL in codes
    assert PROVIDER_DUPLICATE_FOUND not in codes
    assert result.status is ReadinessStatus.REVIEW_REQUIRED
    row = _row(api, unit)["duplicate"]
    assert row["state"] == "MATCH" and row["matches"][0]["key_kind"] == "NORMALIZED_NAME"


@pytest.mark.parametrize(
    ("answer", "refused"),
    [
        ({"status": "COMPLETE", "candidates": [{"origin_product_no": "990"}]}, True),
        ({"status": "COMPLETE", "candidates": []}, False),
        ({"status": "UNAVAILABLE", "candidates": [{"origin_product_no": "990"}]}, False),
        ({"status": "ERROR", "candidates": []}, False),
        (RuntimeError("search failed"), False),
    ],
)
def test_the_first_create_copy_spot_check_is_positive_only(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    answer: Mapping[str, Any] | Exception,
    refused: bool,
) -> None:
    unit = authored(api, container, config)
    spot = FakeSpot(answer)
    store = Store(container, unit["account"], spot=spot)
    store.sync()
    frozen = _freeze(container, unit)
    assert spot.calls == []  # never on a screen or a stage candidate
    snapshot_id = frozen.snapshot.registration_snapshot_id
    if refused:
        with pytest.raises(AppError) as copy_refused:
            container.registration_preparations.execution_copy(snapshot_id, spot_check=True)
        assert copy_refused.value.code == "REGISTER_FIRST_CREATE_INPUTS_NOT_READY"
        assert PROVIDER_DUPLICATE_FOUND in copy_refused.value.details["reasons"]
        # The queue path is the first CREATE copy: nothing is queued.
        with pytest.raises(AppError) as queued:
            container.register.enqueue_create(frozen.intent.intent_id)
        assert queued.value.code == "REGISTER_FIRST_CREATE_INPUTS_NOT_READY"
        assert container.jobs.count(job_type_prefix="register.create") == 0
        assert spot.calls == [frozen.snapshot.listing_identity] * 2
    else:
        copy = container.registration_preparations.execution_copy(snapshot_id, spot_check=True)
        assert copy.final.dependency_fingerprint == frozen.snapshot.preflight_fingerprint
        assert spot.calls == [frozen.snapshot.listing_identity]


def test_the_pre_registration_sync_starts_by_itself_but_never_hammers_a_failing_store(
    container: Container,  # noqa: F811
) -> None:
    """ADR-0042 §5.2: a stale or missing snapshot starts the automatic re-sync (a routine read);
    a store whose last sync just failed is retried only after the retry pause, and nothing starts
    while the periodic sync is disabled by policy."""
    account = "mpa-s3b-refresh"
    clock = FakeClock(datetime.now(UTC))
    source = FakeCatalogSource()

    def service(interval_s: float) -> StoreCatalogService:
        return StoreCatalogService(
            db=container.db,
            clock=clock,
            source=source,
            account_of=lambda key: account if key == MARKET else None,
            interval_s=interval_s,
            retry_after_s=300,
            pause_s=0.0,
        )

    disabled = StoreCatalogScheduler(service(0))
    assert disabled.refresh_for_registration() is False
    catalog = service(1800)
    scheduler = StoreCatalogScheduler(catalog)
    requested: list[str] = []

    def request(*, trigger: str = "OPERATOR") -> bool:
        requested.append(trigger)
        return True

    scheduler.request = request  # type: ignore[method-assign]
    # No sync yet: due at once.
    assert scheduler.refresh_for_registration() is True
    assert requested == [PRE_REGISTRATION]
    # A sync that just failed: not again until the retry pause has passed.
    source.outcome = "UNAVAILABLE"
    catalog.sync(trigger=PRE_REGISTRATION, correlation_id=CID)
    assert scheduler.refresh_for_registration() is False
    clock.advance(300)
    assert scheduler.refresh_for_registration() is True
    # A COMPLETE but stale snapshot: due.
    source.outcome = COMPLETE
    catalog.sync(trigger=PRE_REGISTRATION, correlation_id=CID)
    clock.advance(61 * 60)
    assert scheduler.refresh_for_registration() is True
    assert requested == [PRE_REGISTRATION] * 3


def test_the_review_queue_reads_the_same_duplicate_answer_and_its_token_moves_with_it(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    """The queue never shows 「중복 확인 근거 없음」 while the editor shows 「중복 없음」:
    the review producer evaluates with the duplicate owner's current answer, and its truth token
    carries that owner's state, so a completed sync or a snapshot going stale re-derives the
    queue."""
    from app.capabilities.review.preflight_producer import PreflightReviewProducer

    unit = authored(api, container, config)
    store = Store(container, unit["account"])
    producer = PreflightReviewProducer(
        registrations=container.registrations,
        preparations=container.registration_preparations,
        preflight=container.registration_preflight,
        readiness=container.product_readiness,
        audit=container.audit,
    )
    scope = {"preparation_id": unit["preparation_id"]}

    def codes() -> set[str]:
        return {condition.reason_code for condition in producer.derive(scope)}

    before = producer.truth_token()
    refreshes = store.refreshes
    assert DUPLICATE_EVIDENCE_UNAVAILABLE in codes()
    assert "DUPLICATE_EVIDENCE_MISSING" not in codes()
    # A background reader never starts the store re-sync.
    assert store.refreshes == refreshes
    store.sync()
    synced = producer.truth_token()
    assert synced != before
    assert not {code for code in codes() if code.startswith("DUPLICATE_EVIDENCE")}
    assert _row(api, unit)["duplicate"]["state"] == "NO_MATCH"
    # Going stale moves the token too; a STALE reason is the owner's verdict, never review work.
    store.clock.advance(61 * 60)
    assert producer.truth_token() != synced
    assert not {code for code in codes() if code.startswith("DUPLICATE_EVIDENCE")}
