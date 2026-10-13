"""The C-LIVE-MUT-1 owner is bounded to test 1..11 and never retries."""

from __future__ import annotations

import json
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
    CoupangLiveMutationError,
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
                        "cdnPath": "vendor-item-cdn.invalid/icbm-test.jpg",
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


def test_exact_eleven_create_update_readback_delete_once_each(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    created: dict[int, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            document = json.loads(request.content)
            identity = 1000 + len(created) + 1
            created[identity] = document["items"][0]["externalVendorSku"]
            return httpx.Response(200, json={"code": "SUCCESS", "message": "", "data": identity})
        identity_index = -2 if request.url.path.endswith("/partial") else -1
        identity = int(request.url.path.rsplit("/", 2)[identity_index])
        if request.method == "PUT":
            assert json.loads(request.content) == {
                "extraInfoMessage": f"ICBM TEST {identity - 1000} UPDATED",
                "sellerProductId": identity,
            }
            return httpx.Response(200, json={"code": "SUCCESS", "message": "", "data": None})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "message": "",
                    "data": {
                        "sellerProductId": identity,
                        "status": "SAVED",
                        "items": [{"externalVendorSku": created[identity]}],
                    },
                },
            )
        assert request.method == "DELETE"
        return httpx.Response(200, json={"code": "SUCCESS", "message": "", "data": None})

    owner = _owner(tmp_path, httpx.MockTransport(handler))
    result = owner.run()

    assert (result.scope_id, result.completed, result.total) == (SCOPE_ID, 11, 11)
    assert len(requests) == 44
    assert [request.method for request in requests[:4]] == ["POST", "PUT", "GET", "DELETE"]
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
