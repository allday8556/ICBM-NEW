"""M5 PR-D on real owners: a frozen RegistrationSnapshot projected onto the SmartStore contract,
read back through a fake provider, compared, and recorded.

The Snapshot comes from the real PR-C path (preflight → builder → store), so the adapter is fed
the exact canonical payload production would freeze — never a hand-written stand-in. No test here
mutates the marketplace: IMAGE UPLOAD is adopted but not invoked here, every read call goes through
a fake transport, and ``product_registration.write`` stays UNVERIFIED throughout.
"""

import contextlib
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from app.config import AppConfig
from app.container import Container
from app.core.errors import AppError
from app.products.model import ReadinessStatus
from app.register import provider as ports
from app.register.builder import RegistrationSnapshotBuilder
from app.register.model import ListingShape
from app.register.payload import build_payload
from app.register.preparation import PreflightResult
from app.register.store import RegistrationStore
from integrations.marketplaces.smartstore import assets, lookup, product, readback
from integrations.marketplaces.smartstore.caller import (
    ProductReadRequest,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    SAFE_RETENTION_PROFILE_VERSION,
    SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    EndpointId,
    EndpointNotAdoptedError,
    resolve,
)
from tests.product_support import Collections, count, raw
from tests.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    ReadyItem,
    draft,
    establish,
    no_match,
    preparation,
    prepared,
    ready_final,
    ready_item,
    ready_tiered,
    request,
)

pytestmark = pytest.mark.integration

ORIGIN = EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2
PRODUCT_NO = "9900112233"


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


def _builder(container: Container, prep: Preparation) -> RegistrationSnapshotBuilder:
    return RegistrationSnapshotBuilder(
        preflight=prep.service, registrations=container.registrations
    )


def _frozen(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    items: list[ReadyItem] | None = None,
    shape: ListingShape = ListingShape.SEPARATE_LISTINGS,
) -> tuple[dict[str, Any], PreflightResult]:
    """One real Snapshot: its frozen payload and the final READY preflight it came from."""
    chosen = items or [ready_item(container, sources, "1234")]
    draft_id = draft(store, account, chosen, shape)
    req = request(store, draft_id, account, chosen)
    _req, final = ready_final(prep_of(container, account), req)
    snapshot = _builder(container, preparation(container, account)).freeze(
        final, created_by=OPERATOR, correlation_id=CID
    )
    assert snapshot.registration_snapshot_id
    return dict(build_payload(final).payload), final


def prep_of(container: Container, account: str) -> Preparation:
    return preparation(container, account)


def _readback_body(payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """What a provider read-back would carry for exactly this Snapshot, in an envelope the packet
    does not prove — the normalizer must recognize the proven leaves wherever they sit."""
    codes = product.seller_codes(payload)
    prices = {int(item["sale_price_krw"]) for item in payload["items"]}
    node: dict[str, Any] = {
        "name": payload["name"]["value"],
        "salePrice": min(prices),
        "stockQuantity": 5,
        "sellerCodeInfo": {"sellerManagementCode": codes.seller_management_code},
        "images": [
            {"url": asset["provider_asset_ref"]}
            for item in payload["items"]
            for asset in item["publication_assets"]
        ],
        "optionCombinations": [{"sellerManagerCode": code} for code in codes.option_codes],
        "detailContent": "<p>provider copy</p>",
    }
    node.update(overrides)
    return {"originProduct": node, "traceId": "trace-1"}


def _read(body: dict[str, Any]) -> Any:
    """One adopted read-back through a fake transport; returns the retained response."""
    caller = SmartStoreEndpointCaller(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    return caller.call(ORIGIN, ProductReadRequest("fixture-token-a1", 3, 7, PRODUCT_NO)).retained


# ---------------------------------------------------------------- the adopted surface


def test_adoption_is_bounded_and_every_gap_is_recorded() -> None:
    assert SMARTSTORE_ENDPOINT_MAPPING_REVISION == "m5-image-upload-r1"
    assert SAFE_RETENTION_PROFILE_VERSION == "smartstore-safe-retention/v1"
    assert [c.endpoint_id.value for c in ADOPTED.values() if c.mutating] == [
        "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"
    ]
    for endpoint in (
        EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
        EndpointId.SMARTSTORE_PRODUCT_SEARCH,
    ):
        with pytest.raises(EndpointNotAdoptedError):
            resolve(endpoint)


def test_the_capability_stays_unverified_for_product_write(container: Container) -> None:
    # PR-D §2: adoption creates no mutation authority. The real SmartStore capability owner still
    # reports write UNVERIFIED after the read-backs were adopted.
    view = container.marketplace_capability.capability("smartstore")
    assert view.write.status.value == "UNVERIFIED"


def test_the_adapter_satisfies_the_register_side_ports() -> None:
    # REGISTER sees the adapter only through Protocols; it imports no provider module itself.
    assert isinstance(lookup.SmartStoreDuplicateLookup(), ports.DuplicateLookupSource)
    assert isinstance(assets, ports.ProviderAssetSource)
    assert isinstance(readback, ports.ReadbackComparator)


# ---------------------------------------------------------------- Snapshot → wire → read-back


def test_a_real_snapshot_projects_and_reads_back_as_a_match(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    projected = product.project(payload)
    # The seller-controlled identities are the Snapshot's own stable identities.
    assert projected.codes.seller_management_code == payload["listing_identity"]
    assert projected.codes.option_codes == tuple(
        item["registration_item_key"] for item in payload["items"]
    )
    comparison = readback.compare(payload, _read(_readback_body(payload)))
    assert comparison.verdict is readback.ReadbackVerdict.MATCH
    assert comparison.reasons == ()


def test_the_same_snapshot_projects_identically_every_time(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    first, second = product.project(payload), product.project(dict(payload))
    assert first.proven == second.proven
    assert first.codes == second.codes


def test_a_mutable_display_name_never_moves_the_identities(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    renamed = {**payload, "name": {"value": "완전히 다른 이름", "provenance": "OPERATOR_CONFIRMED"}}
    assert product.seller_codes(renamed) == product.seller_codes(payload)
    # The provider listing that still carries the frozen name is a name MISMATCH, not a new unit.
    comparison = readback.compare(renamed, _read(_readback_body(payload)))
    assert comparison.verdict is readback.ReadbackVerdict.MISMATCH
    assert comparison.reasons == ("NAME_MISMATCH",)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"name": "다른 이름"}, "NAME_MISMATCH"),
        ({"salePrice": 12345}, "SALE_PRICE_MISMATCH"),
        (
            {"sellerCodeInfo": {"sellerManagementCode": "icbm-" + "f" * 32}},
            ("SELLER_MANAGEMENT_CODE_MISMATCH"),
        ),
        (
            {"optionCombinations": [{"sellerManagerCode": "rik1-" + "e" * 32}]},
            "OPTION_UNITS_MISSING",
        ),
    ],
)
def test_every_difference_from_the_frozen_snapshot_is_a_mismatch(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    override: dict[str, Any],
    reason: str,
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    comparison = readback.compare(payload, _read(_readback_body(payload, **override)))
    assert comparison.verdict is readback.ReadbackVerdict.MISMATCH
    assert reason in comparison.reasons


def test_a_one_item_listing_needs_no_option_unit_at_the_provider(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    # A SEPARATE listing is one Item; whether the provider exposes an option unit for it is not
    # proven either way, so its absence is not a mismatch — the listing's own code is the identity.
    payload, _final = _frozen(container, sources, store, account)
    comparison = readback.compare(payload, _read(_readback_body(payload, optionCombinations=[])))
    assert comparison.verdict is readback.ReadbackVerdict.MATCH
    # A unit that is not this Snapshot's is still unexpected.
    foreign = _readback_body(
        payload, optionCombinations=[{"sellerManagerCode": "rik1-" + "e" * 32}]
    )
    assert "OPTION_UNITS_MISSING" in readback.compare(payload, _read(foreign)).reasons


def test_a_single_listing_read_back_missing_an_option_is_one_whole_listing_mismatch(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    items = list(ready_tiered(container, sources, "1234").values())
    payload, _final = _frozen(
        container, sources, store, account, items, ListingShape.SINGLE_LISTING_WITH_OPTIONS
    )
    keys = [item["registration_item_key"] for item in payload["items"]]
    assert len(keys) > 1
    partial = _readback_body(payload, optionCombinations=[{"sellerManagerCode": keys[0]}])
    comparison = readback.compare(payload, _read(partial))
    # R3: one mismatch for the whole listing; the absent unit is never sent as a second CREATE.
    assert comparison.verdict is readback.ReadbackVerdict.MISMATCH
    assert comparison.missing_option_codes == tuple(sorted(keys[1:]))
    assert "OPTION_UNITS_UNEXPECTED" not in comparison.reasons


def test_separate_listings_read_back_one_item_per_listing(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    assert payload["listing_shape"] == ListingShape.SEPARATE_LISTINGS.value
    assert len(payload["items"]) == 1
    comparison = readback.compare(payload, _read(_readback_body(payload)))
    assert comparison.verdict is readback.ReadbackVerdict.MATCH


# ---------------------------------------------------------------- confirmation and evidence


def test_a_2xx_read_that_carries_no_product_is_never_a_confirmation(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    # The endpoint predicate passes (200, JSON object) but nothing recognizable came back.
    comparison = readback.compare(payload, _read({"originProduct": {"detailContent": "x"}}))
    assert comparison.verdict is readback.ReadbackVerdict.UNREADABLE
    assert comparison.verdict is not readback.ReadbackVerdict.MATCH


def test_the_comparison_evidence_is_sanitized_and_versioned(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    signed = "https://cdn.example/a.jpg?X-Signature=abc"
    comparison = readback.compare(payload, _read(_readback_body(payload, images=[{"url": signed}])))
    evidence = json.dumps(comparison.canonical(), ensure_ascii=False)
    assert signed not in evidence and "X-Signature" not in evidence
    assert comparison.comparison_contract_version == "smartstore-readback-comparison/v1"
    assert comparison.normalizer_version == "smartstore-readback-normalizer/v1"
    assert "IMAGE_REFERENCE_UNSAFE" in comparison.reasons


def test_no_provider_value_outside_the_retention_profile_can_be_stored(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
) -> None:
    payload, _final = _frozen(container, sources, store, account)
    retained = _read(_readback_body(payload, detailContent="<p>provider copy</p>"))
    assert "detailContent" not in json.dumps(retained)
    # Nothing the adapter produced was written: PR-D performs no execution at all.
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM registration_intents").fetchone()
    assert rows[0] == 0
    assert count(config, "marketplace_registrations") == 0


# ---------------------------------------------------------------- fail-closed seams


def test_duplicate_lookup_stays_unavailable_and_preflight_therefore_needs_evidence(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    item = ready_item(container, sources, "1234")
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    source = lookup.SmartStoreDuplicateLookup()
    assert not source.available()
    with pytest.raises(AppError):
        source.evidence(marketplace_account_id=account, listing_identity="icbm-x")
    # With no adopted lookup there is no evidence, and PR-C refuses READY rather than assuming.
    result = prep_of(container, account).service.candidate(req)
    assert result.status is ReadinessStatus.REVIEW_REQUIRED
    assert "DUPLICATE_EVIDENCE_MISSING" in set(result.codes)
    # Operator-supplied evidence still works exactly as before.
    assert (
        prep_of(container, account)
        .service.candidate(replace(req, duplicate_evidence=no_match(result)))
        .status
        is ReadinessStatus.READY
    )


def test_an_ambiguous_upload_never_prepares_an_asset_for_a_real_candidate(
    container: Container, sources: Collections, store: RegistrationStore, account: str
) -> None:
    item = ready_item(container, sources, "1234")
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    first = prep_of(container, account).service.candidate(req)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep_of(container, account).service.candidate(req)
    assert candidate.upload_permitted
    (asset,) = prepared(candidate)
    ambiguous = assets.promote(
        {"images": []},
        asset_kind=asset.asset_kind,
        sha256=asset.sha256,
        derivation_id=asset.derivation_id,
        asset_profile=asset.asset_profile,
        candidate_fingerprint=candidate.candidate_fingerprint,
    )
    assert ambiguous.asset is None
    # Without the provider asset identity the final preflight cannot be READY, so nothing freezes.
    final = prep_of(container, account).service.final(req, ())
    assert final.status is not ReadinessStatus.READY
    assert "PROVIDER_ASSET_IDENTITY_MISSING" in set(final.codes)
