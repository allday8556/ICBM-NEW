"""M5 IMAGE UPLOAD amendment: one artifact, one request, fake transport only."""

from typing import Any

import httpx
import pytest

from app.capabilities.live_safety.assets import TransmissionPrecluded
from app.capabilities.live_safety.model import UploadAttemptState
from app.stages.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore.assets import (
    ImageUploadAdapter,
    SmartStoreAssetSender,
    upload_request,
)
from integrations.marketplaces.smartstore.caller import (
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import (
    SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    EndpointId,
    wire_identity,
)

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


def test_search_and_create_are_separate_contracts_from_the_upload() -> None:
    from integrations.marketplaces.smartstore.registry import ADOPTED, resolve

    # The CREATE adoption slice adopted POST /v2/products and the SEARCH slice (ADR-0020 §4
    # order 2) POST /v1/products/search, as a read for positive-only reconcile only.
    assert EndpointId.SMARTSTORE_PRODUCT_SEARCH in ADOPTED
    assert resolve(EndpointId.SMARTSTORE_PRODUCT_SEARCH).mutating is False
    assert EndpointId.SMARTSTORE_PRODUCT_CREATE_V2 in ADOPTED
    # Adoption never merges two contracts: the upload keeps its own path, media type and profile.
    upload, create = (
        resolve(EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD),
        resolve(EndpointId.SMARTSTORE_PRODUCT_CREATE_V2),
    )
    assert upload.path != create.path
    assert (upload.content_type, create.content_type) == (
        "multipart/form-data",
        "application/json",
    )


# ---------------------------------------------------------------- the production ASSET sender


class Session:
    access_token = BEARER
    credential_generation = 3
    session_generation = 7


def sender(provider: Provider, *, session: Any = Session) -> SmartStoreAssetSender:
    return SmartStoreAssetSender(
        SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)),
        bearer=lambda: session,
    )


def send(provider: Provider, **changes: Any) -> Any:
    values: dict[str, Any] = {
        "content": b"exact-artifact-bytes",
        "file_name": "artifact.jpg",
        "media_type": "image/jpeg",
    }
    values.update(changes)
    return sender(provider).send(**values)


def test_the_sender_declares_the_adopted_wire_endpoint_and_nothing_else() -> None:
    declared = sender(Provider(httpx.Response(200, json={"images": [{"url": REF}]})))
    assert declared.marketplace_key == "smartstore"
    assert declared.endpoint_adopted() is True
    assert declared.wire() == wire_identity(EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD)
    assert declared.wire() == (
        "POST",
        "api.commerce.naver.com",
        "/external/v1/product-images/upload",
    )
    assert declared.contract_label() == SMARTSTORE_ENDPOINT_MAPPING_REVISION


def test_without_a_committed_session_the_sender_is_unavailable_and_sends_nothing() -> None:
    provider = Provider(httpx.Response(200, json={"images": [{"url": REF}]}))
    unwired = sender(provider, session=None)
    assert unwired.available() is False
    # The endpoint stays adopted: only the session is missing.
    assert unwired.endpoint_adopted() is True
    with pytest.raises(TransmissionPrecluded, match="SMARTSTORE_SESSION_UNAVAILABLE"):
        unwired.send(content=b"bytes", file_name="a.jpg", media_type="image/jpeg")
    assert provider.requests == []


def test_a_proven_upload_is_applied_with_exactly_one_request_and_clean_evidence() -> None:
    provider = Provider(
        httpx.Response(200, json={"images": [{"url": REF, "token": "drop"}], "traceId": "drop"})
    )
    assert sender(provider).available() is True
    result = send(provider)
    assert result.state is UploadAttemptState.APPLIED_PROVEN
    assert result.provider_asset_ref == REF and result.outcome_reason is None
    assert result.evidence == {
        "outcome_version": "smartstore-image-upload-outcome/v1",
        "contract_label": SMARTSTORE_ENDPOINT_MAPPING_REVISION,
        "http_status": 200,
    }
    assert BEARER not in repr(result) and "drop" not in repr(result)
    (sent,) = provider.requests
    assert (sent.method, str(sent.url)) == ("POST", f"{BASE}/v1/product-images/upload")
    assert sent.headers["authorization"] == f"Bearer {BEARER}"
    assert sent.content.count(b'name="imageFiles"') == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"images": []}, "UPLOAD_NO_REFERENCE_RETURNED"),
        ({"images": [{"url": REF}, {"url": REF}]}, "UPLOAD_REFERENCE_NOT_UNIQUE"),
        ({"images": [{"url": REF}, {"url": REF + "2"}]}, "UPLOAD_REFERENCE_NOT_UNIQUE"),
        ({"images": [{"url": REF + "?token=abc"}]}, "UPLOAD_REFERENCE_UNSAFE"),
    ],
    ids=["no-reference", "two-equal", "two-different", "unsafe-reference"],
)
def test_an_ambiguous_success_is_unknown_never_applied(body: dict[str, Any], reason: str) -> None:
    provider = Provider(httpx.Response(200, json=body))
    result = send(provider)
    assert (result.state, result.provider_asset_ref, result.outcome_reason) == (
        UploadAttemptState.UPLOAD_UNKNOWN,
        None,
        reason,
    )
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}),
        httpx.Response(400, json={"code": "BAD_REQUEST"}),
        httpx.Response(429, json={"code": "GW.RATE_LIMIT"}),
        httpx.Response(200, content=b"not json"),
        httpx.ReadTimeout("timed out"),
    ],
    ids=["500", "400", "429", "unparseable-200", "read-timeout"],
)
def test_a_possibly_transmitted_failure_is_unknown_and_never_retried(
    response: httpx.Response | Exception,
) -> None:
    provider = Provider(response)
    result = send(provider)
    assert result.state is UploadAttemptState.UPLOAD_UNKNOWN and result.provider_asset_ref is None
    assert result.outcome_reason
    # One call: the sender owns no retry, and an UNKNOWN is never re-sent by it.
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"file_name": "../artifact.jpg"},
        {"media_type": "application/octet-stream"},
        {"content": b""},
    ],
    ids=["unsafe-name", "not-an-image", "empty"],
)
def test_a_local_refusal_proves_nothing_left_the_process(change: dict[str, Any]) -> None:
    provider = Provider(httpx.Response(200, json={"images": [{"url": REF}]}))
    with pytest.raises(TransmissionPrecluded):
        send(provider, **change)
    assert provider.requests == []
