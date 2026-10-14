"""ADR-0038 §6 (O3): the SmartStore cross-border projection of a 구매대행 unit, pure.

Every input is invented and fully resolved here (the shapes of ``test_m5_preflight_rules``): no
database, no provider. Proven:

- a KRW unit is byte-identical to before O3: the same candidate and final fingerprints, the same
  payload digest and the same CREATE document bytes (golden values recorded on ``main`` 6f390d39);
- a foreign unit freezes its ``overseas`` section and the wire projects the overseas 출고지 as
  ``claimDeliveryInfo.shippingAddressId`` and ``customsTaxType`` from the pinned duty decision —
  ``INCLUDED`` for ``NOT_OVER_150`` and ``INCLUDED``, ``EXCLUDED`` for ``EXCLUDED`` (FX-04) — while
  the uncaptured members stay named gaps;
- each missing or contradictory input is ``REVIEW_REQUIRED`` and freezes nothing;
- the overseas inputs enter the fingerprint of a foreign unit only.
"""

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from app.capabilities.overseas.settings import OverseasSettings
from app.stages.products.model import ReadinessStatus
from app.stages.register.model import ListingShape
from app.stages.register.overseas import (
    NOT_FOREIGN,
    OVERSEAS_DELIVERY_POLICY_MISSING,
    OVERSEAS_IMPORTER_UNSET,
    OVERSEAS_ITEMS_INCONSISTENT,
    OVERSEAS_MARKETPLACE_UNSUPPORTED,
    OVERSEAS_REASON_CODES,
    OVERSEAS_SETTINGS_UNAVAILABLE,
    OVERSEAS_SHIPPING_ADDRESS_INVALID,
    OVERSEAS_SHIPPING_ADDRESS_UNSET,
    OverseasListingReader,
    OverseasReading,
    OverseasSource,
    OverseasTerms,
    address_number,
)
from app.stages.register.payload import PayloadNotReadyError, build_payload
from app.stages.register.policy import DeliveryPolicy
from app.stages.register.preparation import (
    REASON_CODES,
    DraftItemState,
    PinnedPrice,
    PreflightRequest,
    PreflightResult,
    ResolvedUnit,
    UnitRequest,
    listing_identity,
)
from integrations.marketplaces.smartstore import product
from tests.unit.register import test_m5_preflight_rules as base

SMARTSTORE = "smartstore"
CONTEXT = base._context(marketplace_key=SMARTSTORE)
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
OVERSEAS_ADDRESS = "200441299"
IMPORTER = "invented importer"

# Recorded on main 6f390d39 (before O3) for exactly the KRW unit ``krw_unit()`` builds.
GOLDEN_CANDIDATE = "1df9a5dc475173f8d4b7f20e4336d13d49abb530869e880c06933d8ddfc1e448"
GOLDEN_FINAL = "a8f32f33301c32b964767a7fb4a4637493c97154d598be6383878872fc6c02dc"
GOLDEN_PAYLOAD_DIGEST = "2b4054fcea18e13974e210e3cae053fc03608b16582e997ceed0e3ca82c5bdb0"
GOLDEN_DOCUMENT_SHA256 = "f7d6f81939ff7cfdc3868b46b39153db146c3b133cf6be5964f0882a84cc19c8"


def _item(
    item_id: str,
    ordinal: int,
    group: str,
    signature: str,
    *,
    currency: str | None = None,
    duty: str | None = None,
) -> Any:
    pin = base.item(item_id, ordinal, group, signature).pin
    assert pin is not None
    pin = replace(
        pin,
        marketplace_key=SMARTSTORE,
        pricing_context_fingerprint=CONTEXT.fingerprint,
        final_sale_price_krw=19900,
        source_currency=currency,
        duty_decision=duty,
    )
    return base.item(
        item_id, ordinal, group, signature, pin=pin, current_price_id=pin.pricing_snapshot_id
    )


def _unit(items: tuple[Any, ...], **overrides: Any) -> ResolvedUnit:
    key = [(i.product_group_id, i.composition_signature) for i in items]
    values: dict[str, Any] = {
        "marketplace_key": SMARTSTORE,
        "items": items,
        "open_items": tuple(
            DraftItemState(i.item_id, i.ordinal, i.pin.pricing_snapshot_id) for i in items
        ),
        "unit_item_ids": tuple(i.item_id for i in items),
        "target": base.target(
            marketplace_key=SMARTSTORE, delivery_policy=DELIVERY, pricing_context=CONTEXT
        ),
        "listing_shape": (
            ListingShape.SEPARATE_LISTINGS
            if len(items) == 1
            else ListingShape.SINGLE_LISTING_WITH_OPTIONS
        ),
        "listing_identity": listing_identity(SMARTSTORE, base.ACCOUNT, base.DRAFT, key, 0),
    }
    values.update(overrides)
    return base.resolved(**values)


def krw_unit(**overrides: Any) -> ResolvedUnit:
    return _unit((_item("i1", 0, "g1", "s" * 64),), **overrides)


def reading(
    address: str | None = OVERSEAS_ADDRESS,
    importer: str | None = IMPORTER,
    countries: dict[str, str | None] | None = None,
) -> OverseasReading:
    countries = countries if countries is not None else {"i1": "CN", "i2": "CN"}
    return OverseasReading(
        OverseasTerms(address, importer, 7, 14, 3, "f" * 64),
        tuple(OverseasSource(item, "ali1688", country) for item, country in countries.items()),
    )


def foreign_unit(
    duty: str = "NOT_OVER_150", *, currency: str = "CNY", **overrides: Any
) -> ResolvedUnit:
    values: dict[str, Any] = {"overseas": reading()}
    values.update(overrides)
    return _unit((_item("i1", 0, "g1", "s" * 64, currency=currency, duty=duty),), **values)


def req(unit: ResolvedUnit, **overrides: Any) -> PreflightRequest:
    options = (
        {}
        if len(unit.items) == 1
        else {item.item_id: {"size": f"size-{n}"} for n, item in enumerate(unit.items)}
    )
    values: dict[str, Any] = {
        "unit": UnitRequest(base.DRAFT, 3, unit.unit_item_ids),
        "listing": replace(base.LISTING, options=options),
        "duplicate_evidence": base.evidence(
            marketplace_key=SMARTSTORE, listing_identity=unit.listing_identity
        ),
    }
    values.update(overrides)
    return base.request(**values)


def final(unit: ResolvedUnit) -> PreflightResult:
    return base.final(req(unit), unit)


def _codes(result: PreflightResult) -> set[str]:
    return set(result.codes)


def _document(payload: dict[str, Any]) -> dict[str, Any]:
    return product.project(payload).document.mapping()


# ---------------------------------------------------------------- KRW: byte-identical (golden)


def test_a_krw_unit_is_byte_identical_to_before_o3() -> None:
    result = final(krw_unit())
    assert result.status is ReadinessStatus.READY, result.reasons
    assert result.overseas is NOT_FOREIGN
    assert result.candidate_fingerprint == GOLDEN_CANDIDATE
    assert result.dependency_fingerprint == GOLDEN_FINAL
    built = build_payload(result)
    assert built.payload_digest == GOLDEN_PAYLOAD_DIGEST
    assert "overseas" not in built.payload
    projection = product.project(built.payload)
    assert hashlib.sha256(projection.document.encoded()).hexdigest() == GOLDEN_DOCUMENT_SHA256
    attribute = projection.document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["customsTaxType"] == "NOT_APPLICABLE"
    # A reading wired for a KRW unit changes nothing either: the inputs are a foreign unit's only.
    again = final(krw_unit(overseas=reading()))
    assert (again.candidate_fingerprint, again.dependency_fingerprint) == (
        GOLDEN_CANDIDATE,
        GOLDEN_FINAL,
    )
    assert build_payload(again).payload_digest == GOLDEN_PAYLOAD_DIGEST


# ---------------------------------------------------------------- a foreign unit, projected


@pytest.mark.parametrize(
    ("duty", "customs"),
    [("NOT_OVER_150", "INCLUDED"), ("INCLUDED", "INCLUDED"), ("EXCLUDED", "EXCLUDED")],
)
def test_a_foreign_unit_freezes_its_section_and_projects_the_pinned_duty_term(
    duty: str, customs: str
) -> None:
    result = final(foreign_unit(duty))
    assert result.status is ReadinessStatus.READY, result.reasons
    payload = build_payload(result).payload
    assert payload["overseas"] == {
        "version": "overseas-listing/v1",
        "source_currency": "CNY",
        "source_country": "CN",
        "duty_decisions": [duty],
        "shipping_address_id": int(OVERSEAS_ADDRESS),
        "importer": IMPORTER,
        "settings_revision_no": 3,
        "settings_fingerprint": "f" * 64,
    }
    projection = product.project(payload)
    document = projection.document.mapping()
    origin = document["originProduct"]
    # FX-04: the listing says what the pinned cost assumed — read from the pin, never recomputed.
    assert origin["detailAttribute"]["customsTaxType"] == customs
    claim = origin["deliveryInfo"]["claimDeliveryInfo"]
    # The overseas 출고지; the account's return address and fees stay the delivery policy's.
    assert claim["shippingAddressId"] == int(OVERSEAS_ADDRESS)
    assert claim["returnAddressId"] == DELIVERY.return_address_id
    assert origin["deliveryInfo"]["deliveryFee"]["baseFee"] == DELIVERY.base_fee_krw
    # What the captured evidence does not carry is never sent: it is a named gap.
    assert set(product.OVERSEAS_GAPS) <= set(projection.gaps)
    assert projection.sendable is False
    for never in ("businessCustomsClearanceSaleYn", "kcExemptionType", "importer", "OVERSEAS"):
        assert never not in projection.document.canonical_json
    assert origin["detailAttribute"]["originAreaInfo"] == {"originAreaCode": "03"}


def test_a_jpy_unit_projects_the_same_way() -> None:
    unit = foreign_unit("INCLUDED", currency="JPY", overseas=reading(countries={"i1": "JP"}))
    payload = build_payload(final(unit)).payload
    assert (payload["overseas"]["source_currency"], payload["overseas"]["source_country"]) == (
        "JPY",
        "JP",
    )
    assert _document(payload)["originProduct"]["detailAttribute"]["customsTaxType"] == "INCLUDED"


def test_options_of_one_foreign_listing_agree_and_project_once() -> None:
    items = (
        _item("i1", 0, "g1", "s" * 64, currency="CNY", duty="NOT_OVER_150"),
        _item("i2", 1, "g2", "t" * 64, currency="CNY", duty="INCLUDED"),
    )
    result = final(_unit(items, overseas=reading()))
    assert result.status is ReadinessStatus.READY, result.reasons
    payload = build_payload(result).payload
    assert payload["overseas"]["duty_decisions"] == ["INCLUDED", "NOT_OVER_150"]
    assert _document(payload)["originProduct"]["detailAttribute"]["customsTaxType"] == "INCLUDED"


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"overseas": reading(address=None)}, OVERSEAS_SHIPPING_ADDRESS_UNSET),
        ({"overseas": reading(address="0")}, OVERSEAS_SHIPPING_ADDRESS_INVALID),
        ({"overseas": reading(address=str(2**63))}, OVERSEAS_SHIPPING_ADDRESS_INVALID),
        ({"overseas": reading(importer=None)}, OVERSEAS_IMPORTER_UNSET),
        ({"overseas": reading(importer="   ")}, OVERSEAS_IMPORTER_UNSET),
        ({"overseas": None}, OVERSEAS_SETTINGS_UNAVAILABLE),
    ],
)
def test_a_missing_overseas_input_is_review_required(changes: dict[str, Any], code: str) -> None:
    result = base.candidate(req(foreign_unit(**changes)), foreign_unit(**changes))
    assert result.status is ReadinessStatus.REVIEW_REQUIRED
    assert code in _codes(result)
    assert all(r.status is ReadinessStatus.REVIEW_REQUIRED for r in result.reasons)
    assert result.overseas.listing is None


def test_no_delivery_policy_to_carry_the_overseas_address_is_review_required() -> None:
    unit = foreign_unit(target=base.target(marketplace_key=SMARTSTORE, pricing_context=CONTEXT))
    assert _codes(base.candidate(req(unit), unit)) == {OVERSEAS_DELIVERY_POLICY_MISSING}


def test_a_foreign_item_on_a_marketplace_without_a_cross_border_projection_is_review() -> None:
    pin = base.item("i1", 0, "g1", "s" * 64).pin
    assert pin is not None
    pin = replace(pin, source_currency="CNY", duty_decision="INCLUDED")
    unit = base.resolved(
        items=(base.item("i1", 0, "g1", "s" * 64, pin=pin),),
        open_items=(DraftItemState("i1", 0, pin.pricing_snapshot_id),),
        unit_item_ids=("i1",),
        listing_shape=ListingShape.SEPARATE_LISTINGS,
        overseas=reading(),
    )
    request = base.request(
        unit=UnitRequest(base.DRAFT, 3, ("i1",)),
        listing=replace(base.LISTING, options={}),
        duplicate_evidence=base.evidence(listing_identity=unit.listing_identity),
    )
    result = base.candidate(request, unit)
    assert OVERSEAS_MARKETPLACE_UNSUPPORTED in _codes(result)
    assert result.status is ReadinessStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "items",
    [
        # A KRW price and a foreign one in one listing.
        (
            _item("i1", 0, "g1", "s" * 64, currency="CNY", duty="INCLUDED"),
            _item("i2", 1, "g2", "t" * 64),
        ),
        # Two currencies.
        (
            _item("i1", 0, "g1", "s" * 64, currency="CNY", duty="INCLUDED"),
            _item("i2", 1, "g2", "t" * 64, currency="JPY", duty="INCLUDED"),
        ),
        # Duties included for one option and paid by the buyer for another.
        (
            _item("i1", 0, "g1", "s" * 64, currency="CNY", duty="EXCLUDED"),
            _item("i2", 1, "g2", "t" * 64, currency="CNY", duty="NOT_OVER_150"),
        ),
    ],
)
def test_items_that_cannot_form_one_cross_border_listing_are_review(items: Any) -> None:
    unit = _unit(items, overseas=reading())
    result = base.candidate(req(unit), unit)
    assert _codes(result) == {OVERSEAS_ITEMS_INCONSISTENT}


def test_two_declared_countries_cannot_form_one_listing() -> None:
    items = (
        _item("i1", 0, "g1", "s" * 64, currency="CNY", duty="INCLUDED"),
        _item("i2", 1, "g2", "t" * 64, currency="CNY", duty="INCLUDED"),
    )
    unit = _unit(items, overseas=reading(countries={"i1": "CN", "i2": "JP"}))
    assert _codes(base.candidate(req(unit), unit)) == {OVERSEAS_ITEMS_INCONSISTENT}


def test_a_ready_foreign_result_without_its_section_is_never_a_payload() -> None:
    ready = final(foreign_unit())
    broken = replace(ready, overseas=replace(ready.overseas, listing=None))
    with pytest.raises(PayloadNotReadyError):
        build_payload(broken)


# ---------------------------------------------------------------- the fingerprint


@pytest.mark.parametrize(
    "changed",
    [
        reading(address="200441300"),
        reading(importer="another importer"),
        reading(countries={"i1": "JP"}),
        OverseasReading(OverseasTerms(OVERSEAS_ADDRESS, IMPORTER, 7, 14, 4, "e" * 64)),
    ],
)
def test_the_overseas_inputs_enter_a_foreign_fingerprint(changed: OverseasReading) -> None:
    before = final(foreign_unit())
    after = final(foreign_unit(overseas=changed))
    assert before.candidate_fingerprint != after.candidate_fingerprint


def test_every_overseas_reason_is_inventoried() -> None:
    assert OVERSEAS_REASON_CODES <= REASON_CODES


def test_an_address_number_is_a_positive_int64_and_nothing_else() -> None:
    assert address_number("200441299") == 200441299
    assert address_number(str(2**63 - 1)) == 2**63 - 1
    for bad in (None, "", "0", "-1", "12a", "١٢٣", str(2**63), " 1"):
        assert address_number(bad) is None, bad


# ---------------------------------------------------------------- the reader


def test_the_reader_composes_the_settings_the_suppliers_and_their_declared_countries() -> None:
    settings = OverseasSettings(
        smartstore_overseas_shipping_address_id=OVERSEAS_ADDRESS,
        importer_label=IMPORTER,
        revision_no=2,
        fingerprint="a" * 64,
    )
    reader = OverseasListingReader(
        settings=lambda: settings,
        suppliers=lambda ids: [(i, {"i1": "superdelivery", "i2": None}[i]) for i in ids],
        country_of={"superdelivery": "JP"}.get,
    )
    read = reader(["i1", "i2"])
    assert read.terms == OverseasTerms(OVERSEAS_ADDRESS, IMPORTER, 7, 14, 2, "a" * 64)
    assert read.sources == (
        OverseasSource("i1", "superdelivery", "JP"),
        OverseasSource("i2", None, None),
    )
    assert json.dumps(read.canonical(), sort_keys=True)


def test_a_pinned_price_without_landed_cost_states_no_currency() -> None:
    pin = base.item("i1", 0, "g1", "s" * 64).pin
    assert isinstance(pin, PinnedPrice)
    assert (pin.source_currency, pin.duty_decision) == (None, None)
    # The pin's canonical form is unchanged by O3: the snapshot id already names the landed cost.
    assert "source_currency" not in pin.canonical()


# ---------------------------------------------------------------- the editor's read shape


def test_the_preflight_view_shows_the_overseas_inputs_of_a_foreign_unit_only() -> None:
    from app.stages.register.service import _preflight_view

    krw = _preflight_view(final(krw_unit()), source="PREPARATION", matches=None)
    assert "overseas" not in krw.model_dump(mode="json")
    missing = foreign_unit("EXCLUDED", overseas=reading(importer=None))
    view = _preflight_view(
        base.candidate(req(missing), missing), source="PREPARATION", matches=None
    )
    shown = view.model_dump(mode="json")["overseas"]
    assert shown == {
        "source_currencies": ["CNY"],
        "source_countries": ["CN"],
        "duty_decisions": ["EXCLUDED"],
        "duties_included": False,
        "shipping_address_id": OVERSEAS_ADDRESS,
        "importer": None,
        "delivery_days_min": 7,
        "delivery_days_max": 14,
        "settings_revision_no": 3,
    }
    assert OVERSEAS_IMPORTER_UNSET in view.reason_codes
