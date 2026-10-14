"""The C-LIVE-MUT-1 owner is bounded to test 1..11 and never retries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Credentials,
    IpAllowlistDeclaration,
    Market,
)
from integrations.marketplaces.coupang.canary import ClockPreflight
from integrations.marketplaces.coupang.live_mutations import (
    SCOPE_ID,
    TEST_PRODUCT_BINDINGS,
    CoupangAmbiguousCreateRecovery,
    CoupangCorrectedAmbiguousCreateCompletion,
    CoupangCorrectedRejectedTestProductMutationRun,
    CoupangLiveMutationError,
    CoupangRemainingTestProductMutationRun,
    CoupangTestProductMutationRun,
    LiveMutationTransportRefused,
    MutationJournal,
)
from integrations.marketplaces.coupang.live_mutations import (
    TestProductBundle as ProductBundle,
)
from integrations.marketplaces.coupang.live_mutations import (
    TestProductMutationApproval as MutationApproval,
)


@dataclass(frozen=True)
class Clock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
SOURCE_IP = "203.0.113.10"


def _create(index: int, source_product_id: str) -> dict[str, object]:
    return {
        "displayCategoryCode": 78877,
        "sellerProductName": f"ICBM TEST {index}",
        "vendorId": "A0001",
        "saleStartedAt": "2026-10-13T00:00:00",
        "saleEndedAt": "2099-01-01T23:59:59",
        "deliveryMethod": "SEQUENCIAL",
        "deliveryCompanyCode": "KDEXP",
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 2500,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "UNION_DELIVERY",
        "returnCenterCode": "100",
        "returnChargeName": "ICBM TEST RETURN",
        "companyContactNumber": "02-0000-0000",
        "returnZipCode": "00000",
        "returnAddress": "서울",
        "returnAddressDetail": "ICBM TEST",
        "returnCharge": 2500,
        "outboundShippingPlaceCode": "200",
        "vendorUserId": "wing-user",
        "requested": False,
        "items": [
            {
                "itemName": f"ICBM TEST {index}",
                "originalPrice": 10000,
                "salePrice": 9000,
                "maximumBuyCount": 10,
                "maximumBuyForPerson": 0,
                "maximumBuyForPersonPeriod": 1,
                "outboundShippingTimeDay": 1,
                "unitCount": 1,
                "adultOnly": "EVERYONE",
                "taxType": "TAX",
                "parallelImported": "NOT_PARALLEL_IMPORTED",
                "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
                "pccNeeded": "false",
                "externalVendorSku": source_product_id,
                "images": [
                    {
                        "imageOrder": 0,
                        "imageType": "REPRESENTATION",
                        "vendorPath": "https://images.example.com/icbm-test.jpg",
                    }
                ],
                "attributes": [{"attributeTypeName": "수량", "attributeValueName": "1개"}],
                "contents": [
                    {
                        "contentsType": "TEXT",
                        "contentDetails": [{"content": "ICBM TEST", "detailType": "TEXT"}],
                    }
                ],
            }
        ],
    }


def _bundle_mapping() -> dict[str, object]:
    return {
        "products": [
            {
                "test_name": binding.test_name,
                "synthetic_id": binding.synthetic_id,
                "source_product_id": binding.source_product_id,
                "template": binding.template,
                "create": _create(index, binding.source_product_id),
                "update_message": f"ICBM TEST {index} UPDATED",
            }
            for index, binding in enumerate(TEST_PRODUCT_BINDINGS, start=1)
        ]
    }


@pytest.mark.parametrize(
    ("location", "message"),
    [
        (
            {"cdnPath": "vendor-item-cdn.invalid/icbm-test.jpg"},
            "existing Coupang CDN path",
        ),
        ({"vendorPath": "http://images.example.com/test.jpg"}, "public HTTPS"),
        ({"vendorPath": "https://localhost/test.jpg"}, "public HTTPS"),
        (
            {
                "vendorPath": "https://images.example.com/test.jpg",
                "cdnPath": "vendor-item-cdn.coupangcdn.com/test.jpg",
            },
            "exactly one",
        ),
    ],
)
def test_bundle_rejects_unsafe_live_image_location(location: dict[str, str], message: str) -> None:
    bundle = deepcopy(_bundle_mapping())
    products = bundle["products"]
    assert isinstance(products, list)
    create = products[0]["create"]
    assert isinstance(create, dict)
    items = create["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["images"] = [{"imageOrder": 0, "imageType": "REPRESENTATION", **location}]

    with pytest.raises(ValueError, match=message):
        ProductBundle.from_mapping(bundle)


def _owner(tmp_path: Path, transport: httpx.BaseTransport) -> CoupangTestProductMutationRun:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(
            SOURCE_IP,
            credential_generation=1,
            recorded_at=NOW,
        ),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return CoupangTestProductMutationRun(
        context=context,
        approval=approval,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0.5, checked_at=NOW),
        journal=MutationJournal(tmp_path / "coupang-live-mutations.json"),
        transport=transport,
        environ={},
    )


def _recovery_owner(
    tmp_path: Path, transport: httpx.BaseTransport
) -> CoupangAmbiguousCreateRecovery:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(
            SOURCE_IP,
            credential_generation=1,
            recorded_at=NOW,
        ),
    )
    return CoupangAmbiguousCreateRecovery(
        context=context,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0.5, checked_at=NOW),
        journal=MutationJournal(tmp_path / "coupang-live-mutations.json"),
        transport=transport,
        environ={},
    )


def _continuation_owner(
    tmp_path: Path, transport: httpx.BaseTransport
) -> CoupangRemainingTestProductMutationRun:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(
            SOURCE_IP,
            credential_generation=1,
            recorded_at=NOW,
        ),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return CoupangRemainingTestProductMutationRun(
        context=context,
        approval=approval,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0.5, checked_at=NOW),
        journal=MutationJournal(tmp_path / "coupang-live-mutations.json"),
        transport=transport,
        environ={},
    )


def _corrected_owner(
    tmp_path: Path, transport: httpx.BaseTransport, prior_digest: str
) -> CoupangCorrectedRejectedTestProductMutationRun:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(
            SOURCE_IP,
            credential_generation=1,
            recorded_at=NOW,
        ),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return CoupangCorrectedRejectedTestProductMutationRun(
        context=context,
        approval=approval,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0.5, checked_at=NOW),
        journal=MutationJournal(tmp_path / "coupang-live-mutations.json"),
        transport=transport,
        environ={},
        prior_payload_digest=prior_digest,
    )


def _corrected_recovery_owner(
    tmp_path: Path, transport: httpx.BaseTransport, prior_digest: str
) -> CoupangCorrectedAmbiguousCreateCompletion:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(
            SOURCE_IP,
            credential_generation=1,
            recorded_at=NOW,
        ),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return CoupangCorrectedAmbiguousCreateCompletion(
        context=context,
        approval=approval,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0.5, checked_at=NOW),
        journal=MutationJournal(tmp_path / "coupang-live-mutations.json"),
        transport=transport,
        environ={},
        prior_payload_digest=prior_digest,
    )


def _seed_corrected_completed_journal(tmp_path: Path, prior_digest: str) -> None:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    records = {
        binding.synthetic_id: {
            "state": "deleted",
            "seller_product_id": 2000 + index,
        }
        for index, binding in enumerate(TEST_PRODUCT_BINDINGS, start=1)
    }
    records[TEST_PRODUCT_BINDINGS[0].synthetic_id] = {
        "state": "create_inflight",
        "payload_digest": prior_digest,
    }
    MutationJournal(tmp_path / "coupang-live-mutations.json").commit(
        {
            "version": 2,
            "scope_id": SCOPE_ID,
            "payload_digest": bundle.payload_digest,
            "prior_payload_digest": prior_digest,
            "credential_generation": 1,
            "records": records,
        }
    )


def _seed_resumed_journal(tmp_path: Path, *, phase: str, seller_product_id: int) -> None:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    records = {
        binding.synthetic_id: {
            "state": "deleted",
            "seller_product_id": 2000 + index,
        }
        for index, binding in enumerate(TEST_PRODUCT_BINDINGS, start=1)
    }
    records[TEST_PRODUCT_BINDINGS[0].synthetic_id] = {
        "state": phase,
        "seller_product_id": seller_product_id,
    }
    MutationJournal(tmp_path / "coupang-live-mutations.json").commit(
        {
            "version": 1,
            "scope_id": SCOPE_ID,
            "payload_digest": bundle.payload_digest,
            "credential_generation": 1,
            "records": records,
        }
    )


def test_exact_eleven_create_update_readback_delete_once_each(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    created: dict[int, tuple[str, str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            document = json.loads(request.content)
            identity = 1000 + len(created) + 1
            created[identity] = (
                document["items"][0]["externalVendorSku"],
                document["sellerProductName"],
                f"ICBM TEST {identity - 1000} UPDATED",
            )
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "message": "",
                    "data": {"code": "SUCCESS", "message": "", "data": identity},
                },
            )
        if request.method == "PUT":
            document = json.loads(request.content)
            identity = document["sellerProductId"]
            assert document["sellerProductName"] == f"ICBM TEST {identity - 1000} UPDATED"
            assert document["requested"] is False
            assert document["items"][0]["sellerProductItemId"] == 3000 + identity
            assert document["items"][0]["vendorItemId"] is None
            source_product_id, _, update_message = created[identity]
            created[identity] = (source_product_id, update_message, update_message)
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "message": "",
                    "data": {"code": "SUCCESS", "message": "", "data": identity},
                },
            )
        identity = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            source_product_id, seller_product_name, update_message = created[identity]
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "message": "",
                    "data": {
                        "sellerProductId": identity,
                        "sellerProductName": seller_product_name,
                        "extraInfoMessage": update_message,
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [
                            {
                                "externalVendorSku": source_product_id,
                                "sellerProductItemId": 3000 + identity,
                                "vendorItemId": None,
                            }
                        ],
                    },
                },
            )
        assert request.method == "DELETE"
        return httpx.Response(200, json={"code": "SUCCESS", "message": "", "data": None})

    owner = _owner(tmp_path, httpx.MockTransport(handler))
    result = owner.run()

    assert (result.scope_id, result.completed, result.total) == (SCOPE_ID, 11, 11)
    assert len(requests) == 55
    assert [request.method for request in requests[:5]] == [
        "POST",
        "GET",
        "PUT",
        "GET",
        "DELETE",
    ]
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert all(record["state"] == "deleted" for record in persisted["records"].values())


def test_bundle_rejects_scope_expansion_and_approval_requests() -> None:
    missing = _bundle_mapping()
    products = missing["products"]
    assert isinstance(products, list)
    products.pop()
    with pytest.raises(ValueError, match="exactly test 1 through test 11"):
        ProductBundle.from_mapping(missing)

    approval = _bundle_mapping()
    rows = approval["products"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    create = rows[0]["create"]
    assert isinstance(create, dict)
    create["requested"] = True
    with pytest.raises(ValueError, match="forbids requesting product approval"):
        ProductBundle.from_mapping(approval)


def test_preflight_rejects_body_vendor_mismatch(tmp_path: Path) -> None:
    mapping = _bundle_mapping()
    rows = mapping["products"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    create = rows[0]["create"]
    assert isinstance(create, dict)
    create["vendorId"] = "DIFFERENT"
    bundle = ProductBundle.from_mapping(mapping)
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(SOURCE_IP, credential_generation=1, recorded_at=NOW),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    owner = CoupangTestProductMutationRun(
        context=context,
        approval=approval,
        bundle=bundle,
        clock=Clock(NOW),
        clock_preflight=ClockPreflight.create(0, checked_at=NOW),
        journal=MutationJournal(tmp_path / "journal.json"),
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        environ={},
    )
    with pytest.raises(CoupangLiveMutationError) as refused:
        owner.run()
    assert refused.value.code == "COUPANG_MUTATION_VENDOR_BODY_MISMATCH"


def test_ambiguous_dispatch_persists_barrier_and_never_blindly_retries(
    tmp_path: Path,
) -> None:
    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError) as first:
        _owner(tmp_path, httpx.MockTransport(timeout)).run()
    assert first.value.code == "COUPANG_MUTATION_TRANSPORT_AMBIGUOUS"
    assert calls == 1

    with pytest.raises(CoupangLiveMutationError) as resumed:
        _owner(tmp_path, httpx.MockTransport(timeout)).run()
    assert resumed.value.code == "COUPANG_MUTATION_RECOVERY_REQUIRED"
    assert calls == 1


def test_continuation_quarantines_test_one_and_finishes_tests_two_through_eleven(
    tmp_path: Path,
) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    requests: list[httpx.Request] = []
    created: dict[int, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            document = json.loads(request.content)
            index = int(document["sellerProductName"].removeprefix("ICBM TEST "))
            identity = 1000 + index
            created[identity] = (
                document["items"][0]["externalVendorSku"],
                document["sellerProductName"],
            )
            return httpx.Response(200, json={"code": "SUCCESS", "data": identity})
        if request.method == "PUT":
            document = json.loads(request.content)
            identity = document["sellerProductId"]
            source_product_id, _ = created[identity]
            created[identity] = (source_product_id, document["sellerProductName"])
            return httpx.Response(200, json={"code": "SUCCESS", "data": identity})
        identity = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            source_product_id, seller_product_name = created[identity]
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {
                        "sellerProductId": identity,
                        "sellerProductName": seller_product_name,
                        "extraInfoMessage": seller_product_name,
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [
                            {
                                "externalVendorSku": source_product_id,
                                "sellerProductItemId": 3000 + identity,
                                "vendorItemId": None,
                            }
                        ],
                    },
                },
            )
        assert request.method == "DELETE"
        return httpx.Response(200, json={"code": "SUCCESS", "data": None})

    result = _continuation_owner(tmp_path, httpx.MockTransport(handler)).run()

    assert (result.completed, result.attempted) == (10, 10)
    assert [(issue.test_name, issue.journal_state) for issue in result.quarantined] == [
        ("test 1", "create_inflight")
    ]
    assert len(requests) == 50
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert persisted["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id] == {
        "state": "create_inflight"
    }
    assert all(
        persisted["records"][binding.synthetic_id]["state"] == "deleted"
        for binding in TEST_PRODUCT_BINDINGS[1:]
    )


def test_continuation_does_not_retry_failed_item_and_keeps_going(tmp_path: Path) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    requests: list[httpx.Request] = []
    created: dict[int, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            document = json.loads(request.content)
            index = int(document["sellerProductName"].removeprefix("ICBM TEST "))
            if index == 2:
                return httpx.Response(200, json={"code": "SUCCESS", "data": None})
            identity = 1000 + index
            created[identity] = (
                document["items"][0]["externalVendorSku"],
                document["sellerProductName"],
            )
            return httpx.Response(200, json={"code": "SUCCESS", "data": identity})
        if request.method == "PUT":
            document = json.loads(request.content)
            identity = document["sellerProductId"]
            source_product_id, _ = created[identity]
            created[identity] = (source_product_id, document["sellerProductName"])
            return httpx.Response(200, json={"code": "SUCCESS", "data": identity})
        identity = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            source_product_id, seller_product_name = created[identity]
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {
                        "sellerProductId": identity,
                        "sellerProductName": seller_product_name,
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [
                            {
                                "externalVendorSku": source_product_id,
                                "sellerProductItemId": 3000 + identity,
                                "vendorItemId": None,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(200, json={"code": "SUCCESS", "data": None})

    result = _continuation_owner(tmp_path, httpx.MockTransport(handler)).run()

    assert (result.completed, result.attempted) == (9, 10)
    assert [(issue.test_name, issue.journal_state, issue.code) for issue in result.quarantined] == [
        ("test 1", "create_inflight", "COUPANG_MUTATION_AMBIGUOUS_CREATE_QUARANTINED"),
        ("test 2", "create_inflight", "COUPANG_MUTATION_CREATE_IDENTITY_MISSING"),
    ]
    assert len(requests) == 46
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert persisted["records"][TEST_PRODUCT_BINDINGS[1].synthetic_id] == {
        "state": "create_inflight"
    }
    assert persisted["records"][TEST_PRODUCT_BINDINGS[2].synthetic_id]["state"] == "deleted"


def test_continuation_requires_exact_test_one_barrier_and_ten_pending_records(
    tmp_path: Path,
) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    journal_path = tmp_path / "coupang-live-mutations.json"
    state = json.loads(journal_path.read_text())
    state["records"][TEST_PRODUCT_BINDINGS[1].synthetic_id] = {
        "state": "deleted",
        "seller_product_id": 9999,
    }
    MutationJournal(journal_path).commit(state)
    calls = 0

    def forbidden(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(CoupangLiveMutationError) as refused:
        _continuation_owner(tmp_path, httpx.MockTransport(forbidden)).run()

    assert refused.value.code == "COUPANG_MUTATION_CONTINUATION_SCOPE_INVALID"
    assert calls == 0


def _seed_prior_rejections(tmp_path: Path, prior_digest: str) -> None:
    MutationJournal(tmp_path / "coupang-live-mutations.json").commit(
        {
            "version": 1,
            "scope_id": SCOPE_ID,
            "payload_digest": prior_digest,
            "credential_generation": 1,
            "records": {
                TEST_PRODUCT_BINDINGS[0].synthetic_id: {"state": "create_inflight"},
                **{
                    binding.synthetic_id: {"state": "create_rejected"}
                    for binding in TEST_PRODUCT_BINDINGS[1:]
                },
            },
        }
    )


def test_corrected_payload_retries_only_explicit_old_rejections_once(tmp_path: Path) -> None:
    prior_digest = "a" * 64
    _seed_prior_rejections(tmp_path, prior_digest)
    requests: list[httpx.Request] = []

    def rejected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": "ERROR",
                "errorCode": "IMAGE_INVALID",
                "message": "new payload remains invalid",
            },
        )

    result = _corrected_owner(tmp_path, httpx.MockTransport(rejected), prior_digest).run()

    assert (result.completed, result.attempted) == (0, 10)
    assert len(requests) == 10
    assert all(request.method == "POST" for request in requests)
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert persisted["version"] == 2
    assert persisted["prior_payload_digest"] == prior_digest
    assert persisted["payload_digest"] == result.payload_digest
    assert persisted["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id] == {
        "state": "create_inflight",
        "payload_digest": prior_digest,
    }
    assert all(
        persisted["records"][binding.synthetic_id]["state"] == "create_rejected"
        for binding in TEST_PRODUCT_BINDINGS[1:]
    )


def test_corrected_payload_refuses_any_old_state_that_was_not_explicit_rejection(
    tmp_path: Path,
) -> None:
    prior_digest = "b" * 64
    _seed_prior_rejections(tmp_path, prior_digest)
    path = tmp_path / "coupang-live-mutations.json"
    state = json.loads(path.read_text())
    state["records"][TEST_PRODUCT_BINDINGS[1].synthetic_id] = {"state": "pending"}
    MutationJournal(path).commit(state)
    calls = 0

    def forbidden(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(CoupangLiveMutationError) as refused:
        _corrected_owner(tmp_path, httpx.MockTransport(forbidden), prior_digest).run()

    assert refused.value.code == "COUPANG_CORRECTED_JOURNAL_SCOPE_INVALID"
    assert calls == 0


def test_corrected_recovery_never_recreates_and_completes_test_one(tmp_path: Path) -> None:
    prior_digest = "c" * 64
    _seed_corrected_completed_journal(tmp_path, prior_digest)
    requests: list[httpx.Request] = []
    updated = False
    first = TEST_PRODUCT_BINDINGS[0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal updated
        requests.append(request)
        if "external-vendor-sku-codes" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": [
                        {
                            "vendorId": "A0001",
                            "sellerProductName": "ICBM TEST 1",
                            "sellerProductId": 1001,
                        }
                    ],
                },
            )
        if request.method == "PUT":
            document = json.loads(request.content)
            assert document["sellerProductId"] == 1001
            assert document["sellerProductName"] == "ICBM TEST 1 UPDATED"
            assert document["items"][0]["externalVendorSku"] == first.source_product_id
            updated = True
            return httpx.Response(200, json={"code": "SUCCESS", "data": 1001})
        if request.method == "DELETE":
            return httpx.Response(200, json={"code": "SUCCESS", "data": None})
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": 1001,
                    "sellerProductName": "ICBM TEST 1 UPDATED" if updated else "ICBM TEST 1",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [
                        {
                            "externalVendorSku": first.source_product_id,
                            "sellerProductItemId": 3001,
                            "vendorItemId": None,
                        }
                    ],
                },
            },
        )

    result = _corrected_recovery_owner(tmp_path, httpx.MockTransport(handler), prior_digest).run()

    assert (result.completed, result.total) == (1, 1)
    assert [request.method for request in requests] == ["GET", "GET", "PUT", "GET", "DELETE"]
    assert "POST" not in {request.method for request in requests}
    state = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert state["records"][first.synthetic_id] == {
        "state": "deleted",
        "seller_product_id": 1001,
    }


def test_corrected_recovery_zero_match_preserves_ambiguity_and_never_creates(
    tmp_path: Path,
) -> None:
    prior_digest = "d" * 64
    _seed_corrected_completed_journal(tmp_path, prior_digest)
    requests: list[httpx.Request] = []

    def empty(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": "SUCCESS", "data": []})

    with pytest.raises(CoupangLiveMutationError) as refused:
        _corrected_recovery_owner(tmp_path, httpx.MockTransport(empty), prior_digest).run()

    assert refused.value.code == "COUPANG_MUTATION_RECOVERY_NO_POSITIVE_MATCH"
    assert [request.method for request in requests] == ["GET"]
    state = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    assert state["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id] == {
        "state": "create_inflight",
        "payload_digest": prior_digest,
    }


def test_documented_create_rejection_is_not_recorded_as_ambiguous(tmp_path: Path) -> None:
    calls = 0

    def rejected(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"code": "ERROR", "message": "validation failed", "data": None},
        )

    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(rejected)).run()

    assert refused.value.error_class.value == "VALIDATION"
    assert refused.value.code == "COUPANG_MUTATION_PROVIDER_REJECTED"
    assert refused.value.provider_message == "validation failed"
    assert calls == 1
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    first = persisted["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id]
    assert first == {"state": "create_rejected", "provider_message": "validation failed"}


def test_provider_rejection_keeps_only_bounded_redacted_diagnostics(tmp_path: Path) -> None:
    submitted_name = "ICBM TEST 1"
    submitted_sku = TEST_PRODUCT_BINDINGS[0].source_product_id

    def rejected(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "ERROR",
                "data": {
                    "code": "ERROR",
                    "errorCode": "PRODUCT_VALIDATION_42",
                    "message": f"{submitted_name} has an invalid image for {submitted_sku}",
                    "invalidInputs": [
                        {
                            "name": "items[0].images[0].cdnPath",
                            "type": "INVALID",
                            "message": f"value from {submitted_sku} is not accepted",
                        }
                    ],
                },
            },
        )

    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(rejected)).run()

    assert refused.value.provider_code == "PRODUCT_VALIDATION_42"
    assert refused.value.provider_message == ("[redacted] has an invalid image for [redacted]")
    assert refused.value.provider_invalid_input == (
        "items[0].images[0].cdnPath: INVALID: value from [redacted] is not accepted"
    )
    persisted_text = (tmp_path / "coupang-live-mutations.json").read_text()
    assert submitted_name not in persisted_text
    assert submitted_sku not in persisted_text
    first = json.loads(persisted_text)["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id]
    assert first == {
        "state": "create_rejected",
        "provider_code": "PRODUCT_VALIDATION_42",
        "provider_message": "[redacted] has an invalid image for [redacted]",
        "provider_invalid_input": (
            "items[0].images[0].cdnPath: INVALID: value from [redacted] is not accepted"
        ),
    }


def test_provider_diagnostic_drops_authorization_shaped_messages(tmp_path: Path) -> None:
    def rejected(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "ERROR",
                "message": "Authorization Bearer should never survive",
                "data": None,
            },
        )

    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(rejected)).run()

    assert refused.value.provider_message is None
    first = json.loads((tmp_path / "coupang-live-mutations.json").read_text())["records"][
        TEST_PRODUCT_BINDINGS[0].synthetic_id
    ]
    assert first == {"state": "create_rejected"}


def test_positive_only_recovery_promotes_one_exact_match_to_created(tmp_path: Path) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    requests: list[httpx.Request] = []
    binding = TEST_PRODUCT_BINDINGS[0]

    def recover(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "external-vendor-sku-codes" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": [
                        {
                            "sellerProductId": 1001,
                            "sellerProductName": "ICBM TEST 1",
                            "vendorId": "A0001",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": 1001,
                    "sellerProductName": "ICBM TEST 1",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [
                        {
                            "externalVendorSku": binding.source_product_id,
                            "sellerProductItemId": 4001,
                            "vendorItemId": None,
                        }
                    ],
                },
            },
        )

    result = _recovery_owner(tmp_path, httpx.MockTransport(recover)).recover()

    assert (result.test_name, result.recovered) == ("test 1", True)
    assert [request.method for request in requests] == ["GET", "GET"]
    assert requests[0].url.path.endswith(binding.source_product_id)
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    first = persisted["records"][binding.synthetic_id]
    assert first == {"state": "created", "seller_product_id": 1001}


@pytest.mark.parametrize(
    ("candidates", "expected_code"),
    [
        ([], "COUPANG_MUTATION_RECOVERY_NO_POSITIVE_MATCH"),
        (
            [
                {
                    "sellerProductId": 1001,
                    "sellerProductName": "ICBM TEST 1",
                    "vendorId": "A0001",
                },
                {
                    "sellerProductId": 1002,
                    "sellerProductName": "ICBM TEST 1",
                    "vendorId": "A0001",
                },
            ],
            "COUPANG_MUTATION_RECOVERY_MULTIPLE_MATCHES",
        ),
    ],
)
def test_recovery_never_treats_zero_or_multiple_matches_as_identity(
    tmp_path: Path,
    candidates: list[dict[str, object]],
    expected_code: str,
) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    with pytest.raises(CoupangLiveMutationError) as refused:
        _recovery_owner(
            tmp_path,
            httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": "SUCCESS", "data": candidates})
            ),
        ).recover()

    assert refused.value.code == expected_code
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    first = persisted["records"][TEST_PRODUCT_BINDINGS[0].synthetic_id]
    assert first == {"state": "create_inflight"}


def test_recovery_candidate_or_readback_mismatch_never_advances_journal(
    tmp_path: Path,
) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    binding = TEST_PRODUCT_BINDINGS[0]

    def mismatched_readback(request: httpx.Request) -> httpx.Response:
        if "external-vendor-sku-codes" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": [
                        {
                            "sellerProductId": 1001,
                            "sellerProductName": "ICBM TEST 1",
                            "vendorId": "A0001",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": 1001,
                    "sellerProductName": "ICBM TEST 1",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [
                        {
                            "externalVendorSku": binding.source_product_id,
                            "sellerProductItemId": 4001,
                            "vendorItemId": None,
                        },
                        {
                            "externalVendorSku": "unexpected-second-item",
                            "sellerProductItemId": 4002,
                            "vendorItemId": None,
                        },
                    ],
                },
            },
        )

    with pytest.raises(CoupangLiveMutationError) as refused:
        _recovery_owner(
            tmp_path,
            httpx.MockTransport(mismatched_readback),
        ).recover()

    assert refused.value.code == "COUPANG_MUTATION_READBACK_SCOPE_MISMATCH"
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    first = persisted["records"][binding.synthetic_id]
    assert first == {"state": "create_inflight"}


def test_recovery_requires_provider_assigned_item_identity(tmp_path: Path) -> None:
    def ambiguous(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous", request=request)

    with pytest.raises(CoupangLiveMutationError):
        _owner(tmp_path, httpx.MockTransport(ambiguous)).run()

    binding = TEST_PRODUCT_BINDINGS[0]

    def incomplete(request: httpx.Request) -> httpx.Response:
        if "external-vendor-sku-codes" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": [
                        {
                            "sellerProductId": 1001,
                            "sellerProductName": "ICBM TEST 1",
                            "vendorId": "A0001",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": 1001,
                    "sellerProductName": "ICBM TEST 1",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [
                        {
                            "externalVendorSku": binding.source_product_id,
                            "vendorItemId": None,
                        }
                    ],
                },
            },
        )

    with pytest.raises(CoupangLiveMutationError) as refused:
        _recovery_owner(tmp_path, httpx.MockTransport(incomplete)).recover()

    assert refused.value.code == "COUPANG_MUTATION_UPDATE_ITEM_IDENTITY_MISSING"
    persisted = json.loads((tmp_path / "coupang-live-mutations.json").read_text())
    first = persisted["records"][binding.synthetic_id]
    assert first == {"state": "create_inflight"}


def test_duplicate_create_identity_holds_before_second_product_mutation(tmp_path: Path) -> None:
    calls: list[str] = []
    first_source = TEST_PRODUCT_BINDINGS[0].source_product_id
    current_name = "ICBM TEST 1"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_name
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"code": "SUCCESS", "data": 1001})
        if request.method == "PUT":
            current_name = "ICBM TEST 1 UPDATED"
            return httpx.Response(200, json={"code": "SUCCESS", "data": None})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {
                        "sellerProductId": 1001,
                        "sellerProductName": current_name,
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [
                            {
                                "externalVendorSku": first_source,
                                "sellerProductItemId": 4001,
                                "vendorItemId": None,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(200, json={"code": "SUCCESS", "data": None})

    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(handler)).run()
    assert refused.value.code == "COUPANG_MUTATION_DUPLICATE_CREATE_IDENTITY"
    assert calls == ["POST", "GET", "PUT", "GET", "DELETE", "POST"]


def test_resumed_created_identity_is_reverified_before_update(tmp_path: Path) -> None:
    seller_product_id = 1001
    source_product_id = TEST_PRODUCT_BINDINGS[0].source_product_id
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            seller_product_name = "ICBM TEST 1 UPDATED" if "PUT" in methods else "ICBM TEST 1"
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {
                        "sellerProductId": seller_product_id,
                        "sellerProductName": seller_product_name,
                        "extraInfoMessage": "ICBM TEST 1 UPDATED",
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [
                            {
                                "externalVendorSku": source_product_id,
                                "sellerProductItemId": 4001,
                                "vendorItemId": None,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(200, json={"code": "SUCCESS", "data": None})

    _seed_resumed_journal(
        tmp_path,
        phase="created",
        seller_product_id=seller_product_id,
    )
    result = _owner(tmp_path, httpx.MockTransport(handler)).run()

    assert result.completed == 11
    assert methods == ["GET", "PUT", "GET", "DELETE"]


def test_resumed_created_scope_mismatch_holds_before_update(tmp_path: Path) -> None:
    seller_product_id = 1001
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": seller_product_id,
                    "sellerProductName": "AN EXISTING COUPANG PRODUCT",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [{"externalVendorSku": "existing-product"}],
                },
            },
        )

    _seed_resumed_journal(
        tmp_path,
        phase="created",
        seller_product_id=seller_product_id,
    )
    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(handler)).run()

    assert refused.value.code == "COUPANG_MUTATION_READBACK_NAME_MISMATCH"
    assert methods == ["GET"]


def test_resumed_verified_scope_mismatch_holds_before_delete(tmp_path: Path) -> None:
    seller_product_id = 1001
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {
                    "sellerProductId": seller_product_id,
                    "sellerProductName": "ICBM TEST 1 UPDATED",
                    "extraInfoMessage": "ICBM TEST 1 UPDATED",
                    "vendorId": "A0001",
                    "status": "SAVED",
                    "items": [{"externalVendorSku": "existing-product"}],
                },
            },
        )

    _seed_resumed_journal(
        tmp_path,
        phase="verified",
        seller_product_id=seller_product_id,
    )
    with pytest.raises(CoupangLiveMutationError) as refused:
        _owner(tmp_path, httpx.MockTransport(handler)).run()

    assert refused.value.code == "COUPANG_MUTATION_READBACK_SCOPE_MISMATCH"
    assert methods == ["GET"]


def test_resumed_verified_identity_is_reverified_before_delete(tmp_path: Path) -> None:
    seller_product_id = 1001
    source_product_id = TEST_PRODUCT_BINDINGS[0].source_product_id
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {
                        "sellerProductId": seller_product_id,
                        "sellerProductName": "ICBM TEST 1 UPDATED",
                        "extraInfoMessage": "ICBM TEST 1 UPDATED",
                        "vendorId": "A0001",
                        "status": "SAVED",
                        "items": [{"externalVendorSku": source_product_id}],
                    },
                },
            )
        return httpx.Response(200, json={"code": "SUCCESS", "data": None})

    _seed_resumed_journal(
        tmp_path,
        phase="verified",
        seller_product_id=seller_product_id,
    )
    result = _owner(tmp_path, httpx.MockTransport(handler)).run()

    assert result.completed == 11
    assert methods == ["GET", "DELETE"]


def test_real_transport_never_exists_under_tests(tmp_path: Path) -> None:
    bundle = ProductBundle.from_mapping(_bundle_mapping())
    credentials = Credentials("access", "secret", "A0001", 1)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(SOURCE_IP, credential_generation=1, recorded_at=NOW),
    )
    approval = MutationApproval.create(
        scope_id=SCOPE_ID,
        vendor_id="A0001",
        credential_generation=1,
        source_ip=SOURCE_IP,
        payload_digest=bundle.payload_digest,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(LiveMutationTransportRefused):
        CoupangTestProductMutationRun(
            context=context,
            approval=approval,
            bundle=bundle,
            clock=Clock(NOW),
            clock_preflight=ClockPreflight.create(0, checked_at=NOW),
            journal=MutationJournal(tmp_path / "journal.json"),
            transport=None,
        )
