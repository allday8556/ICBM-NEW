"""The adopted SmartStore CREATE contract (ADR-0020 §4 order 1; ADR-0014 §9-§11, §15, §28;
ADR-0018 §6.1).

Everything here uses a **fake transport only**: no provider, no network, no LIVE. It pins the
registry-gated JSON transport, the frozen response contract, the conservative outcome
classification, the sanitized handoff evidence, the deterministic ``sellerManagementCode``
projection, and the one rule that never bends — an ``UNKNOWN`` is never resent.
"""

import ast
import hashlib
import inspect
import json
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


DOCUMENT: dict[str, Any] = {
    "originProduct": {
        "name": "테스트 상품",
        "detailContent": "본문",
        "images": {"representativeImage": {"url": REF_MAIN}},
        "salePrice": 19900,
        "leafCategoryId": "cat-1",
        "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": SELLER_CODE}},
    }
}


def _caller(provider: Provider) -> SmartStoreEndpointCaller:
    return SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))


def _sender(provider: Provider, *, bearer: object = Bearer()) -> SmartStoreCreateSender:
    """A sender whose projection is declared sendable, so the transport layer can be exercised.

    The real projection is not sendable at this adoption (the official evidence leaves required
    values uncaptured), and that refusal is pinned separately below and in the adapter suite.
    """

    class Sendable:
        sendable = True
        document = DOCUMENT
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
    # The body is exactly the typed document, canonically encoded; nothing is added to it.
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


def test_a_request_the_caller_cannot_encode_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    caller = _caller(provider)
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
            ProductCreateRequest(BEARER, 3, 7, {"originProduct": {"at": ImageAssetKind}}),
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert refused.value.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert provider.requests == []


# ---------------------------------------------------------------- the response contract


def test_the_success_response_yields_the_provider_identities_wherever_they_sit() -> None:
    # The JSON nesting of the identifiers is not captured, so they are recognized by name — but
    # only where the whole body resolves the name to exactly one usable value (no shape asserted,
    # and no reading chosen).
    nested = {
        "result": {"originProductNo": "9900112233", "smartstoreChannelProductNo": 55},
        "originProduct": {"name": "테스트 상품"},
    }
    provider = Provider(httpx.Response(200, json=nested))
    handoff = _send(provider)
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    assert handoff.marketplace_product_id == "9900112233"
    identifiers = handoff.sanitized_response["identifiers"]
    # Both provider identities are kept; neither is ever lost (ADR-0014 §28.2).
    assert identifiers["originProductNo"] == "9900112233"
    assert identifiers["smartstoreChannelProductNo"] == "55"
    assert identifiers["windowChannelProductNo"] is None
    assert identifiers["unresolved_identifiers"] == []
    assert identifiers["response_contract_version"] == "smartstore-create-response/v1"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"originProductNo": None},
        {"originProductNo": ""},
        {"originProductNo": "9900-112233"},
        {"originProductNo": True},
        {"originProductNo": 1.5},
        {"originProductNo": "9" * 33},
        {"smartstoreChannelProductNo": 55},
    ],
)
def test_a_success_without_a_usable_origin_number_is_unknown_not_a_failure(
    body: dict[str, Any],
) -> None:
    # A 200 that carries no usable identity proves nothing either way: the product may exist.
    provider = Provider(httpx.Response(200, json=body))
    handoff = _send(provider)
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.marketplace_product_id is None
    assert handoff.error_code == "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"
    assert handoff.error_class is ErrorClass.UNKNOWN


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
    assert handoff.sanitized_response["retained"]["originProduct"]["salePrice"] == 19900


def test_the_retained_fields_are_exactly_the_endpoint_profile() -> None:
    contract = resolve(EndpointId.SMARTSTORE_PRODUCT_CREATE_V2)
    kept = retain(contract, {"a": {"originProductNo": 1, "nope": 2, "sellerManagerCode": "x"}})
    assert kept == {"a": {"originProductNo": 1, "sellerManagerCode": "x"}}


def test_the_identifier_recognizer_never_coerces_a_non_identifier() -> None:
    # A value this contract does not understand is never coerced, and a usable value elsewhere in
    # the body never rescues it: the name stays unrecognized, recorded as unresolved.
    found = create.identifiers({"originProductNo": ["9900112233"], "x": {"originProductNo": 42}})
    assert found.origin_product_no is None
    assert found.readable is False
    assert found.unresolved == ("originProductNo",)
    absent = create.identifiers({})
    assert absent.readable is False
    # Absent is not unresolved: nothing was there to resolve.
    assert absent.unresolved == ()


@pytest.mark.parametrize(
    "body",
    [
        # Two nestings disagree: neither is preferred, so nothing is recognized.
        {"originProductNo": 9900112233, "result": {"originProductNo": 42}},
        {"a": {"originProductNo": 42}, "b": {"originProductNo": 43}},
        # Present but unusable at one nesting, usable at another: still not recognized.
        {"originProductNo": "", "result": {"originProductNo": 9900112233}},
        {"result": {"originProductNo": 9900112233}, "echo": {"originProductNo": "9900-112233"}},
        {"list": [{"originProductNo": 1}, {"originProductNo": 2}]},
    ],
)
def test_an_ambiguous_origin_number_is_unknown_never_an_applied_mutation(
    body: dict[str, Any],
) -> None:
    # The response-body nesting is uncaptured, so a body that offers more than one reading of the
    # identity is not evidence of an applied mutation: it fails closed to UNKNOWN, which is never
    # resent (ADR-0014 §17.2, §28).
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.marketplace_product_id is None
    assert handoff.error_code == "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"
    assert handoff.error_class is ErrorClass.UNKNOWN
    identifiers = handoff.sanitized_response["identifiers"]
    assert identifiers["originProductNo"] is None
    assert identifiers["unresolved_identifiers"] == ["originProductNo"]


def test_the_same_identity_repeated_at_several_nestings_is_not_ambiguous() -> None:
    # The response echoes the stored ``originProduct``, so the same number may legitimately appear
    # more than once. Agreeing occurrences are one reading, not a choice between two.
    body = {
        "originProductNo": 9900112233,
        "originProduct": {"originProductNo": "9900112233", "name": "테스트 상품"},
    }
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    assert handoff.marketplace_product_id == "9900112233"
    assert handoff.sanitized_response["identifiers"]["unresolved_identifiers"] == []


def test_an_ambiguous_channel_number_never_becomes_a_value_and_never_blocks_the_origin() -> None:
    # Only the origin number decides readability (ADR-0014 §11); an ambiguous channel identity is
    # dropped and recorded, never guessed and never lost silently (§28.2).
    body = {
        "originProductNo": 9900112233,
        "a": {"smartstoreChannelProductNo": 55},
        "b": {"smartstoreChannelProductNo": 56},
    }
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    identifiers = handoff.sanitized_response["identifiers"]
    assert identifiers["smartstoreChannelProductNo"] is None
    assert identifiers["unresolved_identifiers"] == ["smartstoreChannelProductNo"]


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


# ---------------------------------------------------------------- the request is Snapshot-only


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
        document: ClassVar[dict[str, Any]] = {
            "originProduct": {"accessToken": "Bearer abcdefghijklmnop"}
        }
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
        document: ClassVar[dict[str, Any]] = {}
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
