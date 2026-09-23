"""Persistence of the durable operator-reviewed category metadata (Gate 1 G1-B, ADR-0015 §3).

Three tables, created by migration 0020:

- ``registration_category_metadata``: the identity of one ``marketplace_key × taxonomy_revision ×
  category_id``. Immutable.
- ``registration_category_metadata_revisions``: the append-only revisions. Each holds one canonical
  sanitized content document that materializes the existing ``CategoryMetadata`` contract, the
  server-computed fingerprint of it, and explicit review provenance: ``reviewed``, and for a
  reviewed revision its reviewer and review time. Never updated or deleted.
- ``registration_category_metadata_current``: the one current revision of each key. It names the
  newest revision of its own key — never "the newest reviewed one" — and is never deleted.

There is no readiness, status or reason code here, no raw provider payload and no second category
model: the content document is the ``CategoryMetadata`` contract and nothing else.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

REVIEW_PROVENANCE = (
    "reviewed IN (0, 1)"
    " AND (reviewed = 1) = (reviewed_by IS NOT NULL)"
    " AND (reviewed = 1) = (reviewed_at IS NOT NULL)"
    " AND (reviewed_by IS NULL OR reviewed_by <> '')"
)


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class RegistrationCategoryMetadata(Base):
    __tablename__ = "registration_category_metadata"
    __table_args__ = (
        UniqueConstraint("marketplace_key", "taxonomy_revision", "category_id"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("taxonomy_revision <> ''", name="taxonomy_revision_present"),
        CheckConstraint("category_id <> ''", name="category_id_present"),
        CheckConstraint("created_by <> ''", name="created_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    metadata_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    taxonomy_revision: Mapped[str] = mapped_column(String(64))
    category_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationCategoryMetadataRevision(Base):
    __tablename__ = "registration_category_metadata_revisions"
    __table_args__ = (
        UniqueConstraint("metadata_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint(REVIEW_PROVENANCE, name="review_provenance"),
        CheckConstraint("recorded_by <> ''", name="recorded_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    # The server-created identity: the CategoryMetadata ``metadata_revision``.
    metadata_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    metadata_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_category_metadata.metadata_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    # SHA-256 of the canonical sanitized content document, never of request bytes.
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    reviewed: Mapped[int] = mapped_column(Integer)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recorded_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationCategoryMetadataCurrent(Base):
    __tablename__ = "registration_category_metadata_current"
    __table_args__ = (
        CheckConstraint("moved_by <> ''", name="moved_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    metadata_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_category_metadata.metadata_id"), primary_key=True
    )
    metadata_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_category_metadata_revisions.metadata_revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)
