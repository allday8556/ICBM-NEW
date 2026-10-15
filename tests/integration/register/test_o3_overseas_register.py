"""ADR-0038 §6 (O3): a 구매대행 Item end to end to a frozen Snapshot and its SmartStore projection.

The real container on a migrated database, in DRY_RUN: the source is collected in CNY, priced at
its landed cost from invented FX publications, prepared for a SmartStore account, frozen, and the
frozen payload is projected by the SmartStore adapter. Nothing reaches a provider and nothing is
sent: the projection names its uncaptured cross-border members as gaps, so it is not sendable.

Proven here:
- the overseas settings (출고지, importer) and the supplier's declared country are read for a
  foreign unit, enter its preparation fingerprint and are frozen as ``overseas-listing/v1``;
- the projection carries the overseas 출고지 and the pinned duty term (``NOT_OVER_150`` →
  ``INCLUDED``; the user's exclusion above USD 150 → ``EXCLUDED``);
- a missing 출고지 or importer is ``REVIEW_REQUIRED`` and nothing is frozen;
- a settings change after the freeze makes the frozen preparation stale;
- a KRW Item through the same owners freezes no overseas section and states no overseas reason.
"""

from dataclasses import replace
from typing import Any

import pytest

from app.capabilities.overseas.settings import SaveOverseasSettingsRequest
from app.config import AppConfig
from app.container import Container
from app.platform.core.execution import ExecutionMode
from app.stages.collect.facts import FieldFact
from app.stages.products.model import ReadinessStatus
from app.stages.products.pricing import PricingContextInput
from app.stages.products.pricing_service import PricingOutcome
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.model import ListingShape, RegistrationConflictError
from app.stages.register.overseas import (
    OVERSEAS_IMPORTER_UNSET,
    OVERSEAS_REASON_CODES,
    OVERSEAS_SHIPPING_ADDRESS_UNSET,
    OverseasListingReader,
)
from app.stages.register.policy import (
    DeliveryPolicy,
    StaticRegistrationMetadata,
    StaticRegistrationPolicy,
)
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    PreflightRequest,
    PreflightResult,
    UnitRequest,
)
from app.stages.register.store import RegistrationStore
from integrations.marketplaces.smartstore import product
from tests.integration.products.test_landed_cost_pricing import FRI, _foreign, _rates
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections
from tests.support.product_support import context as base_context
from tests.support.product_support import product as krw_product
from tests.support.register_support import (
    CATEGORY,
    CID,
    OPERATOR,
    TAXONOMY,
    FakeCapability,
    establish,
    listing,
    metadata,
    no_match,
    prepared,
    select_and_pass,
    target,
)

pytestmark = pytest.mark.integration

SMARTSTORE = "smartstore"
ADDRESS = "200441299"
IMPORTER = "invented importer"
DELIVERY = DeliveryPolicy(
    delivery_type="DELIVERY",
    delivery_attribute_type="NORMAL",
    delivery_company="CJGLS",
    delivery_fee_type="PAID",
    base_fee_krw=3000,
    delivery_fee_pay_type="PREPAID",
    return_delivery_company_priority_type="PRIMARY",
    return_delivery_fee_krw=3000,
    exchange_delivery_fee_krw=6000,
    shipping_address_id=200441202,
    return_address_id=200441203,
)


def _context() -> PricingContextInput:
    return base_context(marketplace_key=SMARTSTORE)


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, SMARTSTORE, "uid-smartstore-o3")


@pytest.fixture
def service(container: Container, account: str) -> RegistrationPreflightService:
    """The application's own owners, with a SmartStore account policy and an invented supplier
    country: the test supplier (``kmretail``) declares none, so the test declares one for it."""
    deployed = container.registration_preflight._overseas
    assert isinstance(deployed, OverseasListingReader)
    return RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=FakeCapability(),
        metadata=StaticRegistrationMetadata((metadata(),), marketplace_key=SMARTSTORE),
        policies=StaticRegistrationPolicy(
            (
                target(
                    account,
                    marketplace_key=SMARTSTORE,
                    pricing_context=_context(),
                    delivery_policy=DELIVERY,
                    after_service_telephone="02-000-0000",
                ),
            )
        ),
        overseas=OverseasListingReader(
            settings=deployed._settings,
            suppliers=deployed._suppliers,
            country_of={"kmretail": "CN"}.get,
        ),
    )


def _settings(container: Container, **changes: Any) -> None:
    current = container.overseas_settings.current()
    body: dict[str, Any] = {
        "expected_revision_no": current.revision_no,
        "fx_margin_percent": current.fx_margin_percent,
        "overseas_shipping_default_krw": 8000,
        "duties_included_over_usd150": current.duties_included_over_usd150,
        "duty_rate_percent": current.duty_rate_percent,
        "smartstore_overseas_shipping_address_id": ADDRESS,
        "overseas_delivery_days_min": 7,
        "overseas_delivery_days_max": 14,
        "importer_label": IMPORTER,
        **changes,
    }
    container.overseas_settings.save(
        SaveOverseasSettingsRequest(**body), actor=OPERATOR, correlation_id="cid-o3-settings"
    )


def _priced_item(
    container: Container, sources: Collections, fields: dict[str, FieldFact], source_id: str
) -> tuple[str, str]:
    run_id, revision = sources.collect(fields, source_product_id=source_id)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None
    select_and_pass(container, result.item_id, revision)
    priced = container.pricing.price(result.item_id, _context())
    assert priced.outcome is PricingOutcome.RECORDED, priced.reasons
    assert priced.snapshot is not None
    return result.item_id, priced.snapshot.pricing_snapshot_id


def _draft(store: RegistrationStore, account: str, item_id: str, price_id: str) -> str:
    with store.transaction() as unit:
        created = unit.create_draft(
            SMARTSTORE,
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        unit.add_draft_item(
            created.draft_id, item_id, price_id, added_by=OPERATOR, correlation_id=CID
        )
        return created.draft_id


def _request(store: RegistrationStore, draft_id: str) -> PreflightRequest:
    current = store.draft(draft_id)
    assert current is not None
    return PreflightRequest(
        unit=UnitRequest(draft_id, current.draft_revision),
        category=CategorySelection(
            CATEGORY, "mapping-test-1", TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        listing=listing([]),
        detail=DetailComposition("detail-test-1", "invented body text"),
    )


def _final(
    service: RegistrationPreflightService, req: PreflightRequest
) -> tuple[PreflightRequest, PreflightResult]:
    req = replace(req, duplicate_evidence=no_match(service.candidate(req)))
    candidate = service.candidate(req)
    assert candidate.status is ReadinessStatus.READY, candidate.reasons
    final = service.final(req, prepared(candidate))
    assert final.status is ReadinessStatus.READY, final.reasons
    return req, final


def _foreign_unit(
    container: Container,
    clock: FakeClock,
    sources: Collections,
    account: str,
    price: str = "5.80",
    source_id: str = "o3-cny-1",
) -> PreflightRequest:
    _rates(container, clock, FRI)
    item_id, price_id = _priced_item(container, sources, _foreign(price), source_id)
    return _request(
        container.registrations, _draft(container.registrations, account, item_id, price_id)
    )


def test_a_cny_item_freezes_its_cross_border_section_and_projects_it(
    container: Container,
    clock: FakeClock,
    sources: Collections,
    account: str,
    service: RegistrationPreflightService,
) -> None:
    assert container.config.execution_mode is ExecutionMode.DRY_RUN
    _settings(container)
    req, final = _final(service, _foreign_unit(container, clock, sources, account))
    assert final.resolved.overseas is not None
    assert final.dependencies["candidate"]["overseas"]["terms"]["importer_label"] == IMPORTER
    snapshot = RegistrationSnapshotBuilder(
        preflight=service, registrations=container.registrations
    ).freeze(final, created_by=OPERATOR, correlation_id=CID)
    payload = container.registrations.snapshot_payload(snapshot.registration_snapshot_id)
    assert payload is not None
    assert payload["overseas"] == {
        "version": "overseas-listing/v1",
        "source_currency": "CNY",
        "source_country": "CN",
        # 5.80 CNY is far under USD 150: duty-free list clearance, as the pin recorded.
        "duty_decisions": ["NOT_OVER_150"],
        "shipping_address_id": int(ADDRESS),
        "importer": IMPORTER,
        "settings_revision_no": 1,
        "settings_fingerprint": container.overseas_settings.current().fingerprint,
    }
    projection = product.project(payload)
    origin = projection.document.mapping()["originProduct"]
    assert origin["detailAttribute"]["customsTaxType"] == "INCLUDED"
    assert origin["deliveryInfo"]["claimDeliveryInfo"]["shippingAddressId"] == int(ADDRESS)
    assert origin["deliveryInfo"]["claimDeliveryInfo"]["returnAddressId"] == 200441203
    assert set(product.OVERSEAS_GAPS) <= set(projection.gaps)
    assert projection.sendable is False

    # A settings change after the freeze: the frozen preparation is stale, never re-labelled.
    _settings(container, expected_revision_no=1, importer_label="another importer")
    again = service.final(req, prepared(service.candidate(req)))
    assert again.dependency_fingerprint != final.dependency_fingerprint
    with pytest.raises(RegistrationConflictError) as stale:
        RegistrationSnapshotBuilder(
            preflight=service, registrations=container.registrations
        ).freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert stale.value.code == "REGISTER_PREFLIGHT_STALE"


def test_duties_the_user_excluded_above_usd150_are_projected_as_excluded(
    container: Container,
    clock: FakeClock,
    sources: Collections,
    account: str,
    service: RegistrationPreflightService,
) -> None:
    _settings(container, duties_included_over_usd150=False)
    # 1500 CNY is far above USD 150; the user excluded the duties, and the pin says so.
    _req, final = _final(service, _foreign_unit(container, clock, sources, account, "1500.00"))
    built = RegistrationSnapshotBuilder(
        preflight=service, registrations=container.registrations
    ).freeze(final, created_by=OPERATOR, correlation_id=CID)
    payload = container.registrations.snapshot_payload(built.registration_snapshot_id)
    assert payload is not None and payload["overseas"]["duty_decisions"] == ["EXCLUDED"]
    attribute = product.project(payload).document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["customsTaxType"] == "EXCLUDED"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"smartstore_overseas_shipping_address_id": None}, OVERSEAS_SHIPPING_ADDRESS_UNSET),
        ({"importer_label": None}, OVERSEAS_IMPORTER_UNSET),
    ],
)
def test_a_missing_overseas_setting_is_review_and_nothing_is_frozen(
    container: Container,
    clock: FakeClock,
    sources: Collections,
    account: str,
    service: RegistrationPreflightService,
    changes: dict[str, Any],
    code: str,
) -> None:
    _settings(container, **changes)
    req = _foreign_unit(container, clock, sources, account)
    candidate = service.candidate(replace(req, duplicate_evidence=None))
    req = replace(req, duplicate_evidence=no_match(candidate))
    candidate = service.candidate(req)
    assert candidate.status is ReadinessStatus.REVIEW_REQUIRED
    assert {r.code for r in candidate.reasons} == {code}
    assert candidate.upload_permitted is False


def test_a_krw_item_through_the_same_owners_states_and_freezes_nothing_overseas(
    container: Container,
    sources: Collections,
    account: str,
    service: RegistrationPreflightService,
) -> None:
    item_id, price_id = _priced_item(container, sources, krw_product(), "o3-krw-1")
    req = _request(
        container.registrations, _draft(container.registrations, account, item_id, price_id)
    )
    _req, final = _final(service, req)
    assert final.resolved.overseas is None
    assert "overseas" not in final.dependencies["candidate"]
    assert not {r.code for r in final.reasons} & OVERSEAS_REASON_CODES
    built = RegistrationSnapshotBuilder(
        preflight=service, registrations=container.registrations
    ).freeze(final, created_by=OPERATOR, correlation_id=CID)
    payload = container.registrations.snapshot_payload(built.registration_snapshot_id)
    assert payload is not None and "overseas" not in payload
    attribute = product.project(payload).document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["customsTaxType"] == product.REGISTRATION_CUSTOMS_TAX_TYPE
