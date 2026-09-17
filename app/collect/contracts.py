"""Read-back contract for a stored source-truth revision (ADR-0010 §6–§9).

What the API returns is what the database holds: every field of the revision, every field's status
and canonical value, and every image reference with the source asset's own checksum, MIME, size and
original dimensions. Nothing is recomputed for display and nothing is filled in — a reference the
collection could not turn into bytes comes back ``REVIEW_REQUIRED`` with its issue and without a
checksum or a size, exactly as it was recorded.

Read-only: this contract creates nothing and changes nothing.
"""

from datetime import datetime

from pydantic import BaseModel

from app.collect.facts import FactsStatus, FieldLevel, FieldStatus, ImageIssue, ImageRole
from app.collect.models import CollectionOutcome


class SourceAssetView(BaseModel):
    """The stored bytes of one image, described by decoding those bytes."""

    sha256: str
    mime_type: str
    byte_size: int
    width: int
    height: int


class ImageReferenceView(BaseModel):
    """One ordered image reference. ``asset`` is absent while the bytes are not stored.

    The HTTP validators are part of the reference, not decoration: a later collection may reuse
    stored content only after the provider confirms it with them, so they are read back exactly as
    they were recorded.
    """

    role: ImageRole
    ordinal: int
    host: str
    provenance: str
    locator: str | None
    status: FieldStatus
    issue: ImageIssue | None
    http_etag: str | None
    http_last_modified: str | None
    asset: SourceAssetView | None


class EvidenceView(BaseModel):
    kind: str
    locator: str
    observed: str | None
    normalized: str | None
    status: FieldStatus
    digest: str


class FieldView(BaseModel):
    key: str
    level: FieldLevel
    status: FieldStatus
    value_json: str | None
    fingerprint: str
    evidence: tuple[EvidenceView, ...]


class RevisionView(BaseModel):
    """One immutable revision, as the database holds it."""

    revision_id: str
    supplier_key: str
    source_product_id: str
    sequence: int
    source_url: str
    captured_at: datetime
    recorded_at: datetime
    currency: str
    extractor_revision: str
    extractor_fingerprint: str
    source_fingerprint: str
    collection_run_id: str
    correlation_id: str
    facts_status: FactsStatus
    fingerprints_intact: bool
    fields: tuple[FieldView, ...]
    images: tuple[ImageReferenceView, ...]


class RevisionHistoryView(BaseModel):
    """Every revision of one source identity, oldest first."""

    supplier_key: str
    source_product_id: str
    revisions: tuple[RevisionView, ...]


class CollectionRequest(BaseModel):
    """Exactly one product. There is no list form, and no field that could widen a run."""

    supplier_key: str
    product_url: str


class SubmittedCollectionView(BaseModel):
    """What the operator is handed the moment a collection is accepted."""

    collection_run_id: str
    job_id: str
    correlation_id: str


class CollectionRunView(BaseModel):
    """One run's durable result.

    ``NO_REVISION`` is a finished run with nothing to append, because the source stated no stable
    identity; ``detail`` then holds the parser's reason. Only a ``RECORDED`` run names a revision.
    """

    collection_run_id: str
    job_id: str
    correlation_id: str
    supplier_key: str
    source_url: str
    outcome: CollectionOutcome
    revision_id: str | None
    facts_status: FactsStatus | None
    detail: str | None
    requested_at: datetime
    finished_at: datetime | None
