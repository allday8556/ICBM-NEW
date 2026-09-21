"""One-artifact SmartStore image upload and promotion to ``PreparedAsset``.

The official 2.89.0 contract and Issue #89 amendments 5765557497/5765663972 adopt only
``POST /v1/product-images/upload`` with one immutable artifact in one ``imageFiles`` part. The
adapter calls once and never retries. A transport/response ambiguity is represented only as
``UPLOAD_UNKNOWN`` here; it never enters ``RegistrationIntent.UNKNOWN`` and has no durable owner.

* one reference per exactly one M4 artifact, bound to the READY candidate fingerprint it was
  uploaded under, and to the asset profile the target policy names;
* the reference must pass the PR-C sanitizer, so a signed, tokenized or otherwise URI-unsafe
  value never becomes durable;
* anything else is **ambiguous**: an ambiguous outcome yields no asset identity, never a guess,
  and the artifact stays unprepared (ADR-0014 R2's spirit for uploads).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.products.image_model import ImageAssetKind
from app.register.preparation import PreparedAsset
from app.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.caller import (
    ImageUploadRequest,
    ImageUploadResponse,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId

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
    references = _references(retained)
    if not references:
        return UploadOutcome(UPLOAD_OUTCOME_VERSION, None, "UPLOAD_NO_REFERENCE_RETURNED")
    # Exactly one reference for exactly one artifact: two references are ambiguous even when
    # they happen to carry the same string, because the upload proved two, not one.
    if len(references) != 1:
        return UploadOutcome(UPLOAD_OUTCOME_VERSION, None, "UPLOAD_REFERENCE_NOT_UNIQUE")
    reference = references[0]
    if not safe_provider_reference(reference):
        return UploadOutcome(UPLOAD_OUTCOME_VERSION, None, "UPLOAD_REFERENCE_UNSAFE")
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
