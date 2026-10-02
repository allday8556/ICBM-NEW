"""COLLECT: the extension capture transport (ADR-0019 E1; rulings ``5906290729`` B-3 and the owner
amendment ``5907095955`` for the policy read).

The capture routes (E1):

- ``GET  /api/v1/collect/extension/capture-policies/{supplier_key}`` — the reviewed capture
  policy, as exactly its canonical bytes. The extension reads it before every cut.
- ``POST /api/v1/collect/extension/captures`` — one product-scoped capture, or one queue read's
  capture with its ticket. The answer is an identity to read back through the ordinary run read,
  never a result.

The list-queue routes (ADR-0019 §8.1, E3):

- ``GET  /api/v1/collect/extension/queue-policies/{supplier_key}`` — the supplier's reviewed product
  path form and its declared queue limits.
- ``POST /api/v1/collect/extension/queues`` — declare one queue from discovered product URLs.
- ``GET  /api/v1/collect/extension/queues/{queue_id}`` — the queue as it stands.
- ``POST /api/v1/collect/extension/queues/{queue_id}/next`` — wait, one issued read, or done.
- ``POST /api/v1/collect/extension/queues/{queue_id}/cancel`` — cancel every unissued read.

Every route requires everything together: the loopback host (``TrustedHostMiddleware``), the
``X-ICBM-Client`` header, the pinned extension origin when an ``Origin`` is present, and the
pairing signature. Pairing replaces none of the others. A request is authenticated from its
headers before its body is read, so an unauthenticated request is refused as unauthenticated
whatever it carries.

**No CORS is added to the application.** Only these exact paths answer a preflight, and only for
the paired extension's own origin. The DIRECT_URL submit and every other route are unchanged.
"""

import hashlib
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)
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
from app.stages.collect.extension.queue import (
    QueueAnswer,
    QueueDeclaration,
    QueueItemView,
    QueueView,
)
from app.stages.collect.extension.service import (
    AcceptedCapture,
    ExtensionCaptureService,
    ExtensionCeilingExceeded,
)

router = APIRouter(tags=["collect-extension"])

POLICY_PATH = "/api/v1/collect/extension/capture-policies/{supplier_key}"
CAPTURE_PATH = "/api/v1/collect/extension/captures"
QUEUE_POLICY_PATH = "/api/v1/collect/extension/queue-policies/{supplier_key}"
QUEUES_PATH = "/api/v1/collect/extension/queues"
QUEUE_PATH = "/api/v1/collect/extension/queues/{queue_id}"
QUEUE_NEXT_PATH = "/api/v1/collect/extension/queues/{queue_id}/next"
QUEUE_CANCEL_PATH = "/api/v1/collect/extension/queues/{queue_id}/cancel"
POLICY_REVISION_HEADER = "X-ICBM-Capture-Policy-Revision"
POLICY_DIGEST_HEADER = "X-ICBM-Capture-Policy-Digest"
# The whole request: the capture, JSON-escaped at worst, and its small envelope. It bounds what is
# read into memory at all; the capture's own ceilings are applied to what it then holds.
MAX_REQUEST_BYTES = 2 * MAX_HTML_BYTES + 64 * 1024
# A queue declaration: product URLs only, bounded again by the supplier's declared link count.
MAX_QUEUE_REQUEST_BYTES = 512 * 1024
# The most links a declaration may parse at all, before the supplier's own bound is applied.
MAX_PARSED_LINKS = 1000
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


async def _bounded_body(request: Request, ceiling: int = MAX_REQUEST_BYTES) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > ceiling:
        raise ExtensionCeilingExceeded(
            "EXTENSION_CEILING_REQUEST_BYTES", "the request crosses the request ceiling"
        )
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > ceiling:
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


def _signed[M: BaseModel](body: bytes, sender: VerifiedSender, model: type[M], what: str) -> M:
    """The signed body as the exact model, or a refusal that names where and why only."""
    if hashlib.sha256(body).hexdigest() != sender.body_sha256:
        raise PairingRefused(
            "EXTENSION_BODY_DIGEST_MISMATCH", "the body is not the one the request signed"
        )
    try:
        return model.model_validate_json(body)
    except ValidationError as invalid:
        # Where and why only: never a submitted value.
        raise InputValidationError(
            "EXTENSION_PAYLOAD_INVALID",
            f"the {what} is not the exact envelope",
            details={
                "errors": [
                    {"type": error["type"], "loc": [str(part) for part in error["loc"]]}
                    for error in invalid.errors()[:10]
                ]
            },
        ) from None


def _envelope(body: bytes, sender: VerifiedSender) -> CaptureEnvelope:
    return _signed(body, sender, CaptureEnvelope, "capture")


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


# ---------------------------------------------------------------------------- the list queue (E3)


class QueueRequest(BaseModel):
    """One queue declaration: discovered product URLs and the operator's own bounds. A missing
    bound arrives as ``null`` and refuses; nothing is defaulted (ADR-0019 §8.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    supplier_key: StrictStr
    links: list[StrictStr] = Field(max_length=MAX_PARSED_LINKS, repr=False)
    max_products: StrictInt | None
    interval_s: StrictFloat | StrictInt | None
    skip_collected: StrictBool


def _item(item: QueueItemView) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "position": item.position,
        "source_url": item.source_url,
        # The item's own state and its run's own outcome: two axes, never one (AC-31).
        "state": item.state.value,
        "collection_run_id": item.collection_run_id,
        "run_outcome": None if item.run_outcome is None else item.run_outcome.value,
        "run_detail": item.run_detail,
    }


def _queue(view: QueueView) -> dict[str, Any]:
    return {
        "queue_id": view.queue_id,
        "supplier_key": view.supplier_key,
        "state": view.state.value,
        "stop_reason": view.stop_reason,
        "max_products": view.max_products,
        "interval_s": view.interval_s,
        "skip_collected": view.skip_collected,
        "items": [_item(item) for item in view.items],
    }


def _answer(answer: QueueAnswer) -> dict[str, Any]:
    return {
        "kind": answer.kind,
        "queue_state": answer.queue_state.value,
        "wait_s": answer.wait_s,
        "item": None if answer.item is None else _item(answer.item),
        "ticket": answer.ticket,
        "expires_in_s": answer.expires_in_s,
    }


def _json(request: Request, sender: VerifiedSender, body: Any, code: int = 200) -> Response:
    response = JSONResponse(body, status_code=code)
    _allow_origin(response, request, sender)
    return response


def _empty(sender: VerifiedSender) -> None:
    if sender.body_sha256 != EMPTY_BODY_SHA256:
        raise PairingRefused("EXTENSION_BODY_DIGEST_MISMATCH", "this request carries no body")


@router.get(QUEUE_POLICY_PATH)
def queue_policy(supplier_key: str, request: Request, container: ContainerDep) -> Response:
    """The supplier's reviewed product path form and its declared queue limits."""
    sender = _authenticate(request, container.extension_pairing)
    _empty(sender)
    policy = container.extension_queues.discovery_policy(supplier_key)
    return _json(
        request,
        sender,
        {
            "supplier_key": policy.supplier_key,
            "storefront_host": policy.storefront_host,
            "product_path": policy.product_path,
            "max_discovered_links": policy.max_discovered_links,
            "max_queue_products": policy.max_queue_products,
            "min_queue_interval_s": policy.min_queue_interval_s,
        },
    )


@router.post(QUEUES_PATH, status_code=status.HTTP_201_CREATED)
async def declare_queue(request: Request, container: ContainerDep) -> Response:
    """Declare one queue, or refuse it before it exists. The answer counts what became of the
    submitted links; a refused link is never echoed."""
    sender = await run_in_threadpool(_authenticate, request, container.extension_pairing)
    body = await _bounded_body(request, MAX_QUEUE_REQUEST_BYTES)
    declared = await run_in_threadpool(_signed, body, sender, QueueRequest, "queue")
    created = await run_in_threadpool(
        container.extension_queues.create,
        QueueDeclaration(
            supplier_key=declared.supplier_key,
            links=tuple(declared.links),
            max_products=declared.max_products,
            interval_s=None if declared.interval_s is None else float(declared.interval_s),
            skip_collected=declared.skip_collected,
        ),
    )
    count = created.count
    return _json(
        request,
        sender,
        {
            "queue": _queue(created.view),
            "count": {
                "submitted": count.submitted,
                "refused": count.refused,
                "duplicates": count.duplicates,
                "skipped": count.skipped,
                "queued": count.queued,
                "beyond_cap": count.beyond_cap,
            },
        },
        status.HTTP_201_CREATED,
    )


@router.get(QUEUE_PATH)
def read_queue(queue_id: str, request: Request, container: ContainerDep) -> Response:
    sender = _authenticate(request, container.extension_pairing)
    _empty(sender)
    return _json(request, sender, _queue(container.extension_queues.read(queue_id)))


@router.post(QUEUE_NEXT_PATH)
def next_read(queue_id: str, request: Request, container: ContainerDep) -> Response:
    """Wait, one issued read, or done. The server decides; the extension's clock never does."""
    sender = _authenticate(request, container.extension_pairing)
    _empty(sender)
    return _json(request, sender, _answer(container.extension_queues.next(queue_id)))


@router.post(QUEUE_CANCEL_PATH)
def cancel_queue(queue_id: str, request: Request, container: ContainerDep) -> Response:
    sender = _authenticate(request, container.extension_pairing)
    _empty(sender)
    return _json(request, sender, _queue(container.extension_queues.cancel(queue_id)))


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


@router.options(QUEUE_POLICY_PATH)
def queue_policy_preflight(
    supplier_key: str, request: Request, container: ContainerDep
) -> Response:
    return _preflight(request, container, "GET")


@router.options(QUEUES_PATH)
def declare_queue_preflight(request: Request, container: ContainerDep) -> Response:
    return _preflight(request, container, "POST")


@router.options(QUEUE_PATH)
def read_queue_preflight(queue_id: str, request: Request, container: ContainerDep) -> Response:
    return _preflight(request, container, "GET")


@router.options(QUEUE_NEXT_PATH)
def next_read_preflight(queue_id: str, request: Request, container: ContainerDep) -> Response:
    return _preflight(request, container, "POST")


@router.options(QUEUE_CANCEL_PATH)
def cancel_queue_preflight(queue_id: str, request: Request, container: ContainerDep) -> Response:
    return _preflight(request, container, "POST")
