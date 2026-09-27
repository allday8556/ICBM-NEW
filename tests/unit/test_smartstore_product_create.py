"""The adopted SmartStore product CREATE contract (ADR-0020 §4 slice 1).

Every provider interaction here runs through `httpx.MockTransport`: the tests assert the composed
request, the typed result, the retention boundary and the outcome classification without opening a
socket, and the `no_transport` fixture proves the paths that must never reach a client.

What this file pins:

* the frozen endpoint contract — method, path, media type, bearer, group, timeouts, NO_FOLLOW, the
  success predicate and the deny-by-default retention profile (ENDPOINT_MATRIX.md §4.2, §10, §11);
* that only a `CreateDocument` derived from a frozen Snapshot projection can become a request, and
  that no document can be built while the CREATE body has an unproven part;
* the outcome axis (ERRORS.md §14, §15; ADR-0014 §28.3): a definitive provider rejection is
  `NOT_APPLIED_PROVEN`, everything ambiguous stays `UNKNOWN`, and a transient cause never turns
  into a safe replay;
* that the production sender is still unavailable, that one send is one call, and that adoption
  grants no LIVE authority and no confirmation.
"""

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.connect.marketplace.capability import RemoteOutcome
from app.core.egress import EGRESS
from app.core.errors import ErrorClass
from integrations.marketplaces.smartstore import classify
from integrations.marketplaces.smartstore import execution as adapter
from integrations.marketplaces.smartstore.caller import (
    ProductCreateRequest,
    ProductCreateResult,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import (
    CREATE_ENDPOINT,
    CreateSessionUnavailableError,
    SmartStoreCreateSender,
)
from integrations.marketplaces.smartstore.product import (
    CHANNEL_PRODUCT_GAP,
    CreateDocument,
    WireContractError,
    create_document,
    project,
)
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    ADOPTION_GAPS,
    BASE_URL,
    CREATE_AUTOMATIC_RETRY_BUDGET,
    CREATE_PROVIDER_IDEMPOTENCY,
    EndpointId,
    Method,
    RedirectPolicy,
    product_create_succeeded,
    provider_product_no,
    resolve,
)

BEARER = "fixture-access-token-Zq4"
WIRE_URL = f"{BASE_URL}/v2/products"
ORIGIN_NO = 3005432100
CHANNEL_NO = 4005432101
IDENTITY = "icbm-unit-a1"

# One already-built document. How a document is derived is exercised separately: these tests are
# about what the caller and the sender do with one.
DOCUMENT = CreateDocument(
    encoding_version="smartstore-register-wire/v1",
    listing_identity=IDENTITY,
    body={
        "originProduct": {
            "name": "테스트 상품",
            "salePrice": 19900,
            "sellerCodeInfo": {"sellerManagementCode": IDENTITY},
        }
    },
)

SUCCESS_BODY: dict[str, Any] = {
    "originProductNo": ORIGIN_NO,
    "smartstoreChannelProductNo": CHANNEL_NO,
    # Not on the retention allow-list: it must not survive the boundary.
    "sellerTel": "010-0000-0000",
    "originProduct": {"name": "테스트 상품", "salePrice": 19900, "stockQuantity": 7},
}


def documented(**changes: Any) -> dict[str, Any]:
    """The documented success document with one member changed; ``None`` removes it."""
    body = {**SUCCESS_BODY, **changes}
    return {key: value for key, value in body.items() if value is not None}


# A well-formed frozen-Snapshot payload whose unit is nevertheless unsendable, because the CREATE
# body still has unproven parts. It is the shape `project` reads, nothing more.
UNSENDABLE_PAYLOAD: dict[str, Any] = {
    "listing_identity": IDENTITY,
    "listing_shape": "SINGLE_LISTING_WITH_OPTIONS",
    "name": {"value": "테스트 상품"},
    "detail": {"body": "<p>본문</p>"},
    "notice": {"fields": {"제조사": {"value": "테스트"}}},
    "items": [
        {
            "registration_item_key": "item-a1",
            "sale_price_krw": 19900,
            "options": {},
            "publication_assets": [
                {"role": "REPRESENTATIVE", "provider_asset_ref": "https://s.example/a.jpg"}
            ],
        }
    ],
}


class Provider:
    """A fake provider transport. It records every request it was handed — often none."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@dataclasses.dataclass(frozen=True)
class Session:
    """A committed CONNECT session, in the shape the adapter's bearer source yields."""

    access_token: str = BEARER
    credential_generation: int = 3
    session_generation: int = 7


@pytest.fixture
def no_transport(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any client construction or egress grant is recorded and refused."""
    reached: list[str] = []

    def refuse(name: str) -> Callable[..., object]:
        def refused(*args: object, **kwargs: object) -> object:
            reached.append(name)
            raise AssertionError(f"{name} was reached")

        return refused

    monkeypatch.setattr(httpx, "Client", refuse("httpx.Client"))
    monkeypatch.setattr(EGRESS, "grant", refuse("egress grant"))
    return reached


@pytest.fixture
def frozen_document(monkeypatch: pytest.MonkeyPatch) -> CreateDocument:
    """The sender under a Snapshot whose CREATE body is fully proven.

    No unit is sendable at this main — :data:`CHANNEL_PRODUCT_GAP` alone sees to that, and
    :func:`test_no_create_document_can_be_built_while_the_body_has_an_unproven_part` pins it. This
    fixture substitutes the document builder so the *transport* behaviour of the adopted contract
    can be proven now, which is exactly what this slice adopts.
    """
    monkeypatch.setattr(adapter, "create_document", lambda payload: DOCUMENT)
    return DOCUMENT


def caller(provider: Provider) -> SmartStoreEndpointCaller:
    return SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))


def request(**changes: Any) -> ProductCreateRequest:
    values: dict[str, Any] = {
        "access_token": BEARER,
        "credential_generation": 3,
        "session_generation": 7,
        "document": DOCUMENT,
    }
    values.update(changes)
    return ProductCreateRequest(**values)


def call(provider: Provider, **changes: Any) -> ProductCreateResult:
    return caller(provider).call(CREATE_ENDPOINT, request(**changes))


def failure(provider: Provider, **changes: Any) -> SmartStoreCallError:
    with pytest.raises(SmartStoreCallError) as raised:
        call(provider, **changes)
    return raised.value


def sender(provider: Provider) -> SmartStoreCreateSender:
    return SmartStoreCreateSender(caller(provider), Session)


def send(provider: Provider) -> Any:
    return sender(provider).send(
        payload=UNSENDABLE_PAYLOAD, idempotency_key="intent-key", listing_identity=IDENTITY
    )


# ---------------------------------------------------------------- the frozen endpoint contract


def test_the_create_contract_is_adopted_exactly_as_the_evidence_proves_it() -> None:
    contract = resolve(CREATE_ENDPOINT)
    assert (contract.method, contract.path) == (Method.POST, "/v2/products")
    # Review 5768199984: Commerce API messages are JSON except file upload and download.
    assert contract.content_type == "application/json"
    assert contract.requires_bearer is True
    assert contract.required_groups == frozenset({"상품"})
    assert contract.mutating is True
    # EM §10, §11: ICBM policy, frozen per endpoint, never a library default.
    assert (contract.connect_timeout_s, contract.read_timeout_s) == (5.0, 30.0)
    assert contract.redirect is RedirectPolicy.NO_FOLLOW
    assert contract.predicate_revision == "m5-product-create-r1"
    # Deny-by-default: the CREATE sends no query at all, and the path holds no placeholder.
    assert contract.safe_query_keys == frozenset()
    assert contract.path_params == frozenset()
    # An adopted endpoint records no adoption gap; the gap list is exactly the unadopted ones.
    assert CREATE_ENDPOINT not in ADOPTION_GAPS


def test_the_absent_provider_idempotency_is_recorded_not_assumed() -> None:
    # ADR-0014 §17.2: adoption records the provider's actual idempotency — there is none — and
    # binds the endpoint to §28's never-resend rule instead of inventing a replay guarantee.
    assert CREATE_PROVIDER_IDEMPOTENCY == "NONE_DOCUMENTED"
    assert CREATE_AUTOMATIC_RETRY_BUDGET == 0


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, SUCCESS_BODY, True),
        (200, documented(originProductNo="3005432100"), True),
        # The channel numbers are a family: 쇼핑윈도 is not every seller's, so either documented
        # number satisfies the predicate, and a false UNKNOWN is never manufactured from its
        # absence (EM §4.3).
        (200, documented(smartstoreChannelProductNo=None, windowChannelProductNo=5005432102), True),
        # Anything short of the whole documented success document fails closed (EM §4.3, §9):
        # without a usable origin number there is no identity to read back by, without a channel
        # number the documented response was not returned, and without the stored originProduct
        # there is no result data for the ADR-0014 §11 comparison.
        (200, documented(originProductNo=None), False),
        (200, documented(originProductNo=""), False),
        (200, documented(originProductNo=0), False),
        (200, documented(originProductNo=True), False),
        (200, documented(smartstoreChannelProductNo=None), False),
        (200, documented(smartstoreChannelProductNo=0), False),
        (200, documented(originProduct=None), False),
        (200, documented(originProduct={}), False),
        (200, documented(originProduct="stored"), False),
        (200, [], False),
        (200, None, False),
        (201, SUCCESS_BODY, False),
        (202, SUCCESS_BODY, False),
    ],
)
def test_the_success_predicate_is_the_whole_documented_success_document(
    status: int, body: object, expected: bool
) -> None:
    assert product_create_succeeded(status, body) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, "7"), (" 7 ", "7"), ("A-1", "A-1"), (0, None), (-1, None), (True, None), (1.5, None)],
)
def test_a_provider_product_number_is_normalized_or_refused(
    value: object, expected: str | None
) -> None:
    assert provider_product_no(value) == expected


# ---------------------------------------------------------------- the composed request


def test_the_request_is_composed_once_from_the_contract() -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    call(provider)
    (sent,) = provider.requests
    assert (sent.method, str(sent.url)) == ("POST", WIRE_URL)
    assert sent.url.query == b""
    assert sent.headers["Authorization"] == f"Bearer {BEARER}"
    assert sent.headers["Content-Type"] == "application/json"
    assert json.loads(sent.read()) == DOCUMENT.body
    assert sent.extensions["timeout"] == {"connect": 5.0, "read": 30.0, "write": 5.0, "pool": 5.0}
    # No idempotency header is invented for a provider that documents none.
    assert not [name for name in sent.headers if "idempot" in name.lower()]


@pytest.mark.parametrize(
    "changes",
    [
        {"document": {"originProduct": {}}},
        {"document": dataclasses.replace(DOCUMENT, body={})},
        {"access_token": "bad token"},
        {"credential_generation": 0},
        {"session_generation": 0},
    ],
)
def test_a_request_outside_the_contract_never_reaches_the_transport(
    changes: dict[str, Any], no_transport: list[str]
) -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    error = failure(provider, **changes)
    assert provider.requests == [] and no_transport == []
    # A local contract violation: FATAL, and nothing was applied because nothing was sent.
    assert error.error_class is ErrorClass.FATAL
    assert error.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


def test_only_a_typed_create_request_composes(no_transport: list[str]) -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    with pytest.raises(SmartStoreCallError):
        caller(provider).call(CREATE_ENDPOINT, DOCUMENT)
    assert provider.requests == [] and no_transport == []


# ---------------------------------------------------------------- the typed, sanitized result


def test_a_success_returns_the_identifiers_and_only_the_retained_fields() -> None:
    result = call(Provider(httpx.Response(200, json=SUCCESS_BODY)))
    assert result.origin_product_no == str(ORIGIN_NO)
    assert result.channel_product_nos == (str(CHANNEL_NO),)
    assert result.http_status == 200
    # Deny-by-default retention: the identifiers and the product data the profile allows, and
    # nothing else — an unlisted field never crosses the boundary or reaches a digest (§15).
    assert result.retained == {
        "originProduct": {"name": "테스트 상품", "salePrice": 19900, "stockQuantity": 7},
        "originProductNo": ORIGIN_NO,
        "smartstoreChannelProductNo": CHANNEL_NO,
    }


def test_a_channel_number_is_kept_when_named_and_never_invented() -> None:
    # The predicate proves at least one documented channel number; the other is reported only
    # when the response itself named it, and 쇼핑윈도 is never invented for a seller without one.
    one = Provider(httpx.Response(200, json=SUCCESS_BODY))
    assert call(one).channel_product_nos == (str(CHANNEL_NO),)
    window_only = documented(smartstoreChannelProductNo=None, windowChannelProductNo=5005432102)
    assert call(Provider(httpx.Response(200, json=window_only))).channel_product_nos == (
        "5005432102",
    )
    both = documented(windowChannelProductNo=5005432102)
    assert call(Provider(httpx.Response(200, json=both))).channel_product_nos == (
        str(CHANNEL_NO),
        "5005432102",
    )


# ---------------------------------------------------------------- outcome classification


@pytest.mark.parametrize("status", sorted(classify.DEFINITIVE_REJECTION_STATUSES))
def test_a_definitive_provider_rejection_proves_non_application(status: int) -> None:
    # ADR-0014 §28.3: an ordinary definitive rejection is NOT_APPLIED_PROVEN on its own Attempt
    # and never passes through UNKNOWN. ERRORS.md §15 admits it as a reviewed provider proof.
    error = failure(Provider(httpx.Response(status, json={"code": "BAD_REQUEST"})))
    assert error.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert error.http_status == status


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # A 2xx that fails the success predicate: schema drift, application not excluded. The
        # documented success document is the whole predicate, so a 200 missing the identifier,
        # the channel numbers or the stored originProduct is ambiguous, never a reported success.
        (200, {"message": "accepted"}),
        (200, documented(originProductNo=None)),
        (200, documented(smartstoreChannelProductNo=None)),
        (200, documented(originProduct=None)),
        # Never followed for a mutation; a 308 would replay the body (ERRORS.md §10.6).
        (308, {}),
        (301, {}),
        # A server error after transport handoff proves nothing about the mutation (§14.3).
        (500, {"code": "INTERNAL_SERVER_ERROR"}),
        (502, {}),
        (503, {}),
        # Throttling and request-timeout shapes stay ambiguous on purpose.
        (429, {"code": "GW.RATE_LIMIT"}),
        (429, {}),
        (408, {}),
        (425, {}),
    ],
)
def test_an_ambiguous_response_stays_unknown(status: int, body: object) -> None:
    assert failure(Provider(httpx.Response(status, json=body))).remote_outcome is (
        RemoteOutcome.UNKNOWN
    )


def test_a_truncated_or_malformed_response_stays_unknown() -> None:
    # ERRORS.md §15.2: a truncated or malformed provider response is ambiguous, never a rejection.
    truncated = Provider(httpx.Response(200, content=b'{"originProductNo":'))
    error = failure(truncated)
    assert error.remote_outcome is RemoteOutcome.UNKNOWN
    assert error.code == "SMARTSTORE_SUCCESS_PREDICATE_FAILED"


def test_a_gateway_attributed_rejection_is_never_a_definitive_rejection() -> None:
    # ERRORS.md §25 Q2 is open: a pre-service gateway rejection is not proven non-application.
    error = failure(Provider(httpx.Response(403, json={"code": "GW.IP_NOT_ALLOWED"})))
    assert error.remote_outcome is RemoteOutcome.UNKNOWN
    assert classify.definitive_rejection(CREATE_ENDPOINT, 403, "GW.IP_NOT_ALLOWED") is False


@pytest.mark.parametrize("status", sorted(classify.DEFINITIVE_REJECTION_STATUSES))
def test_an_unattributed_rejection_is_never_a_definitive_rejection(status: int) -> None:
    # ERRORS.md §5.2 identifies the gateway layer by a `GW.` code and §5.3 the API-server layer by
    # the provider's own error shape, so a response carrying neither identifies no layer: a
    # code-less 403 is as consistent with a pre-service gateway refusal as with an API-server one.
    # §8 Step 5 forbids promoting that, so the outcome stays UNKNOWN and the CREATE is not resent.
    assert classify.definitive_rejection(CREATE_ENDPOINT, status, None) is False
    assert failure(Provider(httpx.Response(status, json={}))).remote_outcome is (
        RemoteOutcome.UNKNOWN
    )
    # A body that did not parse at all carries no code either, so it is the same refusal.
    unparseable = Provider(httpx.Response(status, content=b'{"code":'))
    assert failure(unparseable).remote_outcome is RemoteOutcome.UNKNOWN


def test_the_reviewed_rejection_set_is_endpoint_specific() -> None:
    # ERRORS.md §15 requires an explicitly reviewed whitelist. The review covers CREATE only; the
    # already-adopted upload keeps the ambiguity contract of ADR-0014 §17.1 unchanged.
    assert set(classify.DEFINITIVE_REJECTION_ENDPOINTS) == {CREATE_ENDPOINT}
    for endpoint in ADOPTED:
        if endpoint is not CREATE_ENDPOINT:
            assert classify.definitive_rejection(endpoint, 400, "BAD_REQUEST") is False


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("name resolution"),
        httpx.ReadTimeout("no answer"),
        httpx.WriteTimeout("stalled"),
        httpx.RemoteProtocolError("reset"),
        RuntimeError("unfamiliar"),
    ],
)
def test_a_transport_failure_never_invents_non_application(error: Exception) -> None:
    # ERRORS.md §15.2: without instrumentation that positively proves the pre-send phase, the
    # outcome of a mutating request stays UNKNOWN — an exception name is never the proof.
    assert failure(Provider(error)).remote_outcome is RemoteOutcome.UNKNOWN


def test_a_transient_cause_does_not_make_a_replay_safe() -> None:
    # ADR-0014 §9: the cause and the outcome are independent, and automatic retry needs both a
    # retryable class **and** NOT_APPLIED_PROVEN. A 5xx is TRANSIENT with an UNKNOWN outcome.
    error = failure(Provider(httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"})))
    assert error.error_class is ErrorClass.TRANSIENT
    assert error.remote_outcome is RemoteOutcome.UNKNOWN


def test_a_rate_limited_cause_does_not_make_a_replay_safe() -> None:
    error = failure(Provider(httpx.Response(429, json={"code": "GW.RATE_LIMIT"})))
    assert error.error_class is ErrorClass.RATE_LIMITED
    assert error.remote_outcome is RemoteOutcome.UNKNOWN


# ---------------------------------------------------------------- the document boundary


def test_no_create_document_can_be_built_while_the_body_has_an_unproven_part() -> None:
    projection = project(UNSENDABLE_PAYLOAD)
    assert projection.sendable is False
    assert CHANNEL_PRODUCT_GAP in projection.gaps
    # The request media type is no longer a gap: the adoption froze application/json.
    assert not [gap for gap in projection.gaps if "media type" in gap]
    with pytest.raises(WireContractError) as refused:
        create_document(UNSENDABLE_PAYLOAD)
    assert refused.value.code == "WIRE_CONTRACT_UNPROVEN"
    assert CHANNEL_PRODUCT_GAP in refused.value.detail


def test_the_sanitized_canonical_document_carries_no_credential() -> None:
    canonical = DOCUMENT.canonical()
    assert canonical["listing_identity"] == IDENTITY
    assert canonical["body"] == DOCUMENT.body
    assert BEARER not in repr(canonical)


# ---------------------------------------------------------------- the sender seam


def test_production_wires_the_adopted_sender_without_a_session(no_transport: list[str]) -> None:
    # Adoption is a contract, never a credential (ADR-0020 §2.4). The production seam is
    # constructed with neither caller nor bearer, so it can transmit nothing.
    production = SmartStoreCreateSender()
    assert production.available() is False
    with pytest.raises(CreateSessionUnavailableError):
        production.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert no_transport == []


def test_a_wired_sender_is_available_only_while_a_session_answers() -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    assert sender(provider).available() is True
    assert SmartStoreCreateSender(caller(provider), lambda: None).available() is False
    assert SmartStoreCreateSender(None, Session).available() is False


def test_an_unsendable_snapshot_is_refused_before_transport(no_transport: list[str]) -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    handoff = send(provider)
    # ERRORS.md §15.1 item 1: a local pre-submit rejection, before any transport handoff.
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_class is ErrorClass.FATAL
    assert handoff.error_code == "WIRE_CONTRACT_UNPROVEN"
    assert handoff.marketplace_product_id is None
    assert provider.requests == [] and no_transport == []


def test_a_malformed_snapshot_payload_is_also_refused_before_transport(
    no_transport: list[str],
) -> None:
    provider = Provider(httpx.Response(200, json=SUCCESS_BODY))
    handoff = sender(provider).send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "WIRE_SNAPSHOT_PAYLOAD_MALFORMED"
    assert provider.requests == [] and no_transport == []


@pytest.mark.usefixtures("frozen_document")
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=SUCCESS_BODY),
        httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}),
        httpx.Response(400, json={"code": "BAD_REQUEST"}),
        httpx.ReadTimeout("no answer"),
    ],
)
def test_one_send_is_exactly_one_call_and_never_a_resend(
    response: httpx.Response | Exception,
) -> None:
    # ADR-0014 §28.3 / M5-08: the adapter owns no retry loop at all, whatever the outcome was.
    provider = Provider(response)
    send(provider)
    assert len(provider.requests) == 1


@pytest.mark.usefixtures("frozen_document")
def test_a_successful_handoff_is_applied_proven_and_never_a_confirmation() -> None:
    handoff = send(Provider(httpx.Response(200, json=SUCCESS_BODY)))
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    # The origin-product number is the identity the adopted read-back is performed by; the channel
    # number travels with it so neither provider identity is lost (ADR-0014 §28.2).
    assert handoff.marketplace_product_id == str(ORIGIN_NO)
    assert handoff.details["channel_product_nos"] == [str(CHANNEL_NO)]
    assert handoff.details["provider_idempotency"] == "NONE_DOCUMENTED"
    assert handoff.details["automatic_retry_budget"] == 0
    # A 2xx is not registration success (ADR-0014 §11): the handoff carries the sanitized response
    # for the read-back comparison to follow, and no verification of its own.
    assert handoff.sanitized_response == {
        "originProduct": {"name": "테스트 상품", "salePrice": 19900, "stockQuantity": 7},
        "originProductNo": ORIGIN_NO,
        "smartstoreChannelProductNo": CHANNEL_NO,
    }
    # The durable request digest is taken over the sanitized document, never over wire bytes.
    assert handoff.sanitized_request == DOCUMENT.canonical()
    assert BEARER not in repr(handoff)


@pytest.mark.usefixtures("frozen_document")
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(400, json={"code": "BAD_REQUEST"}), RemoteOutcome.NOT_APPLIED_PROVEN),
        (httpx.Response(500, json={}), RemoteOutcome.UNKNOWN),
        (httpx.Response(200, json={"message": "accepted"}), RemoteOutcome.UNKNOWN),
        (httpx.ReadTimeout("no answer"), RemoteOutcome.UNKNOWN),
    ],
)
def test_the_handoff_reports_the_callers_outcome_unchanged(
    response: httpx.Response | Exception, outcome: RemoteOutcome
) -> None:
    handoff = send(Provider(response))
    assert handoff.remote_outcome is outcome
    # No provider identity is invented for a failure, whatever the outcome axis says.
    assert handoff.marketplace_product_id is None
    assert handoff.details["endpoint_id"] == EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value
