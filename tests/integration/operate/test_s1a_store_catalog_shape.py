"""The shape-only runtime capture of a store-catalog sync (ADR-0042 §5.1 item 3).

The real registry-gated caller and the real SmartStore catalog source answer through a fake
transport with synthetic values; no provider is reached. Proven here: an operator's 「지금 동기화」
records the shape of the raw search pages and origin reads — key paths and JSON types — and never
a value (a name, a price, a URL, a phone number, a product number); enum tokens are kept only at a
key named ``…Type``/``…Status``/``…Unit``/``…Method`` and only in the upper-case code form; a
periodic sync records nothing; the bounds hold; only the search and the origin read accept a body
sink; and what retention keeps is unchanged.
"""

import json
import sqlite3
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.stages.operate.catalog.facts import COMPLETE
from app.stages.operate.catalog.service import AUTO, OPERATOR, StoreCatalogService
from app.stages.operate.catalog.shape import (
    MAX_PATHS,
    MAX_TOKENS_PER_PATH,
    SHAPE_VERSION,
    ShapeRecorder,
    enum_token,
    json_type,
)
from integrations.marketplaces.smartstore.caller import (
    AccountRequest,
    ProductReadRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.catalog import SmartStoreCatalogSource
from integrations.marketplaces.smartstore.registry import EndpointId
from tests.support.jobs_support import FakeClock
from tests.unit.integrations.marketplaces.smartstore.test_smartstore_catalog import (
    BEARER,
    Bearer,
    Provider,
    item,
    ok,
    origin_read,
    page,
)

pytestmark = pytest.mark.integration

ACCOUNT = "mpa-fixture-shape"
CID = "cid-store-catalog-shape"
# Synthetic values that must never appear in a recorded shape.
ORIGIN_NO = 9_876_543_210
CHANNEL_NO = 1_234_567_890
VALUES = (
    "합성 상품 A",
    "12900",
    "https://shop-phinf.pstatic.net/fixture/main.jpg",
    "02-0000-0000",
    "합성 안내문",
    "8801234567893",
    str(ORIGIN_NO),
    str(CHANNEL_NO),
    "S-1",
    BEARER,
)


def _service(container: Container, provider: Provider) -> StoreCatalogService:
    return StoreCatalogService(
        db=container.db,
        clock=container.clock,
        source=SmartStoreCatalogSource(
            SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)), Bearer
        ),
        account_of=lambda key: ACCOUNT if key == "smartstore" else None,
        interval_s=1800,
        pause_s=0.0,
    )


def _provider() -> Provider:
    return Provider(
        [ok(page([item(ORIGIN_NO, CHANNEL_NO, "S-1")]))], {str(ORIGIN_NO): ok(origin_read())}
    )


def _stored(container: Container) -> list[tuple[Any, ...]]:
    with sqlite3.connect(container.config.database_path) as raw:
        return raw.execute(
            "SELECT full_read, search_shape_json, origin_shape_json FROM store_catalog_sync_runs"
            " ORDER BY started_at"
        ).fetchall()


def test_an_operator_sync_records_paths_and_types_and_never_a_value(
    container: Container,
) -> None:
    service = _service(container, _provider())
    run = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert run.outcome == COMPLETE and run.full_read
    ((full, search_text, origin_text),) = _stored(container)
    assert full and search_text and origin_text
    for text in (search_text, origin_text):
        for value in VALUES:
            assert value not in text, value
    search, origin = json.loads(search_text), json.loads(origin_text)
    assert search["version"] == origin["version"] == SHAPE_VERSION
    assert (search["responses"], origin["responses"]) == (1, 1)
    paths = search["paths"]
    assert paths["$"]["types"] == ["object"]
    assert paths["$.contents"]["types"] == ["array"]
    assert paths["$.contents[].originProductNo"]["types"] == ["number"]
    assert paths["$.contents[].channelProducts[].sellerManagementCode"]["types"] == ["string"]
    assert paths["$.last"]["types"] == ["boolean"]
    # An enum token: a code at a key named …Type.
    assert paths["$.contents[].channelProducts[].channelServiceType"]["enum_tokens"] == [
        "STOREFARM"
    ]
    read = origin["paths"]
    # Every leaf of the raw response is in the shape, the unretained ones included — that is the
    # evidence — while retention still keeps none of them.
    assert read["$.originProduct.customerBenefit.immediateDiscountPolicy.value"]["types"] == [
        "number"
    ]
    assert read["$.originProduct.detailContent"]["types"] == ["string"]
    assert read["$.originProduct.statusType"]["enum_tokens"] == ["SALE"]
    assert read["$.smartstoreChannelProduct.channelProductDisplayStatusType"]["enum_tokens"] == [
        "ON"
    ]
    assert read["$.originProduct.deliveryInfo.deliveryFee.deliveryFeeType"]["enum_tokens"] == [
        "CONDITIONAL_FREE"
    ]
    # Never a token at a key outside the suffix rule, however code-like its value.
    assert "enum_tokens" not in read["$.originProduct.leafCategoryId"]
    assert "enum_tokens" not in read["$.originProduct.name"]
    # The catalog fields read are exactly the retained ones: the capture widened nothing.
    view = service.listings()[0]
    assert view.fields is not None
    assert "customerBenefit" not in json.dumps(view.fields)
    # The read-only API and the status count expose it; the status carries no path itself.
    shape = service.shape()
    assert shape is not None and shape.run_id == run.run_id
    status = service.status()
    assert status.shape_path_count == shape.path_count > 0


def test_a_periodic_sync_records_no_shape(container: Container, clock: FakeClock) -> None:
    provider = _provider()
    service = _service(container, provider)
    assert service.sync(trigger=AUTO, correlation_id=CID).outcome == COMPLETE
    clock.advance(1800)
    service.sync(trigger=AUTO, correlation_id=CID)
    assert [row[0] for row in _stored(container)] == [0, 0]
    assert all(row[1] is None and row[2] is None for row in _stored(container))
    assert service.shape() is None and service.status().shape_path_count is None
    # The database refuses a shape on a periodic run.
    with (
        sqlite3.connect(container.config.database_path) as raw,
        pytest.raises(sqlite3.IntegrityError),
    ):
        raw.execute(
            "INSERT INTO store_catalog_sync_runs (run_id, marketplace_key,"
            " marketplace_account_id, trigger, state, contract_version, full_read,"
            " search_shape_json, pages_read, listings_enumerated, details_target,"
            " details_done, details_failed, correlation_id, started_at)"
            " VALUES ('r-x', 'smartstore', ?, 'AUTO', 'RUNNING', 'v1', 0, '{}', 0, 0, 0,"
            " 0, 0, 'c', '2026-09-13 00:00:00.000000')",
            (ACCOUNT,),
        )


def test_a_failed_page_is_recorded_as_shape_too(container: Container) -> None:
    # E6 evidence: an error answer's shape (its code is not a token under the suffix rule).
    provider = Provider([httpx.Response(400, json={"code": "BAD_REQUEST", "message": "x"})])
    service = _service(container, provider)
    run = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert run.outcome == "ERROR"
    ((_, search_text, _),) = _stored(container)
    paths = json.loads(search_text)["paths"]
    assert set(paths) == {"$", "$.code", "$.message"}
    assert "BAD_REQUEST" not in search_text


@pytest.mark.parametrize(
    ("key", "value", "kept"),
    [
        ("statusType", "SALE", "SALE"),
        ("productOrderStatus", "PAYED", "PAYED"),
        ("indicationUnit", "ML", "ML"),
        ("deliveryMethod", "DELIVERY", "DELIVERY"),
        ("statusType", "sale", None),
        ("statusType", "SALE NOW", None),
        ("statusType", "A" * 42, None),
        ("statusType", "1SALE", None),
        ("statusTypes", "SALE", None),
        ("code", "BAD_REQUEST", None),
        ("name", "SALE", None),
        ("statusType", 7, None),
        (None, "SALE", None),
    ],
)
def test_an_enum_token_is_kept_only_under_the_suffix_rule(
    key: str | None, value: object, kept: str | None
) -> None:
    assert enum_token(key, value) == kept


def test_types_keys_and_bounds() -> None:
    assert [json_type(v) for v in (None, True, 1, 1.5, "x", {}, [])] == [
        "null",
        "boolean",
        "number",
        "number",
        "string",
        "object",
        "array",
    ]
    recorder = ShapeRecorder()
    # A map keyed by values never puts the value into a path.
    recorder.add({"byProduct": {"9876543210": {"x": 1}, "a b": 2}, "list": [1, "a", None]})
    shape = recorder.shape()
    assert "9876543210" not in json.dumps(shape)
    assert shape["paths"]["$.byProduct.*"]["types"] == ["number", "object"]
    assert shape["paths"]["$.list[]"]["types"] == ["null", "number", "string"]
    # At most MAX_PATHS paths; the rest is counted as truncated, never kept.
    wide = ShapeRecorder()
    wide.add({f"k{n}": n for n in range(MAX_PATHS + 50)})
    assert len(wide.shape()["paths"]) == MAX_PATHS and wide.shape()["truncated"]
    # At most MAX_TOKENS_PER_PATH tokens per path.
    tokens = ShapeRecorder()
    for n in range(MAX_TOKENS_PER_PATH + 10):
        tokens.add({"statusType": f"S{n}"})
    entry = tokens.shape()["paths"]["$.statusType"]
    assert len(entry["enum_tokens"]) == MAX_TOKENS_PER_PATH and entry["enum_tokens_truncated"]
    # Depth is bounded too.
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(100):
        node["d"] = {}
        node = node["d"]
    bounded = ShapeRecorder()
    bounded.add(deep)
    assert bounded.shape()["truncated"] and len(bounded.shape()["paths"]) <= 34


def test_only_the_search_and_the_origin_read_take_a_body_sink() -> None:
    provider = Provider([ok(page([]))], {"1": ok(origin_read())})
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    seen: list[object] = []
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_SELLER_ACCOUNT,
            AccountRequest(BEARER, 2, 5),
            body_sink=seen.append,
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert provider.requests == [] and seen == []
    # A sink that fails changes nothing the call returns.

    def broken(body: object) -> None:
        raise RuntimeError("sink failure")

    answer = caller.call(
        EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
        ProductReadRequest(BEARER, 2, 5, "1"),
        body_sink=broken,
    )
    plain = caller.call(
        EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2, ProductReadRequest(BEARER, 2, 5, "1")
    )
    assert answer.retained == plain.retained


def test_the_shape_route_is_read_only_and_empty_before_an_operator_sync(
    client: TestClient,
) -> None:
    shown = client.get("/api/v1/operate/store-catalog/shape")
    assert shown.status_code == 200 and shown.json() == {"shape": None}
    assert client.get("/api/v1/operate/store-catalog").json()["status"]["shape_path_count"] is None
