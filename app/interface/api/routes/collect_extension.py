"""COLLECT: the extension capture transport (ADR-0019 E1; rulings ``5906290729`` B-3 and the owner
amendment ``5907095955`` for the policy read).

Two routes, and nothing else:

- ``GET  /api/v1/collect/extension/capture-policies/{supplier_key}`` — the reviewed capture
  policy, as exactly its canonical bytes. The extension reads it before every cut.
- ``POST /api/v1/collect/extension/captures`` — one product-scoped capture. The answer is an
  identity to read back through the ordinary run read, never a result.

Both require everything together: the loopback host (``TrustedHostMiddleware``), the
``X-ICBM-Client`` header, the pinned extension origin when an ``Origin`` is present, and the
pairing signature. Pairing replaces none of the others. A request is authenticated from its
headers before its body is read, so an unauthenticated request is refused as unauthenticated
whatever it carries.

**No CORS is added to the application.** Only these two exact paths answer a preflight, and only
for the paired extension's own origin. The DIRECT_URL submit and every other route are unchanged.
"""

import hashlib

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.interface.api.deps import ContainerDep
from app.interface.api.middleware import CLIENT_HEADER
from app.platform.core.errors import InputValidationError, PolicyBlockedError
from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.extension.pairing import (
    EMPTY_BODY_SHA256,
    SIGNED_HEADERS,
    ExtensionPairing,
    PairingRefused,
    VerifiedSender,
)
from app.stages.collect.extension.policy import MAX_HTML_BYTES
from app.stages.collect.extension.service import (
    AcceptedCapture,
    ExtensionCaptureService,
    ExtensionCeilingExceeded,
)

router = APIRouter(tags=["collect-extension"])

POLICY_PATH = "/api/v1/collect/extension/capture-policies/{supplier_key}"
CAPTURE_PATH = "/api/v1/collect/extension/captures"
POLICY_REVISION_HEADER = "X-ICBM-Capture-Policy-Revision"
POLICY_DIGEST_HEADER = "X-ICBM-Capture-Policy-Digest"
# The whole request: the capture, JSON-escaped at worst, and its small envelope. It bounds what is
# read into memory at all; the capture's own ceilings are applied to what it then holds.
MAX_REQUEST_BYTES = 2 * MAX_HTML_BYTES + 64 * 1024
_PREFLIGHT_HEADERS = ", ".join(("Content-Type", CLIENT_HEADER, *SIGNED_HEADERS))


def _authenticate(request: Request, pairing: ExtensionPairing) -> VerifiedSender:
    """Loopback is already enforced. Here: the client header, the pinned origin, the pairing."""
    if not request.headers.get(CLIENT_HEADER):
        raise PolicyBlockedError(
            "CLIENT_HEADER_REQUIRED", f"extension requests require the {CLIENT_HEADER} header"
        )
    return pairing.verify(
        method=request.method,
        path=request.url.path,
        origin=request.headers.get("origin"),
        headers=request.headers,
    )


def _allow_origin(response: Response, request: Request, sender: VerifiedSender) -> None:
    """Name the paired origin on a response the paired extension asked for, and nothing wider."""
    if request.headers.get("origin") == sender.origin:
        response.headers["Access-Control-Allow-Origin"] = sender.origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = (
            f"{POLICY_REVISION_HEADER}, {POLICY_DIGEST_HEADER}"
        )


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise ExtensionCeilingExceeded(
            "EXTENSION_CEILING_REQUEST_BYTES", "the request crosses the request ceiling"
        )
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_REQUEST_BYTES:
            raise ExtensionCeilingExceeded(
                "EXTENSION_CEILING_REQUEST_BYTES", "the request crosses the request ceiling"
            )
    return bytes(received)


@router.get(POLICY_PATH)
def capture_policy(supplier_key: str, request: Request, container: ContainerDep) -> Response:
    """The reviewed capture policy of one supplier: exactly its canonical bytes."""
    sender = _authenticate(request, container.extension_pairing)
    if sender.body_sha256 != EMPTY_BODY_SHA256:
        raise PairingRefused("EXTENSION_BODY_DIGEST_MISMATCH", "a policy read carries no body")
    policy = container.extension_capture.policy(supplier_key)
    response = Response(
        content=policy.canonical,
        media_type="application/json",
        headers={POLICY_REVISION_HEADER: policy.revision, POLICY_DIGEST_HEADER: policy.digest},
    )
    _allow_origin(response, request, sender)
    return response


def _envelope(body: bytes, sender: VerifiedSender) -> CaptureEnvelope:
    """The signed body as the exact envelope, or a refusal that names where and why only."""
    if hashlib.sha256(body).hexdigest() != sender.body_sha256:
        raise PairingRefused(
            "EXTENSION_BODY_DIGEST_MISMATCH", "the body is not the one the request signed"
        )
    try:
        return CaptureEnvelope.model_validate_json(body)
    except ValidationError as invalid:
        # Where and why only: never a submitted value.
        raise InputValidationError(
            "EXTENSION_PAYLOAD_INVALID",
            "the capture is not the exact envelope",
            details={
                "errors": [
                    {"type": error["type"], "loc": [str(part) for part in error["loc"]]}
                    for error in invalid.errors()[:10]
                ]
            },
        ) from None


def _accept(
    service: ExtensionCaptureService, body: bytes, sender: VerifiedSender
) -> AcceptedCapture:
    return service.ingest(_envelope(body, sender))


@router.post(CAPTURE_PATH, status_code=status.HTTP_202_ACCEPTED)
async def submit_capture(request: Request, container: ContainerDep) -> Response:
    """Accept one capture, or refuse it whole. Nothing here is a collection result.

    The order is fixed: the sender is proven from the headers, then the bounded body is read, then
    its digest, its envelope and the ingest. Only the body read runs on the event loop; the keyring
    read, the hashing and the parsing of up to a megabyte do not.
    """
    sender = await run_in_threadpool(_authenticate, request, container.extension_pairing)
    body = await _bounded_body(request)
    accepted = await run_in_threadpool(_accept, container.extension_capture, body, sender)
    response = JSONResponse(
        {
            "collection_run_id": accepted.collection_run_id,
            "job_id": accepted.job_id,
            "correlation_id": accepted.correlation_id,
            # A transport state only. The outcome is read back from the run.
            "state": "ACCEPTED",
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
    _allow_origin(response, request, sender)
    return response


def _preflight(request: Request, container: ContainerDep, method: str) -> Response:
    """The only preflight answers this application gives: these exact paths, the paired origin."""
    paired = container.extension_pairing.current()
    origin = request.headers.get("origin")
    if (
        paired is None
        or origin != f"chrome-extension://{paired.extension_id}"
        or request.headers.get("access-control-request-method", "").upper() != method
    ):
        raise PolicyBlockedError("EXTENSION_PREFLIGHT_REFUSED", "this preflight is not allowed")
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": method,
            "Access-Control-Allow-Headers": _PREFLIGHT_HEADERS,
            "Access-Control-Max-Age": "60",
            "Vary": "Origin",
        },
    )


@router.options(POLICY_PATH)
def capture_policy_preflight(
    supplier_key: str, request: Request, container: ContainerDep
) -> Response:
    return _preflight(request, container, "GET")


@router.options(CAPTURE_PATH)
def submit_capture_preflight(request: Request, container: ContainerDep) -> Response:
    return _preflight(request, container, "POST")
