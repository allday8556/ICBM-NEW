"""Notice coverage S0: the read-only capture of the official 상품정보제공고시 schema.

It reads the official type list, then each listed type once, with the committed bearer; it keeps
only the allow-listed members; it never writes or retries; a failed type is reported, not filled.
"""

import httpx

from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.notice_catalog import SmartStoreNoticeCatalog
from integrations.marketplaces.smartstore.registry import EndpointId, resolve
from tests.unit.integrations.marketplaces.smartstore.test_smartstore_create import Bearer

LIST = [
    {"productInfoProvidedNoticeType": "WEAR", "productInfoProvidedNoticeTypeName": "의류"},
    {"productInfoProvidedNoticeType": "DIET_FOOD", "productInfoProvidedNoticeTypeName": "건강"},
]
WEAR = {
    "productInfoProvidedNoticeType": "WEAR",
    "productInfoProvidedNoticeTypeName": "의류",
    "productInfoProvidedNoticeContents": [
        {
            "fieldType": "STRING",
            "fieldName": "material",
            "fieldDescription": "제품소재",
            "fieldAddDescription": "",
            "fieldMaxLength": 1500,
            "unlisted": "dropped",
        }
    ],
}


class Provider:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/products-for-provided-notice"):
            return httpx.Response(200, json=LIST)
        if path.endswith("/WEAR"):
            return httpx.Response(200, json=WEAR)
        return httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"})


PAUSES: list[float] = []


def _catalog(provider: Provider, bearer: object = Bearer()) -> SmartStoreNoticeCatalog:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    PAUSES.clear()
    return SmartStoreNoticeCatalog(caller, bearer=lambda: bearer, pause=PAUSES.append)


def test_both_reads_are_adopted_as_reads() -> None:
    for endpoint in (EndpointId.SMARTSTORE_NOTICE_TYPES, EndpointId.SMARTSTORE_NOTICE_TYPE_READ):
        contract = resolve(endpoint)
        assert contract.method.value == "GET" and not contract.mutating
        assert contract.safe_query_keys == frozenset()


def test_the_capture_reads_the_list_then_each_type_once_keeping_only_listed_members() -> None:
    provider = Provider()
    captured = _catalog(provider).capture()
    assert captured["captured"] is True
    assert [r.method for r in provider.requests] == ["GET", "GET", "GET"]
    assert [r.url.path for r in provider.requests] == [
        "/external/v1/products-for-provided-notice",
        "/external/v1/products-for-provided-notice/WEAR",
        "/external/v1/products-for-provided-notice/DIET_FOOD",
    ]
    (field,) = captured["types"]["WEAR"]["productInfoProvidedNoticeContents"]
    assert field == {
        "fieldAddDescription": "",
        "fieldDescription": "제품소재",
        "fieldMaxLength": 1500,
        "fieldName": "material",
        "fieldType": "STRING",
    }
    # A failed type is reported with its code, never filled in, and never retried.
    assert "DIET_FOOD" not in captured["types"]
    assert captured["failures"]["DIET_FOOD"]["http_status"] == 500
    assert captured["mapping_revision"] == "m65-dispatch-r1"
    # Each type read is spaced by the policy interval, so a full capture stays under the provider's
    # request-rate limit; nothing is retried.
    assert PAUSES == [1.0, 1.0]


def test_without_a_session_nothing_is_read() -> None:
    provider = Provider()
    captured = _catalog(provider, bearer=None).capture()
    assert captured == {"captured": False, "code": "SMARTSTORE_SESSION_UNAVAILABLE"}
    assert provider.requests == []
