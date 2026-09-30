"""One-artifact SmartStore image upload and promotion to ``PreparedAsset``.

The official 2.89.0 contract and Issue #89 amendments 5765557497/5765663972 adopt only
``POST /v1/product-images/upload`` with one immutable artifact in one ``imageFiles`` part. The
adapter calls once and never retries. A transport/response ambiguity is represented only as
``UPLOAD_UNKNOWN`` here; it never enters ``RegistrationIntent.UNKNOWN``, and the durable owner that
records and terminalizes it is the ASSET upload-attempt owner of ADR-0018 §3.4
(``app.capabilities.live_safety.assets``,
Gate 3 area 1) — never this adapter, which owns no state.

* one reference per exactly one M4 artifact, bound to the READY candidate fingerprint it was
  uploaded under, and to the asset profile the target policy names;
* the reference must pass the PR-C sanitizer, so a signed, tokenized or otherwise URI-unsafe
  value never becomes durable;
* anything else is **ambiguous**: an ambiguous outcome yields no asset identity, never a guess,
  and the artifact stays unprepared (ADR-0014 R2's spirit for uploads).

:class:`SmartStoreAssetSender` is the production side of the ASSET upload path (ADR-0018 §3.4,
§10): the ``AssetUploadSender`` the durable attempt owner hands one artifact to. It uses only the
adopted upload contract, through the registry-gated caller, once per send. Without a committed
session it is unavailable and proves that nothing left the process; production wires none, so at
this main it sends nothing.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.capabilities.live_safety.assets import TransmissionPrecluded, UploadSendResult
from app.capabilities.live_safety.model import UploadAttemptState
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.products.image_model import ImageAssetKind
from app.stages.register.preparation import PreparedAsset
from app.stages.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.caller import (
    ImageUploadRequest,
    ImageUploadResponse,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    EndpointId,
    wire_identity,
)

UPLOAD_OUTCOME_VERSION: Final = "smartstore-image-upload-outcome/v1"
_URL_FIELD: Final = "url"


@dataclass(frozen=True)
class UploadOutcome:
    """What one upload proved. ``asset`` exists only when the outcome is unambiguous."""

    outcome_version: str
    asset: PreparedAsset | None
    ambiguous_reason: str | None

    @property
    def ambiguous(self) -> bool:
        return self.asset is None


def upload_request(
    *,
    access_token: str,
    credential_generation: int,
    session_generation: int,
    filename: str,
    media_type: str,
    content: bytes,
) -> ImageUploadRequest:
    """Build the typed request for exactly one artifact; validation occurs before transport."""
    return ImageUploadRequest(
        access_token=access_token,
        credential_generation=credential_generation,
        session_generation=session_generation,
        filename=filename,
        media_type=media_type,
        content=content,
    )


class ImageUploadAdapter:
    """Single-call adapter. It owns no cache, ledger, retry loop or durable state."""

    def __init__(self, caller: SmartStoreEndpointCaller) -> None:
        self._caller = caller

    def upload(
        self,
        request: ImageUploadRequest,
        *,
        asset_kind: ImageAssetKind,
        sha256: str,
        derivation_id: str | None,
        asset_profile: str,
        candidate_fingerprint: str,
    ) -> UploadOutcome:
        try:
            response = self._caller.call(EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD, request)
        except SmartStoreCallError as exc:
            # Any provider response failure or possibly transmitted request is upload ambiguity.
            # Pre-transmission failures remain ordinary local/transport failures, not UNKNOWN.
            if exc.remote_outcome.value == "UNKNOWN":
                return UploadOutcome(UPLOAD_OUTCOME_VERSION, None, "UPLOAD_UNKNOWN")
            raise
        assert isinstance(response, ImageUploadResponse)
        return promote(
            response.retained,
            asset_kind=asset_kind,
            sha256=sha256,
            derivation_id=derivation_id,
            asset_profile=asset_profile,
            candidate_fingerprint=candidate_fingerprint,
        )


def _references(retained: Mapping[str, Any]) -> list[str]:
    """Every ``url`` in a retained upload response, in document order."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            reference = value.get(_URL_FIELD)
            if isinstance(reference, str):
                found.append(reference)
            for child in value.values():
                walk(child)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for item in value:
                walk(item)

    walk(retained)
    return found


def single_reference(retained: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The one provider asset reference a retained upload response proves, or why it proves none.

    Exactly one reference for exactly one artifact: none, several — two equal ones included,
    because the upload then proved two — or one that fails sanitation is ambiguous.
    """
    references = _references(retained)
    if not references:
        return None, "UPLOAD_NO_REFERENCE_RETURNED"
    if len(references) != 1:
        return None, "UPLOAD_REFERENCE_NOT_UNIQUE"
    reference = references[0]
    if not safe_provider_reference(reference):
        return None, "UPLOAD_REFERENCE_UNSAFE"
    return reference, None


def promote(
    retained: Mapping[str, Any],
    *,
    asset_kind: ImageAssetKind,
    sha256: str,
    derivation_id: str | None,
    asset_profile: str,
    candidate_fingerprint: str,
) -> UploadOutcome:
    """The provider asset identity of exactly one uploaded artifact, or an ambiguous outcome.

    An upload that returns no reference, or more than one for a single artifact — **two equal
    references included** — or a reference that fails sanitation, proves nothing about which
    provider asset now exists.
    """
    reference, ambiguous = single_reference(retained)
    if reference is None:
        return UploadOutcome(UPLOAD_OUTCOME_VERSION, None, ambiguous)
    return UploadOutcome(
        UPLOAD_OUTCOME_VERSION,
        PreparedAsset(
            asset_kind=asset_kind,
            sha256=sha256,
            derivation_id=derivation_id,
            asset_profile=asset_profile,
            candidate_fingerprint=candidate_fingerprint,
            provider_asset_ref=reference,
        ),
        None,
    )


# ---------------------------------------------------------------- the production ASSET sender

BearerSource = Callable[[], Any]
SESSION_UNAVAILABLE: Final = "SMARTSTORE_SESSION_UNAVAILABLE"


class SmartStoreAssetSender:
    """The adopted image upload as the ASSET upload path's sender (ADR-0018 §3.4, §10).

    It owns no ledger, cache, retry or durable state: the ASSET upload-attempt owner records the
    attempt before calling :meth:`send` and terminalizes it from the result, and the send-time
    safety stack admits or refuses before that. One ``send`` is at most one provider call.

    - **No committed session, nothing sent.** ``available`` is false and ``send`` raises
      :class:`TransmissionPrecluded` before any transport — the honest proof that no byte left.
    - **Applied** only when the adopted success predicate passed and the retained response proves
      exactly one sanitized reference.
    - **Not applied** only on the caller's transmission-precluded whitelist (a local preflight
      rejection, an egress refusal, or a new connection that failed before any request byte).
    - **Everything else is ``UPLOAD_UNKNOWN``** — a provider error response, a timeout, a dropped
      or reused connection, an ambiguous success — and is never retried here or anywhere.
    """

    marketplace_key = "smartstore"

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: BearerSource) -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether an upload could be handed off at all: the endpoint is adopted **and** a
        committed session exists. ``False`` keeps the stack's sender layer refusing."""
        return self.endpoint_adopted() and self._bearer() is not None

    def endpoint_adopted(self) -> bool:
        return EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD in ADOPTED

    def wire(self) -> tuple[str, str, str]:
        return wire_identity(EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD)

    def contract_label(self) -> str:
        return SMARTSTORE_ENDPOINT_MAPPING_REVISION

    def send(self, *, content: bytes, file_name: str, media_type: str) -> UploadSendResult:
        bearer = self._bearer()
        if bearer is None:
            raise TransmissionPrecluded(SESSION_UNAVAILABLE)
        try:
            response = self._caller.call(
                EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD,
                upload_request(
                    access_token=bearer.access_token,
                    credential_generation=bearer.credential_generation,
                    session_generation=bearer.session_generation,
                    filename=file_name,
                    media_type=media_type,
                    content=content,
                ),
            )
        except SmartStoreCallError as exc:
            if exc.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN:
                # The caller's own whitelist: this request provably never reached the provider.
                raise TransmissionPrecluded(exc.code) from exc
            return UploadSendResult(
                UploadAttemptState.UPLOAD_UNKNOWN,
                outcome_reason=exc.code,
                evidence=_evidence(None),
            )
        assert isinstance(response, ImageUploadResponse)
        reference, ambiguous = single_reference(response.retained)
        if reference is None:
            return UploadSendResult(
                UploadAttemptState.UPLOAD_UNKNOWN,
                outcome_reason=ambiguous,
                evidence=_evidence(response.http_status),
            )
        return UploadSendResult(
            UploadAttemptState.APPLIED_PROVEN,
            provider_asset_ref=reference,
            evidence=_evidence(response.http_status),
        )


def _evidence(http_status: int | None) -> dict[str, Any]:
    """Sanitized facts only: versions and the status, never a header, a body or a token."""
    return {
        "outcome_version": UPLOAD_OUTCOME_VERSION,
        "contract_label": SMARTSTORE_ENDPOINT_MAPPING_REVISION,
        "http_status": http_status,
    }
