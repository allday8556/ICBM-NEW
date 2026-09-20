"""Promotion of a provider image-upload outcome to a PreparedAsset (ADR-0014 §3 B2; kickoff §4).

The packet proves that ``POST /v1/product-images/upload`` takes ``multipart/form-data`` and that
the URL it returns is used directly as the product image URL, but not the multipart part name the
API expects, so the request cannot be composed without inventing it: the endpoint stays
NOT_ADOPTED and :func:`upload_request` refuses. What *is* provable — when an upload outcome may
become a known provider asset identity — is fixed here, so PR-E inherits the rule rather than
inventing it after the endpoint is adopted:

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

from app.core.errors import AppError
from app.products.image_model import ImageAssetKind
from app.register.preparation import PreparedAsset
from app.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.registry import ADOPTION_GAPS, EndpointId

UPLOAD_OUTCOME_VERSION: Final = "smartstore-image-upload-outcome/v1"
_URL_FIELD: Final = "url"


class ImageUploadNotAdoptedError(AppError):
    """The upload request cannot be composed from the proven contract."""

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_IMAGE_UPLOAD_NOT_ADOPTED",
            "SmartStore image upload is not adopted: "
            + ADOPTION_GAPS[EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD],
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_IMAGE_UPLOAD.value},
        )


@dataclass(frozen=True)
class UploadOutcome:
    """What one upload proved. ``asset`` exists only when the outcome is unambiguous."""

    outcome_version: str
    asset: PreparedAsset | None
    ambiguous_reason: str | None

    @property
    def ambiguous(self) -> bool:
        return self.asset is None


def upload_request(*_: object, **__: object) -> None:
    """Compose an image-upload request. Always refuses while the part name is unproven."""
    raise ImageUploadNotAdoptedError()


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
