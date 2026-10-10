"""Persistence of the Detail Guidance owner (ADR-0033 §2), migration 0058.

- ``detail_guidances``: the immutable identity of one notice, its placement and kind. A placement
  has at most one ``STANDING`` identity, whose newest revision is its standing notice.
- ``guidance_image_artifacts``: one rendered PNG by its SHA-256; the bytes live in the guidance
  image store, never here.
- ``detail_guidance_revisions``: append-only revisions numbered from one by exactly one, each with
  its template, plain-text content, ``enabled``, the half-open UTC period of a ``PERIOD`` notice
  and the image it was saved with.

The database refuses an update or a delete of any row, a second standing identity of a placement,
a numbering gap, a kind that is not the identity's and a template that is not the image's.
``store.DetailGuidanceStore`` is the only writer.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

_TEMPLATES = "template IN ('CLEAN', 'MODERN', 'WARM', 'DOMESTIC', 'OVERSEAS')"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class DetailGuidance(Base):
    __tablename__ = "detail_guidances"
    __table_args__ = (
        Index(
            "ux_detail_guidances_one_standing",
            "placement",
            unique=True,
            sqlite_where=sql("kind = 'STANDING'"),
        ),
        CheckConstraint("guidance_id <> ''", name="id_present"),
        CheckConstraint("placement IN ('TOP', 'BOTTOM')", name="placement_known"),
        CheckConstraint("kind IN ('STANDING', 'PERIOD')", name="kind_known"),
    )

    guidance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    placement: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class GuidanceImageArtifact(Base):
    __tablename__ = "guidance_image_artifacts"
    __table_args__ = (
        CheckConstraint(_hex64("sha256"), name="sha256_hex"),
        CheckConstraint("width > 0 AND height > 0 AND byte_size > 0", name="sizes_positive"),
        CheckConstraint("renderer_version <> ''", name="renderer_version_present"),
        CheckConstraint(_TEMPLATES, name="template_known"),
        CheckConstraint(_hex64("font_sha256"), name="font_sha256_hex"),
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    renderer_version: Mapped[str] = mapped_column(String(64))
    template: Mapped[str] = mapped_column(String(16))
    font_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DetailGuidanceRevision(Base):
    __tablename__ = "detail_guidance_revisions"
    __table_args__ = (
        UniqueConstraint("guidance_id", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("kind IN ('STANDING', 'PERIOD')", name="kind_known"),
        CheckConstraint(_TEMPLATES, name="template_known"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint("enabled IN (0, 1)", name="enabled_flag"),
        CheckConstraint(
            "(kind = 'PERIOD' AND starts_at IS NOT NULL AND ends_at IS NOT NULL"
            " AND starts_at < ends_at)"
            " OR (kind = 'STANDING' AND starts_at IS NULL AND ends_at IS NULL)",
            name="period_matches_kind",
        ),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    guidance_id: Mapped[str] = mapped_column(String(36), ForeignKey("detail_guidances.guidance_id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))
    template: Mapped[str] = mapped_column(String(16))
    content_json: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    image_sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("guidance_image_artifacts.sha256")
    )
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)
