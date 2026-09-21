"""The registry-gated SmartStore endpoint caller (ENDPOINT_MATRIX.md §13; M2 PR-A).

This is the only code that owns an HTTP client for SmartStore. Every provider call goes through
``SmartStoreEndpointCaller.call(endpoint_id, request)``, which:

* resolves only an ADOPTED endpoint; anything else fails here, before any network I/O;
* builds the request from the endpoint contract: the canonical SELF token form or the committed
  bearer, validated locally first;
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

from app.connect.marketplace.capability import RemoteOutcome
from app.core.egress import EGRESS
from app.core.errors import AppError
from app.core.safe_payload import safe_payload
from integrations.marketplaces.smartstore import classify
from integrations.marketplaces.smartstore.classify import Classification
from integrations.marketplaces.smartstore.registry import (
    BASE_URL,
    PROVIDER_HOST,
    EndpointContract,
    EndpointId,
    EndpointNotAdoptedError,
    resolve,
)
from integrations.marketplaces.smartstore.retention import retain
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
_IMAGE_UPLOAD = EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD
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
            },
        )
        self.error_class = classification.error_class
        self.classification = classification
        self.endpoint = endpoint
        self.phase = phase
        self.remote_outcome = outcome
        self.http_status = http_status


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
    if isinstance(request, AccountRequest | ProductReadRequest | ImageUploadRequest):
        return request.credential_generation, request.session_generation
    return None, None


@dataclass(frozen=True)
class _Wire:
    """One composed request: the path this call uses, its headers and its form body."""

    path: str
    headers: dict[str, str]
    form: dict[str, str]
    files: tuple[tuple[str, tuple[str, bytes, str]], ...] = ()


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
    if contract.endpoint_id in _PRODUCT_READS:
        if not isinstance(request, ProductReadRequest):
            raise _Preflight("SMARTSTORE_REQUEST_CONTRACT_VIOLATION")
        _bearer(
            headers, request.access_token, request.credential_generation, request.session_generation
        )
        (placeholder,) = contract.path_params
        return _Wire(_path(contract, **{placeholder: request.product_no}), headers, {})
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


def _json(content: bytes) -> object:
    try:
        return json.loads(content)
    except ValueError:
        return None


def _marker(value: object) -> str | None:
    return value if isinstance(value, str) and _PROVIDER_MARKER.fullmatch(value) else None


def _result(
    contract: EndpointContract, request: object, body: object, status: int
) -> TokenGrant | SellerAccount | ProductReadback | ImageUploadResponse:
    """The typed result of a response that passed the endpoint's success predicate."""
    fields = cast(dict[str, object], body)
    if contract.endpoint_id in _PRODUCT_READS:
        assert isinstance(request, ProductReadRequest)
        # Only the endpoint's retained-field allow-list crosses this boundary (ADR-0014 §15).
        return ProductReadback(
            endpoint_id=contract.endpoint_id,
            product_no=request.product_no,
            retained=retain(contract, fields),
            http_status=status,
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
        endpoint_id: Literal[EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD],
        request: ImageUploadRequest,
    ) -> ImageUploadResponse: ...

    @overload
    def call(
        self, endpoint_id: object, request: object
    ) -> TokenGrant | SellerAccount | ProductReadback | ImageUploadResponse: ...

    def call(
        self, endpoint_id: object, request: object
    ) -> TokenGrant | SellerAccount | ProductReadback | ImageUploadResponse:
        started, started_mono = datetime.now(UTC), time.monotonic()
        endpoint = _endpoint_name(endpoint_id)
        recorder = TraceRecorder()
        contract: EndpointContract | None = None
        status: int | None = None
        trace_id: str | None = None
        result: TokenGrant | SellerAccount | ProductReadback | ImageUploadResponse | None = None
        error: SmartStoreCallError | None = None
        try:
            contract = resolve(endpoint_id)
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
                    error = SmartStoreCallError(
                        endpoint,
                        classify.response(status, _marker(fields.get("code"))),
                        Phase.RESPONSE_RECEIVED,
                        remote_outcome(Phase.RESPONSE_RECEIVED),
                        http_status=status,
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
                data=wire.form or None,
                files=wire.files or None,
                extensions={"trace": recorder},
            )
            return client.send(request, follow_redirects=False)
