"""The durable path from a fetched source image to an immutable reference (ADR-0010 §9).

A collection run hands over exactly what came back from the provider. What happens next is not the
run's business and not a harness's: the bytes are preserved as they arrived, their SHA-256 is
computed here from those bytes, their original width, height and MIME come from decoding them, and
the role, the source order and the provenance are persisted with the reference that names them.

Nothing is invented. A reference the run could not turn into bytes — refused, oversize, the budget
gone, never fetched — is ``REVIEW_REQUIRED`` and says why; bytes the decoder cannot read are
``UNSUPPORTED_FORMAT`` and are not stored at all. A checksum or a dimension is never derived from a
URL, a header a server declared, or anything other than the bytes themselves.

This module owns that path. A reconnaissance harness calls it; it never reimplements it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.collect.assets import SourceAssetStore, UnsupportedSourceImageError
from app.collect.facts import FieldStatus, ImageIssue, ImageReference, ImageRole


@dataclass(frozen=True)
class FetchedImage:
    """One image a collection run actually received, exactly as it arrived.

    ``content`` is the response body unchanged: not resized, not re-encoded, not re-compressed.
    """

    role: ImageRole
    ordinal: int
    host: str
    provenance: str
    content: bytes = field(repr=False)
    locator: str | None = None
    http_etag: str | None = None
    http_last_modified: str | None = None


@dataclass(frozen=True)
class UnfetchedImage:
    """One image reference a collection run could not turn into bytes, and why."""

    role: ImageRole
    ordinal: int
    host: str
    provenance: str
    issue: ImageIssue
    locator: str | None = None
    http_etag: str | None = None
    http_last_modified: str | None = None


SourceImage = FetchedImage | UnfetchedImage


class SourceAssetRecorder:
    """Persists the bytes of a collection's images and returns the references to record."""

    def __init__(self, assets: SourceAssetStore) -> None:
        self._assets = assets

    def record(self, images: Sequence[SourceImage]) -> tuple[ImageReference, ...]:
        """Store every fetched image's bytes once and describe each reference.

        The order given is the order kept: a reference's ordinal is the position the page put it
        in, and it is never renumbered to hide a reference that could not be stored.
        """
        return tuple(self._reference(image) for image in images)

    def _reference(self, image: SourceImage) -> ImageReference:
        if isinstance(image, UnfetchedImage):
            return _review(image, image.issue)
        try:
            stored = self._assets.put(image.content)
        except UnsupportedSourceImageError:
            # The bytes are kept out of the store entirely: an asset whose size and type cannot be
            # read from itself would be evidence of nothing.
            return _review(image, ImageIssue.UNSUPPORTED_FORMAT)
        return ImageReference(
            role=image.role,
            ordinal=image.ordinal,
            host=image.host,
            provenance=image.provenance,
            locator=image.locator,
            sha256=stored.sha256,
            status=FieldStatus.CONFIRMED,
            issue=None,
            etag=image.http_etag,
            last_modified=image.http_last_modified,
        )


def _review(image: SourceImage, issue: ImageIssue) -> ImageReference:
    return ImageReference(
        role=image.role,
        ordinal=image.ordinal,
        host=image.host,
        provenance=image.provenance,
        locator=image.locator,
        sha256=None,
        status=FieldStatus.REVIEW_REQUIRED,
        issue=issue,
        etag=image.http_etag,
        last_modified=image.http_last_modified,
    )
