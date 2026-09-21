"""M5 IMAGE UPLOAD amendment: one artifact, one request, fake transport only."""

from typing import Any

import httpx
import pytest

from app.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore.assets import ImageUploadAdapter, upload_request
from integrations.marketplaces.smartstore.caller import (
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId

BEARER = "fixture-access-token-Qx7"
BASE = "https://api.commerce.naver.com/external"
REF = "https://shop-phinf.example/a/main.jpg"


class Provider:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def request(**changes: Any):
    values: dict[str, Any] = {
        "access_token": BEARER,
        "credential_generation": 3,
        "session_generation": 7,
        "filename": "artifact.jpg",
        "media_type": "image/jpeg",
        "content": b"exact-artifact-bytes",
    }
    values.update(changes)
    return upload_request(**values)


def adapter(provider: Provider) -> ImageUploadAdapter:
    return ImageUploadAdapter(SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)))


def upload(provider: Provider):
    return adapter(provider).upload(
        request(),
        asset_kind=ImageAssetKind.SOURCE_ASSET,
        sha256="a" * 64,
        derivation_id=None,
        asset_profile="smartstore-main/v1",
        candidate_fingerprint="c" * 64,
    )


def test_one_artifact_is_one_image_files_request_and_promotes_the_returned_url() -> None:
    provider = Provider(httpx.Response(200, json={"images": [{"url": REF}]}))
    outcome = upload(provider)
    assert outcome.asset is not None and outcome.asset.provider_asset_ref == REF
    assert outcome.ambiguous_reason is None
    (sent,) = provider.requests
    assert (sent.method, str(sent.url)) == (
        "POST",
        f"{BASE}/v1/product-images/upload",
    )
    assert sent.headers["authorization"] == f"Bearer {BEARER}"
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = sent.content
    assert body.count(b'name="imageFiles"') == 1
    assert b'filename="artifact.jpg"' in body
    assert b"exact-artifact-bytes" in body
    assert sent.url.query == b""


def test_only_documented_url_leaves_cross_the_response_boundary() -> None:
    provider = Provider(
        httpx.Response(
            200,
            json={"images": [{"url": REF, "token": "drop"}], "traceId": "drop"},
        )
    )
    outcome = upload(provider)
    assert outcome.asset is not None and outcome.asset.provider_asset_ref == REF
    assert "drop" not in repr(outcome)


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "../artifact.jpg"},
        {"filename": "artifact\r\nx.jpg"},
        {"media_type": "application/octet-stream"},
        {"content": b""},
        {"credential_generation": 0},
    ],
)
def test_unusable_artifact_or_session_fails_before_transport(change: dict[str, Any]) -> None:
    provider = Provider(httpx.Response(200, json={"images": [{"url": REF}]}))
    with pytest.raises(SmartStoreCallError):
        adapter(provider).upload(
            request(**change),
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256="a" * 64,
            derivation_id=None,
            asset_profile="smartstore-main/v1",
            candidate_fingerprint="c" * 64,
        )
    assert provider.requests == []


def test_response_failure_is_upload_unknown_and_is_not_retried() -> None:
    provider = Provider(httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}))
    outcome = upload(provider)
    assert (outcome.asset, outcome.ambiguous_reason) == (None, "UPLOAD_UNKNOWN")
    assert len(provider.requests) == 1


def test_transport_ambiguity_is_upload_unknown_and_is_not_retried() -> None:
    provider = Provider(httpx.ReadTimeout("uncertain"))
    outcome = upload(provider)
    assert (outcome.asset, outcome.ambiguous_reason) == (None, "UPLOAD_UNKNOWN")
    assert len(provider.requests) == 1


def test_create_and_search_remain_unadopted() -> None:
    from integrations.marketplaces.smartstore.registry import NOT_ADOPTED

    assert EndpointId.SMARTSTORE_PRODUCT_CREATE_V2 in NOT_ADOPTED
    assert EndpointId.SMARTSTORE_PRODUCT_SEARCH in NOT_ADOPTED
