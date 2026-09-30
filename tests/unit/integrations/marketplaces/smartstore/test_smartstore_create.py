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

from app.platform.core.errors import ErrorClass
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore import create, execution, product
from integrations.marketplaces.smartstore.caller import (
    ProductCreateRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import SmartStoreCreateSender
from integrations.marketplaces.smartstore.registry import EndpointId, resolve
from integrations.marketplaces.smartstore.retention import retain
from tests.support.smartstore_create_support import DeclaredProjectionSender, declared

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
    # E2 (Issue #89 `5868542027`): on registration the CREATE endpoint accepts only SALE.
    "statusType": "SALE",
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

    return DeclaredProjectionSender(
        caller=_caller(provider), bearer=lambda: bearer, projection=lambda payload: declared(FROZEN)
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

    sender = DeclaredProjectionSender(
        caller=_caller(provider), bearer=lambda: Bearer(), projection=lambda payload: Raw()
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_WIRE_CONTRACT_VIOLATION"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


def _forged(canonical_json: str, **fields: str) -> product.CreateDocument:
    """A CreateDocument built directly, never through the validating projection."""
    return product.CreateDocument(
        encoding_version=fields.get("encoding_version", product.WIRE_ENCODING_VERSION),
        listing_identity=fields.get("listing_identity", IDENTITY),
        canonical_json=canonical_json,
    )


_FORGERIES = [
    # A path the adopted contract does not record, smuggled past the validation.
    _forged(json.dumps({"originProduct": {"evil": 1}})),
    # A valid body whose text is not its validated canonical form.
    _forged(json.dumps(DOCUMENT, indent=2, ensure_ascii=False)),
    # A valid body under a seller code that is not this identity's projection.
    _forged(FROZEN.canonical_json, listing_identity="icbm-" + "f" * 32),
    # Another encoding version.
    _forged(FROZEN.canonical_json, encoding_version="smartstore-register-wire/v0"),
    # Not JSON at all.
    _forged("not json"),
]


@pytest.mark.parametrize("forged", _FORGERIES)
def test_a_directly_built_document_never_reaches_the_wire(forged: product.CreateDocument) -> None:
    # The type's constructor proves nothing, so the one wire boundary re-validates the whole
    # document: a forged or injected CreateDocument is refused before any byte is written.
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    with pytest.raises(SmartStoreCallError) as refused:
        _caller(provider).call(
            EndpointId.SMARTSTORE_PRODUCT_CREATE_V2, ProductCreateRequest(BEARER, 3, 7, forged)
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert refused.value.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert provider.requests == []

    sender = DeclaredProjectionSender(
        caller=_caller(provider),
        bearer=lambda: Bearer(),
        projection=lambda payload: declared(forged),
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=forged.listing_identity)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_WIRE_CONTRACT_VIOLATION"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN


def test_the_validated_projection_document_survives_the_wire_check() -> None:
    assert product.verified(FROZEN) is FROZEN
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    _caller(provider).call(
        EndpointId.SMARTSTORE_PRODUCT_CREATE_V2, ProductCreateRequest(BEARER, 3, 7, FROZEN)
    )
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        # Deny-by-default: a path the captured official evidence does not record cannot be sent,
        # whatever built the mapping — including a required field whose value no ICBM owner decides.
        (_origin(stockQuantity=3), "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        (
            {**deepcopy(DOCUMENT), "smartstoreChannelProduct": {"naverShoppingRegistration": True}},
            "WIRE_DOCUMENT_FIELD_UNKNOWN",
        ),
        (
            {
                **deepcopy(DOCUMENT),
                "smartstoreChannelProduct": {"naverShoppingRegistration": False},
            },
            "WIRE_DOCUMENT_FIELD_UNKNOWN",
        ),
        # E2: on registration only SALE may be entered — the broader shared-schema values, and
        # SUSPENSION (an update input), are never a CREATE input.
        (_origin(statusType="WAIT"), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(statusType="SUSPENSION"), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(statusType="sale"), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(statusType=True), "WIRE_DOCUMENT_VALUE_INVALID"),
        (_origin(statusType=None), "WIRE_DOCUMENT_FIELD_MISSING"),
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
        # A zero-dimensional combination form is no supported option form, so it fails closed.
        (
            _origin(
                detailAttribute={
                    "sellerCodeInfo": {"sellerManagementCode": SELLER_CODE},
                    "optionInfo": {
                        "optionCombinationGroupNames": {},
                        "optionCombinations": [{"sellerManagerCode": "item-1"}],
                    },
                }
            ),
            "WIRE_OPTION_DIMENSIONS_MISSING",
        ),
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

    leaky = product.create_document(IDENTITY, _origin(name="Bearer abcdefghijklmnop"))
    sender = DeclaredProjectionSender(
        caller=_caller(provider),
        bearer=lambda: Bearer(),
        projection=lambda payload: declared(leaky),
    )
    handoff = sender.send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert provider.requests == []
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "SMARTSTORE_CREATE_REQUEST_UNSANITIZED"


def test_a_document_of_another_listing_identity_never_reaches_the_transport() -> None:
    # The frozen document is bound to the listing identity its Snapshot projected. The execution
    # owner hands the Intent's own identity, and a document of any other identity is refused
    # locally, before a session is even asked for: nothing is sent under the wrong Intent.
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))
    other = "icbm-" + "f" * 32
    assert other != IDENTITY
    handoff = _sender(provider).send(payload={}, idempotency_key="k", listing_identity=other)
    assert provider.requests == []
    assert handoff.error_code == "SMARTSTORE_CREATE_IDENTITY_MISMATCH"
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.marketplace_product_id is None
    # The same document under its own identity is the one that is sent.
    _sender(provider).send(payload={}, idempotency_key="k", listing_identity=IDENTITY)
    assert len(provider.requests) == 1


def test_an_unsendable_projection_never_reaches_the_transport() -> None:
    provider = Provider(httpx.Response(200, json={"originProductNo": 1}))

    unsendable = declared(FROZEN, gaps=(product.GAP_SHOPPING_REGISTRATION,))
    sender = DeclaredProjectionSender(
        caller=_caller(provider), bearer=lambda: Bearer(), projection=lambda payload: unsendable
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


ORIGIN_NO = 9900112233
CHANNEL_NO = 55
INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)


def test_the_documented_top_level_identifiers_are_read() -> None:
    # E3 (Issue #89 `5868542027`): originProductNo and smartstoreChannelProductNo are direct
    # top-level members of the success object, each an integer<int64>.
    reading = create.read(
        {
            "originProductNo": ORIGIN_NO,
            "smartstoreChannelProductNo": CHANNEL_NO,
            "windowChannelProductNo": 77,
        }
    )
    assert reading.readable is True
    assert (reading.origin_product_no, reading.smartstore_channel_product_no) == (
        ORIGIN_NO,
        CHANNEL_NO,
    )
    assert reading.window_channel_product_no == 77
    assert reading.problems == ()
    # The read-back identity is originProductNo: the adopted origin read-back is addressed by it.
    assert reading.marketplace_product_id == str(ORIGIN_NO)
    canonical = reading.canonical()
    assert canonical["identifier_read"] == "READ"
    assert canonical["response_contract_version"] == "smartstore-create-response/v2"
    assert canonical["identifiers"] == {
        "originProductNo": ORIGIN_NO,
        "smartstoreChannelProductNo": CHANNEL_NO,
        "windowChannelProductNo": 77,
    }


def test_a_missing_window_channel_identifier_alone_keeps_the_success_readable() -> None:
    # windowChannelProductNo belongs to the Shopping Window channel ICBM never emits; its absence
    # alone never makes the documented SmartStore success unreadable.
    reading = create.read({"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": CHANNEL_NO})
    assert reading.readable is True
    assert reading.window_channel_product_no is None
    assert reading.marketplace_product_id == str(ORIGIN_NO)


@pytest.mark.parametrize("value", [0, 1, INT64_MAX, INT64_MIN])
def test_the_whole_signed_int64_range_is_an_identifier(value: int) -> None:
    reading = create.read({"originProductNo": value, "smartstoreChannelProductNo": value})
    assert reading.readable is True
    assert reading.origin_product_no == value


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ({}, "originProductNo: MISSING"),
        ({"smartstoreChannelProductNo": CHANNEL_NO}, "originProductNo: MISSING"),
        ({"originProductNo": ORIGIN_NO}, "smartstoreChannelProductNo: MISSING"),
        # A bool is a Python int, and is refused explicitly.
        (
            {"originProductNo": True, "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        (
            {"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": False},
            "smartstoreChannelProductNo: NOT_INT64",
        ),
        # A numeric string is not the documented representation.
        (
            {"originProductNo": "9900112233", "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        (
            {"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": "55"},
            "smartstoreChannelProductNo: NOT_INT64",
        ),
        # Neither is a float, however integral.
        (
            {"originProductNo": 9900112233.0, "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        # Outside the signed 64-bit range.
        (
            {"originProductNo": INT64_MAX + 1, "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        (
            {"originProductNo": INT64_MIN - 1, "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        (
            {"originProductNo": None, "smartstoreChannelProductNo": CHANNEL_NO},
            "originProductNo: NOT_INT64",
        ),
        # A present Shopping Window identifier that is not an integer<int64> is a response the
        # evidence does not describe.
        (
            {
                "originProductNo": ORIGIN_NO,
                "smartstoreChannelProductNo": CHANNEL_NO,
                "windowChannelProductNo": "77",
            },
            "windowChannelProductNo: NOT_INT64",
        ),
        # Top level only: nothing nested is ever searched.
        (
            {"result": {"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": CHANNEL_NO}},
            "originProductNo: MISSING",
        ),
        (
            {
                "originProduct": {"originProductNo": ORIGIN_NO},
                "smartstoreChannelProductNo": CHANNEL_NO,
            },
            "originProductNo: MISSING",
        ),
        (
            {"list": [{"originProductNo": 1, "smartstoreChannelProductNo": 2}]},
            "originProductNo: MISSING",
        ),
    ],
)
def test_an_identifier_outside_the_documented_shape_is_never_read(
    body: dict[str, Any], problem: str
) -> None:
    reading = create.read(body)
    assert reading.readable is False
    assert reading.marketplace_product_id is None
    assert problem in reading.problems
    assert reading.canonical()["identifier_read"] == "UNREADABLE"


def test_a_readable_success_is_applied_proven_and_hands_on_the_read_back_identity() -> None:
    # F5: readable documented identifiers are provider-side application evidence. They name the
    # identity a read-back is made by — and nothing more: confirmation stays read-back plus
    # Snapshot comparison (ADR-0014 §11), which is the execution owner's, not this seam's.
    body = {"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": CHANNEL_NO}
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    assert handoff.marketplace_product_id == str(ORIGIN_NO)
    assert handoff.response_status == 200
    assert handoff.error_class is None and handoff.error_code is None
    contract = handoff.sanitized_response["response_contract"]
    assert contract["identifier_read"] == "READ"
    assert contract["response_contract_version"] == "smartstore-create-response/v2"
    assert contract["problems"] == []
    assert handoff.sanitized_response["retained"] == body


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"originProductNo": ORIGIN_NO},
        {"originProductNo": "9900112233", "smartstoreChannelProductNo": CHANNEL_NO},
        {"originProductNo": True, "smartstoreChannelProductNo": CHANNEL_NO},
        {"originProductNo": INT64_MAX + 1, "smartstoreChannelProductNo": CHANNEL_NO},
        {"result": {"originProductNo": ORIGIN_NO, "smartstoreChannelProductNo": CHANNEL_NO}},
        {
            "originProduct": {"originProductNo": 1, "name": "테스트 상품"},
            "smartstoreChannelProductNo": 55,
        },
        {"list": [{"originProductNo": 1}, {"originProductNo": 2}]},
        {"smartstoreChannelProductNo": CHANNEL_NO},
    ],
)
def test_an_unreadable_success_is_unknown_and_never_a_proven_absence(body: dict[str, Any]) -> None:
    # F4: a missing or wrong-type identifier proves nothing either way — the product may exist.
    # That is why it is UNKNOWN, never NOT_APPLIED_PROVEN, never a failure and never a resend
    # (ADR-0014 §28.3, M5-08, G3-07).
    handoff = _send(Provider(httpx.Response(200, json=body)))
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    assert handoff.marketplace_product_id is None
    assert handoff.error_code == "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"
    assert handoff.error_class is ErrorClass.UNKNOWN
    contract = handoff.sanitized_response["response_contract"]
    assert contract["identifier_read"] == "UNREADABLE"
    assert contract["problems"]
    assert contract["response_contract_version"] == "smartstore-create-response/v2"


def test_applied_proven_is_reached_only_through_a_readable_response() -> None:
    # Structural, so an edit cannot quietly reintroduce an identity read beside the contract: the
    # seam names APPLIED_PROVEN once, and sets a marketplace product id only from the reading.
    tree = ast.parse(Path(inspect.getsourcefile(execution) or "").read_text("utf-8"))
    (sender,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "SmartStoreCreateSender"
    ]
    applied = [
        node
        for node in ast.walk(sender)
        if isinstance(node, ast.Attribute) and node.attr == "APPLIED_PROVEN"
    ]
    assert len(applied) == 1
    product_ids = [
        node.value
        for node in ast.walk(sender)
        if isinstance(node, ast.keyword) and node.arg == "marketplace_product_id"
    ]
    assert [ast.unparse(value) for value in product_ids] == ["reading.marketplace_product_id"]


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
    # The retained body is kept as evidence of what came back; only its top-level identifiers
    # were read.
    assert handoff.sanitized_response["retained"]["originProduct"]["salePrice"] == 19900
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN


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


def test_the_production_sender_takes_no_projection() -> None:
    # Whether a CREATE request is complete is decided only by the gaps the adopted projection
    # records from the frozen Snapshot. The production sender therefore accepts no projection, no
    # projector and no sendability claim from its caller: its only inputs are the registry-gated
    # caller and the session source, and it always projects through ``product.project``.
    parameters = list(inspect.signature(SmartStoreCreateSender.__init__).parameters)
    assert parameters == ["self", "caller", "bearer"]
    source = inspect.getsource(SmartStoreCreateSender._projection)
    assert "product.project(payload)" in source
