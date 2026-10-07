"""The registry-gated SmartStore endpoint caller (ENDPOINT_MATRIX.md §13; M2 PR-A).

This is the only code that owns an HTTP client for SmartStore. Every provider call goes through
``SmartStoreEndpointCaller.call(endpoint_id, request)``, which:

* resolves only an ADOPTED endpoint; anything else fails here, before any network I/O;
* builds the request from the endpoint contract: the canonical SELF token form, the committed
  bearer, a multipart artifact or a canonically encoded JSON document, validated locally first;
* composes ``BASE_URL + path`` exactly once;
* applies the endpoint's own connect/read timeouts and never follows a redirect;
* opens the egress grant for the provider host only, for this one call;
* evaluates the endpoint success predicate on every response, 2xx included;
* records one sanitized evidence line per call and raises a classified error on failure.

Callers receive typed results, never the client, the URL or the response.
"""

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast, overload

import httpx

from app.platform.core.egress import EGRESS
from app.platform.core.errors import AppError
from app.platform.core.safe_payload import safe_payload
from app.stages.connect.marketplace.capability import RemoteOutcome
from integrations.marketplaces.smartstore import classify
from integrations.marketplaces.smartstore.classify import Classification
from integrations.marketplaces.smartstore.product import (
    CreateDocument,
    WireContractError,
    completeness_gaps,
    verified,
)
from integrations.marketplaces.smartstore.registry import (
    BASE_URL,
    PROVIDER_HOST,
    EndpointContract,
    EndpointId,
    EndpointNotAdoptedError,
    resolve,
)
from integrations.marketplaces.smartstore.retention import retain, retained_query
from integrations.marketplaces.smartstore.search import (
    FIRST_PAGE,
    INT32_MAX,
    MAX_PAGE_SIZE,
    request_body,
)
from integrations.marketplaces.smartstore.signing import (
    TOKEN_FORM_FIELDS,
    ApplicationCredentials,
    SignatureError,
    token_form,
)
from integrations.marketplaces.smartstore.transmission import (
    Phase,
    TraceRecorder,
    remote_outcome,
    transmission_phase,
)

logger = logging.getLogger("icbm.connect.smartstore")

MARKETPLACE_KEY = "smartstore"
EGRESS_OWNER = f"marketplace:{MARKETPLACE_KEY}"
# A bearer is one header token: printable ASCII, no whitespace (no header injection).
_BEARER = re.compile(r"^[\x21-\x7e]+$")
# Provider codes and trace ids are kept only in this shape; anything else is dropped.
_PROVIDER_MARKER = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_TRACE_HEADER = "GNCP-GW-Trace-ID"
# A path placeholder value: one conservative URL segment, so nothing needs escaping and no value
# can traverse or extend the adopted path. This is a local safety bound, not a provider claim.
_PATH_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PRODUCT_READS = frozenset(
    {EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2, EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2}
)
_PRODUCT_CREATE = EndpointId.SMARTSTORE_PRODUCT_CREATE_V2
_IMAGE_UPLOAD = EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD
_PRODUCT_SEARCH = EndpointId.SMARTSTORE_PRODUCT_SEARCH
_PRODUCT_DELETE = EndpointId.SMARTSTORE_PRODUCT_DELETE_V2
_NOTICE_LIST = EndpointId.SMARTSTORE_NOTICE_TYPES
_NOTICE_TYPE = EndpointId.SMARTSTORE_NOTICE_TYPE_READ
_CATEGORY_LIST = EndpointId.SMARTSTORE_CATEGORY_LIST
_ADDRESSBOOK_LIST = EndpointId.SMARTSTORE_ADDRESSBOOK_LIST
# An official 상품정보제공고시 type code: upper-case words joined by underscores.
_NOTICE_TYPE_CODE = re.compile(r"^[A-Z][A-Z_]{1,39}$")
# The only seller code a search may carry: the ``smartstore-seller-management-code/v1`` projection
# of an ICBM listing identity (ruling R1), 30 lowercase hexadecimal characters. A search is never
# made with an operator's text, a product name or the 37-character internal identity.
_SEARCH_CODE = re.compile(r"^[0-9a-f]{30}$")
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/gif", "image/png", "image/bmp"})
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------- requests and results


@dataclass(frozen=True)
class TokenRequest:
    """Issue a token under the committed credential bundle (AUTH.md §14 steps 1-3)."""

    credentials: ApplicationCredentials
    # Millisecond Unix time, from the service's clock; signed and sent unchanged (AUTH §5).
    timestamp_ms: int


@dataclass(frozen=True)
class AccountRequest:
    """Read the seller account with the bearer of the current committed session (EM §7)."""

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int


@dataclass(frozen=True)
class TokenGrant:
    """A token candidate. It is not a session until durably committed (AUTH §13.2)."""

    access_token: str = field(repr=False)
    token_type: str
    expires_in: int
    credential_generation: int


@dataclass(frozen=True)
class SellerAccount:
    """The authenticated seller account and the generations it was read under (AUTH §13.3)."""

    account_uid: str = field(repr=False)
    account_id: str | None = field(repr=False)
    credential_generation: int
    session_generation: int


@dataclass(frozen=True)
class ProductReadRequest:
    """Read one product back by its provider number (M5 PR-D; packet 5746489554).

    ``product_no`` fills the single path placeholder of the adopted read-back. It is the provider's
    own identifier, so it is accepted only in the conservative shape below; the request carries no
    query at all, because both endpoints declare an empty safe query-key allow-list."""

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    product_no: str


@dataclass(frozen=True)
class ProductDeleteRequest:
    """Delete one origin product by its provider number (ADR-0018 §3.5).

    ``product_no`` fills the single path placeholder, in the same conservative shape as a
    read-back; the request carries no query and no body. The bearer exists only for this call.
    """

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    product_no: str


@dataclass(frozen=True)
class NoticeCatalogRequest:
    """Read the official 상품정보제공고시 type list (``notice_type`` is ``None``) or one type's
    content fields (notice coverage S0). No query, no body; the bearer exists only for this call."""

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    notice_type: str | None = None


@dataclass(frozen=True)
class CategoryListRequest:
    """Read the official category catalog, restricted to registrable leaf categories."""

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    last: bool = True


@dataclass(frozen=True)
class AddressBookListRequest:
    """Read one page of the seller's address book (출고지 / 반품·교환지)."""

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    page: int = 1


@dataclass(frozen=True)
class ProductCreateRequest:
    """Register one product through the adopted ``POST /v2/products`` (CREATE adoption slice).

    ``document`` is the frozen :class:`~integrations.marketplaces.smartstore.product.CreateDocument`
    the wire projection built from the **immutable** RegistrationSnapshot and nothing else. A plain
    mapping is not accepted here: only that type carries the projection's provenance and its
    validation against the adopted request contract, and its body is already canonical JSON, so an
    unchecked document — or one changed after the Snapshot was projected — cannot become a request.
    It carries business values only — no credential, no session and no tokenized material — so the
    same document is the sanitized canonical representation a durable digest is taken over
    (ADR-0014 §15, B4). The bearer is the wire secret: it exists only for this call and is never
    part of the request object's repr.
    """

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    document: CreateDocument = field(repr=False)


@dataclass(frozen=True)
class ProductSearchRequest:
    """One page of the seller-code search (SEARCH positive-only reconcile slice; S1).

    ``seller_management_code`` must be the R1 projection of an ICBM listing identity, and the body
    is exactly the documented seller-code search (``search.request_body``): no other filter is
    invented. ``page`` starts at 1 and ``size`` is at most 500.
    """

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    seller_management_code: str
    page: int
    size: int


@dataclass(frozen=True)
class ImageUploadRequest:
    """Upload exactly one immutable artifact in exactly one ``imageFiles`` part.

    The bytes are an execution input only. They are never written by this caller and are excluded
    from repr/log/evidence. A second artifact requires a distinct request object and call.
    """

    access_token: str = field(repr=False)
    credential_generation: int
    session_generation: int
    filename: str
    media_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class ProductReadback:
    """A read-back response reduced to the endpoint's retained-field allow-list.

    Nothing else survives this boundary: the caller never hands a raw provider body to REGISTER,
    so no unlisted field can reach a digest, a durable row or a log (ADR-0011 §3, ADR-0014 §15).
    """

    endpoint_id: EndpointId
    product_no: str
    retained: Mapping[str, object]
    http_status: int


@dataclass(frozen=True)
class NoticeCatalogResponse:
    """A notice read reduced to its retained-field allow-list. A JSON array body is retained under
    ``items``."""

    endpoint_id: EndpointId
    notice_type: str | None
    retained: Mapping[str, object]
    http_status: int


@dataclass(frozen=True)
class CategoryListResponse:
    """The documented category array after deny-by-default field retention."""

    retained: Mapping[str, object]
    http_status: int


@dataclass(frozen=True)
class AddressBookListResponse:
    """One page of the address book after deny-by-default field retention."""

    retained: Mapping[str, object]
    http_status: int


@dataclass(frozen=True)
class ProductDeleteResponse:
    """A deletion the provider answered with the documented success (``product_delete_succeeded``).
    Nothing of the body is retained."""

    product_no: str
    http_status: int


@dataclass(frozen=True)
class ProductCreateResponse:
    """A CREATE response reduced to the endpoint's retained-field allow-list.

    Only the approved provider identifiers and the safe product leaves survive this boundary; the
    caller never hands a raw provider body to REGISTER (ADR-0011 §3, ADR-0014 §15). Reaching this
    type means the endpoint success predicate passed — it does **not** mean a product exists: the
    response contract still has to recognize a usable ``originProductNo`` (``create.py``), and a
    2xx is never a registration confirmation (ADR-0014 §11).
    """

    retained: Mapping[str, object]
    http_status: int


@dataclass(frozen=True)
class ProductSearchPage:
    """One search page reduced to the endpoint's retained-field allow-list.

    Reaching this type means only that a documented page arrived. What the page proves is decided
    by the search contract (``search.py``) — and a page never proves remote absence.
    """

    retained: Mapping[str, object]
    http_status: int
    page: int


@dataclass(frozen=True)
class ImageUploadResponse:
    """Sanitized upload response; only the documented ``images[].url`` leaves survive."""

    retained: Mapping[str, object]
    http_status: int


class SmartStoreCallError(AppError):
    """A classified SmartStore call failure. It carries the class, the layer, the basis, the
    transmission phase and the remote-outcome decision; never a secret or a response body."""

    def __init__(
        self,
        endpoint: str,
        classification: Classification,
        phase: Phase,
        outcome: RemoteOutcome,
        *,
        http_status: int | None = None,
        provider_message: str | None = None,
        provider_invalid_input: str | None = None,
    ) -> None:
        super().__init__(
            classification.code,
            f"SmartStore {endpoint} failed ({classification.code})",
            details={
                "endpoint_id": endpoint,
                "failure_layer": classification.layer.value,
                "classification_basis": classification.basis.value,
                "transmission_phase": phase.value,
                "remote_outcome": outcome.value,
                "http_status": http_status,
                "provider_code": classification.provider_code,
                "provider_message": provider_message,
                "provider_invalid_input": provider_invalid_input,
            },
        )
        self.error_class = classification.error_class
        self.classification = classification
        self.endpoint = endpoint
        self.phase = phase
        self.remote_outcome = outcome
        self.http_status = http_status
        self.provider_message = provider_message
        self.provider_invalid_input = provider_invalid_input


class _Preflight(Exception):
    """A local request-contract violation: the request never reaches the transport."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _endpoint_name(endpoint_id: object) -> str:
    return endpoint_id.value if isinstance(endpoint_id, EndpointId) else "UNRECOGNIZED"


def _generations(request: object) -> tuple[int | None, int | None]:
    if isinstance(request, TokenRequest):
        return request.credentials.credential_generation, None
    if isinstance(
        request,
        AccountRequest
        | ProductReadRequest
        | ProductCreateRequest
        | ProductSearchRequest
        | ImageUploadRequest
        | CategoryListRequest
        | AddressBookListRequest,
    ):
        return request.credential_generation, request.session_generation
    return None, None


@dataclass(frozen=True)
class _Wire:
    """One composed request: the path this call uses, its headers and its body."""

    path: str
    headers: dict[str, str]
    form: dict[str, str]
    files: tuple[tuple[str, tuple[str, bytes, str]], ...] = ()
    # A pre-encoded body, used by the JSON endpoints: the bytes are produced here, from the typed
    # document, so the media type of the wire is the endpoint contract's and nothing else.
    content: bytes | None = None
    query: dict[str, str] = field(default_factory=dict)


def _bearer(headers: dict[str, str], token: str, credentials: int, session: int) -> None:
    if not _BEARER.fullmatch(token):
        raise _Preflight("SMARTSTORE_BEARER_UNUSABLE")
    if credentials < 1 or session < 1:
        raise _Preflight("SMARTSTORE_SESSION_NOT_COMMITTED")
    headers["Authorization"] = f"Bearer {token}"


def _path(contract: EndpointContract, **params: str) -> str:
    """The endpoint path with its placeholders filled. A value outside the conservative shape, or
    a placeholder set that does not match the template exactly, never reaches the transport."""
    if set(params) != contract.path_params:
        raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
    if any(not _PATH_VALUE.fullmatch(value) for value in params.values()):
        raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
    path = contract.path
    for name, value in params.items():
        path = path.replace("{" + name + "}", value)
    return path


def _compose(contract: EndpointContract, request: object) -> _Wire:
    """Headers, path and form body for one adopted endpoint, validated before any transport
    exists."""
    headers = {"Accept": "application/json"}
    if contract.endpoint_id is EndpointId.SMARTSTORE_AUTH_TOKEN:
        if not isinstance(request, TokenRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        try:
            form = token_form(request.credentials, request.timestamp_ms)
        except SignatureError as exc:
            raise _Preflight("SMARTSTORE_SIGNATURE_UNAVAILABLE") from exc
        except ValueError as exc:
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION") from exc
        # AUTH §21 / EM §6: exactly the SELF fields, never account_id.
        if set(form) != TOKEN_FORM_FIELDS:
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        assert contract.content_type is not None
        headers["Content-Type"] = contract.content_type
        return _Wire(contract.path, headers, form)
    if contract.endpoint_id is EndpointId.SMARTSTORE_SELLER_ACCOUNT:
        if not isinstance(request, AccountRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        return _Wire(contract.path, headers, {})
    if contract.endpoint_id is _CATEGORY_LIST:
        if not isinstance(request, CategoryListRequest) or request.last is not True:
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        try:
            query = retained_query(contract, {"last": "true"})
        except ValueError as exc:  # pragma: no cover - registry/caller contract drift
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION") from exc
        return _Wire(contract.path, headers, {}, query=query)
    if contract.endpoint_id is _ADDRESSBOOK_LIST:
        if (
            not isinstance(request, AddressBookListRequest)
            or isinstance(request.page, bool)
            or not 1 <= request.page <= 100
        ):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        try:
            query = retained_query(contract, {"page": str(request.page)})
        except ValueError as exc:  # pragma: no cover - registry/caller contract drift
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION") from exc
        return _Wire(contract.path, headers, {}, query=query)
    if contract.endpoint_id in _PRODUCT_READS:
        if not isinstance(request, ProductReadRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        (placeholder,) = contract.path_params
        return _Wire(_path(contract, **{placeholder: request.product_no}), headers, {})
    if contract.endpoint_id in (_NOTICE_LIST, _NOTICE_TYPE):
        if not isinstance(request, NoticeCatalogRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        if contract.endpoint_id is _NOTICE_LIST:
            if request.notice_type is not None:
                raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
            return _Wire(contract.path, headers, {})
        if request.notice_type is None or not _NOTICE_TYPE_CODE.fullmatch(request.notice_type):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        (placeholder,) = contract.path_params
        return _Wire(_path(contract, **{placeholder: request.notice_type}), headers, {})
    if contract.endpoint_id is _PRODUCT_DELETE:
        if not isinstance(request, ProductDeleteRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        (placeholder,) = contract.path_params
        return _Wire(_path(contract, **{placeholder: request.product_no}), headers, {})
    if contract.endpoint_id is _PRODUCT_CREATE:
        # The document type is the provenance gate: only the wire projection produces one, and it
        # produces one only after validating the whole request against the adopted contract.
        if not isinstance(request, ProductCreateRequest) or not isinstance(
            request.document, CreateDocument
        ):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        # The type alone is no proof: the document is re-validated against the adopted contract
        # here, at the one wire boundary, so a directly built or injected document never passes.
        try:
            verified(request.document)
        except WireContractError as refused:
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION") from refused
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        assert contract.content_type is not None
        headers["Content-Type"] = contract.content_type
        return _Wire(contract.path, headers, {}, content=_json_body(request.document))
    if contract.endpoint_id is _PRODUCT_SEARCH:
        if (
            not isinstance(request, ProductSearchRequest)
            or not isinstance(request.seller_management_code, str)
            or not _SEARCH_CODE.fullmatch(request.seller_management_code)
            or isinstance(request.page, bool)
            or not isinstance(request.page, int)
            or not FIRST_PAGE <= request.page <= INT32_MAX
            or isinstance(request.size, bool)
            or not isinstance(request.size, int)
            or not 1 <= request.size <= MAX_PAGE_SIZE
        ):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        assert contract.content_type is not None
        headers["Content-Type"] = contract.content_type
        body = request_body(request.seller_management_code, request.page, request.size)
        content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _Wire(contract.path, headers, {}, content=content)
    if contract.endpoint_id is _IMAGE_UPLOAD:
        if not isinstance(request, ImageUploadRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        if (
            not _FILENAME.fullmatch(request.filename)
            or request.media_type not in _IMAGE_MEDIA_TYPES
            or not isinstance(request.content, bytes)
            or not request.content
        ):
            raise _Preflight("SMARTSTORE_IMAGE_UPLOAD_ARTIFACT_UNUSABLE")
        # Do not set Content-Type by hand: httpx supplies the required multipart boundary.
        return _Wire(
            contract.path,
            headers,
            {},
            (("imageFiles", (request.filename, request.content, request.media_type)),),
        )
    raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")


def _json_body(document: CreateDocument) -> bytes:
    """The canonical JSON bytes of one frozen request document.

    The projection encoded them deterministically (sorted keys, no insignificant space, UTF-8) when
    it validated and froze the document, so the wire body is a function of that checked document
    alone and nothing can have changed since. An empty body would be a local contract violation: it
    never reaches the transport, so nothing can have been applied.
    """
    body = document.encoded()
    if not body or body == b"{}":
        raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
    return body


def _json(content: bytes) -> object:
    try:
        return json.loads(content)
    except ValueError:
        return None


def _marker(value: object) -> str | None:
    return value if isinstance(value, str) and _PROVIDER_MARKER.fullmatch(value) else None


def _diagnostic_text(value: object, *, forbidden: tuple[str, ...] = ()) -> str | None:
    """Retain one provider diagnostic, never the body or an echoed submitted value."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    lowered = text.lower()
    secret_markers = ("bearer ", "access_token", "client_secret")
    if (
        not text
        or any(marker in lowered for marker in secret_markers)
        or any(secret and secret in text for secret in forbidden)
    ):
        return None
    return text[:200]


def _invalid_input(fields: Mapping[str, object], *, forbidden: tuple[str, ...] = ()) -> str | None:
    values = fields.get("invalidInputs")
    if not isinstance(values, list):
        return None
    summaries: list[str] = []
    for value in values[:3]:
        if not isinstance(value, dict):
            continue
        name = _marker(value.get("name"))
        kind = _marker(value.get("type"))
        message = _diagnostic_text(value.get("message"), forbidden=forbidden)
        parts = [part for part in (name, kind, message) if part]
        if parts:
            summaries.append(": ".join(parts))
    return _diagnostic_text("; ".join(summaries), forbidden=forbidden)


_Result = (
    TokenGrant
    | SellerAccount
    | ProductReadback
    | ProductCreateResponse
    | ProductDeleteResponse
    | NoticeCatalogResponse
    | CategoryListResponse
    | AddressBookListResponse
    | ProductSearchPage
    | ImageUploadResponse
)


def _result(contract: EndpointContract, request: object, body: object, status: int) -> _Result:
    """The typed result of a response that passed the endpoint's success predicate."""
    if contract.endpoint_id in (_NOTICE_LIST, _NOTICE_TYPE):
        assert isinstance(request, NoticeCatalogRequest)
        wrapped = body if isinstance(body, dict) else {"items": body}
        return NoticeCatalogResponse(
            endpoint_id=contract.endpoint_id,
            notice_type=request.notice_type,
            retained=retain(contract, wrapped),
            http_status=status,
        )
    if contract.endpoint_id is _ADDRESSBOOK_LIST:
        assert isinstance(request, AddressBookListRequest)
        return AddressBookListResponse(retained=retain(contract, body), http_status=status)
    if contract.endpoint_id is _CATEGORY_LIST:
        assert isinstance(request, CategoryListRequest)
        return CategoryListResponse(
            retained=retain(contract, {"items": body}),
            http_status=status,
        )
    fields = cast(dict[str, object], body)
    if contract.endpoint_id is _PRODUCT_DELETE:
        assert isinstance(request, ProductDeleteRequest)
        return ProductDeleteResponse(product_no=request.product_no, http_status=status)
    if contract.endpoint_id is _PRODUCT_CREATE:
        assert isinstance(request, ProductCreateRequest)
        # Only the endpoint's retained-field allow-list crosses this boundary (ADR-0014 §15).
        return ProductCreateResponse(retained=retain(contract, fields), http_status=status)
    if contract.endpoint_id in _PRODUCT_READS:
        assert isinstance(request, ProductReadRequest)
        # Only the endpoint's retained-field allow-list crosses this boundary (ADR-0014 §15).
        return ProductReadback(
            endpoint_id=contract.endpoint_id,
            product_no=request.product_no,
            retained=retain(contract, fields),
            http_status=status,
        )
    if contract.endpoint_id is _PRODUCT_SEARCH:
        assert isinstance(request, ProductSearchRequest)
        # Only the endpoint's retained-field allow-list crosses this boundary (ADR-0014 §15).
        return ProductSearchPage(
            retained=retain(contract, fields), http_status=status, page=request.page
        )
    if contract.endpoint_id is _IMAGE_UPLOAD:
        assert isinstance(request, ImageUploadRequest)
        return ImageUploadResponse(retained=retain(contract, fields), http_status=status)
    if contract.endpoint_id is EndpointId.SMARTSTORE_AUTH_TOKEN:
        assert isinstance(request, TokenRequest)
        return TokenGrant(
            access_token=cast(str, fields["access_token"]),
            token_type=cast(str, fields["token_type"]),
            expires_in=cast(int, fields["expires_in"]),
            credential_generation=request.credentials.credential_generation,
        )
    assert isinstance(request, AccountRequest)
    account_id = fields.get("accountId")
    return SellerAccount(
        account_uid=cast(str, fields["accountUid"]),
        # Secondary evidence when available; never a substitute for accountUid (EM §7).
        account_id=account_id if isinstance(account_id, str) and account_id else None,
        credential_generation=request.credential_generation,
        session_generation=request.session_generation,
    )


class SmartStoreEndpointCaller:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        # Tests substitute a fake transport; production always uses the network stack.
        self._transport = transport

    @overload
    def call(
        self, endpoint_id: Literal[EndpointId.SMARTSTORE_AUTH_TOKEN], request: TokenRequest
    ) -> TokenGrant: ...

    @overload
    def call(
        self, endpoint_id: Literal[EndpointId.SMARTSTORE_SELLER_ACCOUNT], request: AccountRequest
    ) -> SellerAccount: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[
            EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
            EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2,
        ],
        request: ProductReadRequest,
    ) -> ProductReadback: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_PRODUCT_CREATE_V2],
        request: ProductCreateRequest,
    ) -> ProductCreateResponse: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_PRODUCT_SEARCH],
        request: ProductSearchRequest,
    ) -> ProductSearchPage: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD],
        request: ImageUploadRequest,
    ) -> ImageUploadResponse: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_PRODUCT_DELETE_V2],
        request: ProductDeleteRequest,
    ) -> ProductDeleteResponse: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[
            EndpointId.SMARTSTORE_NOTICE_TYPES, EndpointId.SMARTSTORE_NOTICE_TYPE_READ
        ],
        request: NoticeCatalogRequest,
    ) -> NoticeCatalogResponse: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_CATEGORY_LIST],
        request: CategoryListRequest,
    ) -> CategoryListResponse: ...

    @overload
    def call(
        self,
        endpoint_id: Literal[EndpointId.SMARTSTORE_ADDRESSBOOK_LIST],
        request: AddressBookListRequest,
    ) -> AddressBookListResponse: ...

    @overload
    def call(self, endpoint_id: object, request: object) -> _Result: ...

    def call(self, endpoint_id: object, request: object) -> _Result:
        started, started_mono = datetime.now(UTC), time.monotonic()
        endpoint = _endpoint_name(endpoint_id)
        recorder = TraceRecorder()
        contract: EndpointContract | None = None
        status: int | None = None
        trace_id: str | None = None
        result: _Result | None = None
        error: SmartStoreCallError | None = None
        try:
            contract = resolve(endpoint_id)
            if contract.endpoint_id is _PRODUCT_CREATE:
                # Before the request is composed: no body is encoded for an incomplete CREATE.
                self._require_complete(request)
            wire = _compose(contract, request)
        except EndpointNotAdoptedError as exc:
            error = self._local(endpoint, "SMARTSTORE_ENDPOINT_NOT_ADOPTED", exc)
        except _Preflight as exc:
            error = self._local(endpoint, exc.code, exc)
        else:
            try:
                response = self._send(contract, wire, recorder)
            except Exception as exc:  # every failure without a response is classified by phase
                phase = transmission_phase(recorder.events, exc)
                error = SmartStoreCallError(
                    endpoint,
                    classify.transport(
                        phase, known_transport_failure=isinstance(exc, httpx.TransportError)
                    ),
                    phase,
                    remote_outcome(phase),
                )
                error.__cause__ = exc
            else:
                status = response.status_code
                body = _json(response.content)
                fields = body if isinstance(body, dict) else {}
                trace_id = _marker(fields.get("traceId")) or _marker(
                    response.headers.get(_TRACE_HEADER)
                )
                if contract.success_predicate(status, body):
                    result = _result(contract, request, body, status)
                else:
                    forbidden = (
                        (request.access_token,)
                        if isinstance(request, ProductCreateRequest)
                        and contract.endpoint_id is _PRODUCT_CREATE
                        else ()
                    )
                    provider_message = (
                        _diagnostic_text(fields.get("message"), forbidden=forbidden)
                        if contract.endpoint_id is _PRODUCT_CREATE
                        else None
                    )
                    provider_invalid_input = (
                        _invalid_input(fields, forbidden=forbidden)
                        if contract.endpoint_id is _PRODUCT_CREATE
                        else None
                    )
                    provider_code = _marker(fields.get("code"))
                    rejection = (
                        classify.create_rejection(status, provider_code)
                        if contract.endpoint_id is _PRODUCT_CREATE
                        else None
                    )
                    error = SmartStoreCallError(
                        endpoint,
                        rejection or classify.response(status, provider_code),
                        Phase.RESPONSE_RECEIVED,
                        # A definitive CREATE rejection proves nothing was applied; every other
                        # received failure is UNKNOWN (ADR-0014 §28.3 as amended 2026-10-07).
                        RemoteOutcome.NOT_APPLIED_PROVEN
                        if rejection is not None
                        else remote_outcome(Phase.RESPONSE_RECEIVED),
                        http_status=status,
                        provider_message=provider_message,
                        provider_invalid_input=provider_invalid_input,
                    )
        credential_generation, session_generation = _generations(request)
        logger.info(
            "smartstore.request",
            extra=safe_payload(
                marketplace_key=MARKETPLACE_KEY,
                endpoint_id=endpoint,
                method=contract.method if contract else None,
                credential_generation=credential_generation,
                session_generation=session_generation,
                connect_timeout_s=contract.connect_timeout_s if contract else None,
                read_timeout_s=contract.read_timeout_s if contract else None,
                redirect_policy=contract.redirect if contract else None,
                predicate_revision=contract.predicate_revision if contract else None,
                started_at=started,
                finished_at=datetime.now(UTC),
                latency_ms=round((time.monotonic() - started_mono) * 1000, 1),
                retry_count=0,
                result_class="SUCCEEDED" if error is None else error.code,
                http_status=status,
                provider_trace_id=trace_id,
                provider_message=error.provider_message if error else None,
                provider_invalid_input=error.provider_invalid_input if error else None,
                error_class=error.error_class if error else None,
                provider_code=error.classification.provider_code if error else None,
                failure_layer=error.classification.layer if error else None,
                classification_basis=error.classification.basis if error else None,
                transmission_phase=error.phase if error else None,
                remote_outcome=error.remote_outcome if error else None,
                transmission_events=list(recorder.events),
            ),
        )
        if error is not None:
            raise error
        assert result is not None
        return result

    def _require_complete(self, request: object) -> None:
        """Refuse a CREATE whose validated document lacks a part the provider requires on
        registration (``product.completeness_gaps``).

        It runs before the request is composed, so no body is ever encoded for an incomplete
        CREATE, and it reads the document itself: no projection, sender or caller of this method
        can declare a request complete. A document that is not a validated projection output is a
        contract violation first. At this adoption every CREATE document has gaps, so every CREATE
        is refused here, locally and before any transport — ``NOT_APPLIED_PROVEN`` by the
        pre-handoff whitelist.
        """
        document = getattr(request, "document", None)
        if not isinstance(request, ProductCreateRequest) or not isinstance(
            document, CreateDocument
        ):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        try:
            verified(document)
        except WireContractError as refused:
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION") from refused
        if completeness_gaps(document):
            raise _Preflight("SMARTSTORE_CREATE_REQUEST_INCOMPLETE")

    @staticmethod
    def _local(endpoint: str, code: str, cause: Exception) -> SmartStoreCallError:
        error = SmartStoreCallError(
            endpoint,
            classify.local_contract(code),
            Phase.LOCAL_PREFLIGHT,
            remote_outcome(Phase.LOCAL_PREFLIGHT),
        )
        error.__cause__ = cause
        return error

    def _send(
        self, contract: EndpointContract, wire: _Wire, recorder: TraceRecorder
    ) -> httpx.Response:
        # EM §10: the endpoint's own timeouts, never library defaults. The contract fixes connect
        # and read; writing the small request and waiting for a pooled slot use the connect bound.
        timeout = httpx.Timeout(
            connect=contract.connect_timeout_s,
            read=contract.read_timeout_s,
            write=contract.connect_timeout_s,
            pool=contract.connect_timeout_s,
        )
        with (
            EGRESS.grant(EGRESS_OWNER, {PROVIDER_HOST}),
            httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client,
        ):
            request = client.build_request(
                contract.method.value,
                BASE_URL + wire.path,
                headers=wire.headers,
                params=wire.query or None,
                content=wire.content,
                data=wire.form or None,
                files=wire.files or None,
                extensions={"trace": recorder},
            )
            return client.send(request, follow_redirects=False)
