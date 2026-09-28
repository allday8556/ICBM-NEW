"""The adopted SmartStore CREATE contract (ADR-0020 §4 order 1; ADR-0014 §9-§11, §15, §28;
ADR-0018 §6.1).

Everything here uses a **fake transport only**: no provider, no network, no LIVE. It pins the
registry-gated JSON transport, the validated and frozen request document, the fail-closed response
contract, the conservative outcome classification, the sanitized handoff evidence, the
deterministic ``sellerManagementCode`` projection, and the one rule that never bends — an
``UNKNOWN`` is never resent.
"""

import ast
import hashlib
import inspect
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import ErrorClass
from app.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore import create, execution, product
from integrations.marketplaces.smartstore.caller import (
    ProductCreateRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import SmartStoreCreateSender
from integrations.marketplaces.smartstore.registry import EndpointId, resolve
from integrations.marketplaces.smartstore.retention import retain

BEARER = "fixture-access-token-Qx7"
BASE = "https://api.commerce.naver.com/external"
CREATE_URL = f"{BASE}/v2/products"
IDENTITY = "icbm-0123456789abcdef0123456789abcdef"
KEY_A = "rik1-" + "a" * 32
REF_MAIN = "https://shop-phinf.example/a/main.jpg"
REF_DETAIL = "https://shop-phinf.example/a/detail.jpg"
SELLER_CODE = hashlib.sha256(
    b"smartstore-seller-management-code/v1\0" + IDENTITY.encode("utf-8")
).hexdigest()[:30]


class Bearer:
    access_token = BEARER
    credential_generation = 3
    session_generation = 7


class Provider:
    """A fake transport. It records what it was handed and answers what the case declares."""

    def __init__(self, answer: httpx.Response | Exception) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


ORIGIN: dict[str, Any] = {
    "name": "테스트 상품",
    "detailContent": "본문",
    "images": {"representativeImage": {"url": REF_MAIN}},
    "salePrice": 19900,
    "leafCategoryId": "cat-1",
    "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": SELLER_CODE}},
}
DOCUMENT: dict[str, Any] = {"originProduct": ORIGIN}
# The only form a request may reach the caller in: checked against the adopted request contract and
# frozen with its provenance by the wire projection.
FROZEN = product.create_document(IDENTITY, DOCUMENT)


def _origin(**changes: Any) -> dict[str, Any]:
    """One request body with the named origin-product fields replaced or removed."""
    origin = {**deepcopy(ORIGIN), **changes}
    return {"originProduct": {key: value for key, value in origin.items() if value is not None}}


def _caller(provider: Provider) -> SmartStoreEndpointCaller:
    return SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))


def _sender(provider: Provider, *, bearer: object = Bearer()) -> SmartStoreCreateSender:
    """A sender whose projection is declared sendable, so the transport layer can be exercised.

    The real projection is not sendable at this adoption (the official evidence leaves required
    values uncaptured), and that refusal is pinned separately below and in the adapter suite.
    """

    class Sendable:
        sendable = True
        document = FROZEN
        gaps: tuple[str, ...] = ()

    return SmartStoreCreateSender(
        caller=_caller(provider), bearer=lambda: bearer, projector=lambda payload: Sendable()
    )


def _send(provider: Provider, **kwargs: Any):
    return _sender(provider, **kwargs).send(
        payload={}, idempotency_key="idem-1", listing_identity=IDENTITY
    )


# ---------------------------------------------------------------- the registry-gated transport


def test_the_create_goes_out_as_the_registry_contract_says_and_nothing_else() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 9900112233}))
    _send(provider)
    (sent,) = provider.requests
    assert (sent.method, str(sent.url)) == ("POST", CREATE_URL)
    assert sent.headers["authorization"] == f"Bearer {BEARER}"
    assert sent.headers["content-type"] == "application/json"
    # Deny-by-default: the CREATE sends no query key at all.
    assert sent.url.query == b""
    # The body is exactly the frozen document's own bytes; nothing is added to it on the way out.
    assert sent.content == FROZEN.encoded()
    assert json.loads(sent.content.decode("utf-8")) == DOCUMENT
    assert sent.content == json.dumps(
        DOCUMENT, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def test_no_idempotency_or_correlation_header_is_invented() -> None:
    # The provider documents no idempotency key, no request-correlation key and no replay rule.
    # Sending one would claim a guarantee it does not give (ADR-0014 §17.2, §28).
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    _send(provider)
    (sent,) = provider.requests
    names = {name.lower() for name in sent.headers}
    assert not {name for name in names if "idempot" in name or "correlat" in name}
    assert "idem-1" not in sent.content.decode("utf-8")


def test_the_endpoint_contract_is_the_only_timeout_and_redirect_policy() -> None:
    contract = resolve(EndpointId.SMARTSTORE_PRODUCT_CREATE_V2)
    assert (contract.connect_timeout_s, contract.read_timeout_s) == (5.0, 30.0)
    assert contract.redirect.value == "NO_FOLLOW"
    assert contract.mutating and contract.requires_bearer


@pytest.mark.parametrize("token", ["", "   ", "bad token", "tab\there"])
def test_an_unusable_bearer_never_reaches_the_transport(token: str) -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))

    class BadBearer:
        access_token = token
        credential_generation = 3
        session_generation = 7

    handoff = _send(provider, bearer=BadBearer())
    assert provider.requests == []
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_class is ErrorClass.FATAL


# ---------------------------------------------- the request is the validated typed projection


@pytest.mark.parametrize(
    "document",
    [
        # A plain mapping, however well shaped, carries neither the projection's provenance nor its
        # validation, so the caller refuses it before any wire bytes exist.
        DOCUMENT,
        {},
        {"originProduct": {"at": ImageAssetKind}},
        FROZEN.canonical_json,
    ],
)
def test_only_the_frozen_projection_document_can_become_a_request(document: object) -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    with pytest.raises(SmartStoreCallError) as refused:
        _caller(provider).call(
            EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
            ProductCreateRequest(BEARER, 3, 7, document),  # type: ignore[arg-type]
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert refused.value.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert provider.requests == []


def test_a_projection_that_is_not_a_frozen_document_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))

    class Raw:
        sendable = True
        document: ClassVar[dict[str, Any]] = DOCUMENT
        gaps: tuple[str, ...] = ()

    sender = SmartStoreCreateSender(
        caller=_caller(provider), bearer=lambda: Bearer(), projector=lambda payload: Raw()
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_WIRE_CONTRACT_VIOLATION"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


@pytest.mark.parametrize(
    ("body", "code"),
    [
        # Deny-by-default: a path the captured official evidence does not record cannot be sent,
        # whatever built the mapping — including a required field whose values are a named gap.
        (_origin(statusType="SALE"), "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        (_origin(stockQuantity=3), "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        (
            {**deepcopy(DOCUMENT), "smartstoreChannelProduct": {"naverShoppingRegistration": True}},
            "WIRE_DOCUMENT_FIELD_UNKNOWN",
        ),
        ({**deepcopy(DOCUMENT), "windowChannelProduct": {}}, "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        # A field the provider requires, missing.
        (_origin(salePrice=None), "WIRE_DOCUMENT_FIELD_MISSING"),
        (_origin(leafCategoryId=None), "WIRE_DOCUMENT_FIELD_MISSING"),
        ({}, "WIRE_DOCUMENT_FIELD_MISSING"),
        # A value outside its documented bound, or of a type this contract does not encode.
        (_origin(salePrice=999_999_991), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(salePrice="19900"), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(salePrice=True), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(name=""), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(detailContent=123), "WIRE_DOCUMENT_VALUE_INVALID"),
        # At most nine optional images beside the representative one.
        (
            _origin(
                images={
                    "representativeImage": {"url": REF_MAIN},
                    "optionalImages": [{"url": f"{REF_DETAIL}?{n}"} for n in range(10)],
                }
            ),
            "WIRE_DOCUMENT_VALUE_INVALID",
        ),
        # Every URL must still be a prepared, sanitized provider reference.
        (
            _origin(images={"representativeImage": {"url": "http://supplier.example/a.jpg"}}),
            "WIRE_IMAGE_REFERENCE_UNSAFE",
        ),
        (_origin(images={"representativeImage": {}}), "WIRE_DOCUMENT_FIELD_MISSING"),
        (_origin(images={"optionalImages": [{"url": REF_DETAIL}]}), "WIRE_DOCUMENT_FIELD_MISSING"),
    ],
)
def test_a_document_outside_the_adopted_request_contract_is_refused(
    body: dict[str, Any], code: str
) -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code == code


def test_a_document_that_is_not_this_listings_projection_is_refused() -> None:
    # The provenance the wire boundary must be able to trust: the management code has to be the
    # projection of the listing identity the document claims (architect ruling R1).
    with pytest.raises(product.WireContractError) as refused:
        product.create_document("icbm-" + "f" * 32, DOCUMENT)
    assert refused.value.code == "WIRE_DOCUMENT_NOT_THIS_SNAPSHOT"
    with pytest.raises(product.WireContractError) as forged:
        product.create_document(
            IDENTITY,
            _origin(detailAttribute={"sellerCodeInfo": {"sellerManagementCode": "f" * 30}}),
        )
    assert forged.value.code == "WIRE_DOCUMENT_NOT_THIS_SNAPSHOT"


def test_a_frozen_document_cannot_change_after_the_snapshot_was_projected() -> None:
    # The same document is the wire body, the sanitized evidence and the durable digest source
    # (ADR-0014 §15, B4), so nothing may be added to it or changed in it after the projection.
    source = deepcopy(DOCUMENT)
    frozen = product.create_document(IDENTITY, source)
    source["originProduct"]["name"] = "무단 변경"
    source["originProduct"]["stockQuantity"] = 1
    handed = frozen.mapping()
    handed["originProduct"]["name"] = "무단 변경"
    assert frozen.mapping() == DOCUMENT
    assert json.loads(frozen.encoded().decode("utf-8")) == DOCUMENT
    assert frozen.listing_identity == IDENTITY


def test_the_sanitized_request_is_the_document_and_carries_no_credential() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    handoff = _send(provider)
    assert handoff.sanitized_request == DOCUMENT
    text = json.dumps(handoff.sanitized_request, ensure_ascii=False)
    assert BEARER not in text and "Bearer" not in text and "authorization" not in text.lower()


def test_a_request_carrying_secret_material_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))

    class Leaky:
        sendable = True
        document = product.create_document(IDENTITY, _origin(name="Bearer abcdefghijklmnop"))
        gaps: tuple[str, ...] = ()

    sender = SmartStoreCreateSender(
        caller=_caller(provider), bearer=lambda: Bearer(), projector=lambda payload: Leaky()
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "SMARTSTORE_CREATE_REQUEST_UNSANITIZED"


def test_an_unsendable_projection_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))

    class NotSendable:
        sendable = False
        document = FROZEN
        gaps = (product.GAP_STATUS_TYPE,)

    sender = SmartStoreCreateSender(
        caller=_caller(provider), bearer=lambda: Bearer(), projector=lambda payload: NotSendable()
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_WIRE_NOT_SENDABLE"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


def test_a_payload_that_is_not_a_registration_payload_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    sender = SmartStoreCreateSender(caller=_caller(provider), bearer=lambda: Bearer())
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_WIRE_PAYLOAD_MALFORMED"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


# ---------------------------------------------------------------- the response contract


def test_the_response_contract_reads_no_identity_and_names_the_gap() -> None:
    reading = create.read({"originProductNo": 9900112233})
    assert reading.readable is False
    assert reading.gaps == (create.GAP_RESPONSE_IDENTIFIER_SHAPE,)
    # The gap names the exact uncaptured facts, so a later slice knows what closing it requires.
    assert "nesting" in reading.gaps[0] and "value type" in reading.gaps[0]
    assert reading.canonical()["identifier_read"] == "NOT_PROJECTABLE"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"originProductNo": 9900112233},
        {"originProductNo": "9900112233"},
        {"result": {"originProductNo": "9900112233", "smartstoreChannelProductNo": 55}},
        {"originProductNo": 1, "originProduct": {"originProductNo": 1, "name": "테스트 상품"}},
        {"list": [{"originProductNo": 1}, {"originProductNo": 2}]},
        {"smartstoreChannelProductNo": 55},
    ],
)
def test_no_success_body_ever_yields_a_provider_identity(body: dict[str, Any]) -> None:
    # The official evidence captures the identifier *names* only — neither their JSON nesting inside
    # the body nor their value type. Reading an identity out of such a body, by asserting one
    # nesting, by searching every nesting, or by accepting more than one value type, would invent a
    # response semantic, so the contract reads none at all (ADR-0014 §17.3; EM §4.1.1).
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.marketplace_product_id is None
    assert handoff.error_code == "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"
    assert handoff.error_class is ErrorClass.UNKNOWN
    contract = handoff.sanitized_response["response_contract"]
    assert contract["identifier_read"] == "NOT_PROJECTABLE"
    assert contract["gaps"] == [create.GAP_RESPONSE_IDENTIFIER_SHAPE]
    assert contract["response_contract_version"] == "smartstore-create-response/v1"


def test_an_unreadable_success_is_unknown_and_never_a_proven_absence() -> None:
    # It proves nothing either way: the product may exist. That is why it is neither a failure nor
    # a resend, and why NOT_APPLIED_PROVEN stays whitelist-only (ADR-0014 §28.3, M5-08, G3-07).
    handoff = _send(Provider(httpx.Response(200, json={"originProductNo": 9900112233})))
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.error_class is not ErrorClass.FATAL


def test_no_applied_proven_outcome_exists_while_the_identifier_read_is_a_gap() -> None:
    # Structural, so an edit cannot quietly reintroduce an invented identity read: the seam names
    # no APPLIED_PROVEN and sets no marketplace product id from a response.
    tree = ast.parse(Path(inspect.getsourcefile(execution) or "").read_text("utf-8"))
    (sender,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "SmartStoreCreateSender"
    ]
    assert "APPLIED_PROVEN" not in {
        node.attr for node in ast.walk(sender) if isinstance(node, ast.Attribute)
    }
    assert "marketplace_product_id" not in {
        node.arg for node in ast.walk(sender) if isinstance(node, ast.keyword) and node.arg
    }


def test_only_the_retention_allow_list_crosses_the_response_boundary() -> None:
    body = {
        "originProductNo": 9900112233,
        "smartstoreChannelProductNo": 55,
        "originProduct": {
            "name": "테스트 상품",
            "salePrice": 19900,
            "detailContent": "<p>본문</p>",
            "sellerCodeInfo": {"sellerManagementCode": SELLER_CODE, "sellerBarcode": "880123"},
            "images": {"representativeImage": {"url": REF_MAIN, "order": 1}},
        },
        "traceId": "trace-1",
        "accessToken": "Bearer abcdefghijklmnop",
    }
    handoff = _send(Provider(httpx.Response(200, json=body)))
    text = json.dumps(handoff.sanitized_response, ensure_ascii=False)
    for dropped in ("detailContent", "본문", "sellerBarcode", "880123", "traceId", "accessToken"):
        assert dropped not in text
    assert "Bearer" not in text
    # The retained body is still kept as evidence of what came back; it is simply never read for
    # an identity while the response shape is uncaptured.
    assert handoff.sanitized_response["retained"]["originProduct"]["salePrice"] == 19900


def test_the_retained_fields_are_exactly_the_endpoint_profile() -> None:
    contract = resolve(EndpointId.SMARTSTORE_PRODUCT_CREATE_V2)
    kept = retain(contract, {"a": {"originProductNo": 1, "nope": 2, "sellerManagerCode": "x"}})
    assert kept == {"a": {"originProductNo": 1, "sellerManagerCode": "x"}}


# ---------------------------------------------------------------- outcome classification


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 502, 503])
def test_every_response_after_handoff_is_unknown_never_proven_not_applied(status: int) -> None:
    # Architect ruling R2 (Issue #89 `5861607665`): an ordinary provider 4xx received after
    # transport handoff is not proof of non-application, and neither is a 5xx (ADR-0014 §17.2).
    provider = Provider(httpx.Response(status, json={"code": "BAD_REQUEST", "message": "no"}))
    handoff = _send(provider)
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.marketplace_product_id is None
    assert handoff.response_status == status
    assert handoff.details["transmission_phase"] == "RESPONSE_RECEIVED"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_never_followed_and_never_proves_anything(status: int) -> None:
    # ERRORS.md §10.6, §17: a 308 is never followed for a mutation, and the response is evidence.
    provider = Provider(httpx.Response(status, headers={"location": "https://elsewhere.invalid"}))
    handoff = _send(provider)
    assert len(provider.requests) == 1
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.error_code == "SMARTSTORE_UNEXPECTED_REDIRECT"


def test_a_timeout_after_the_request_was_written_is_unknown() -> None:
    provider = Provider(httpx.ReadTimeout("no response"))
    handoff = _send(provider)
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.error_class is ErrorClass.TRANSIENT
    # A transient cause never implies a safe replay (ADR-0014 §9).
    assert handoff.marketplace_product_id is None


def test_a_connection_this_request_did_not_open_is_unknown() -> None:
    provider = Provider(httpx.ConnectError("lost"))
    handoff = _send(provider)
    # The trace shows no new connection was opened by this request, so nothing is precluded.
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.details["transmission_phase"] in {"NO_NEW_CONNECTION", "UNOBSERVED"}


def test_a_local_refusal_is_the_only_proven_non_application() -> None:
    # ERRORS.md §15.1: the whitelist. Nothing left the machine, so nothing was applied — and the
    # cause is FATAL, so no automatic retry follows either (ADR-0014 §9).
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    handoff = _send(provider, bearer=None)
    assert provider.requests == []
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_class is ErrorClass.FATAL
    assert handoff.error_code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert handoff.details["transmission_phase"] == "LOCAL_PREFLIGHT"


def test_the_cause_and_the_remote_outcome_stay_independent_axes() -> None:
    provider = Provider(httpx.Response(429, json={"code": "GW.RATE_LIMIT"}))
    handoff = _send(provider)
    assert handoff.error_class is ErrorClass.RATE_LIMITED
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN


def test_the_seam_holds_no_resend_path_of_its_own() -> None:
    # ADR-0014 §28.3 / M5-08 / G3-07: an UNKNOWN is never resent. One send is one call, whatever
    # the outcome was, and the seam schedules, retries and reopens nothing.
    for answer in (
        httpx.Response(500, json={}),
        httpx.Response(200, json={}),
        httpx.ReadTimeout("no response"),
    ):
        provider = Provider(answer)
        handoff = _send(provider)
        assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
        assert len(provider.requests) == 1
    # Structurally, too: the sender has no loop to send from twice, and holds no scheduler.
    tree = ast.parse(Path(inspect.getsourcefile(execution) or "").read_text("utf-8"))
    (sender,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "SmartStoreCreateSender"
    ]
    assert not [n for n in ast.walk(sender) if isinstance(n, ast.While | ast.For)]
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"time", "asyncio", "threading", "sched"}
