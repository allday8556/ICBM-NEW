"""Persistence of the PromptTemplate and PlatformPolicy stores (ADR-0026 §3), migration 0053.

Two separate families of three tables (Issue #30: no shared storage or version lifecycle):

- an immutable identity per entry (a PromptTemplate also names its layer: GLOBAL, ROLE or TASK);
- append-only revisions: the content is one JSON object of text fields, its fingerprint is the
  SHA-256 of its canonical JSON, and the number opens at one and moves by exactly one. Revision 1 is
  the v29 seed (``origin = SEED``); every later one is an operator's save (``OPERATOR``) or reset to
  the seed (``RESET``);
- one current pointer per entry, which only moves forward to its own newest revision.

The database refuses an update or a delete of an identity or a revision, a gap in the numbering, a
content that is not exactly its layer's text fields, and a pointer that names anything but its own
newest revision.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _revision_checks() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(_hex64("content_fingerprint"), name="content_fingerprint_hex"),
        CheckConstraint("origin IN ('SEED', 'OPERATOR', 'RESET')", name="origin_known"),
        CheckConstraint(
            "(origin = 'SEED') = (revision_no = 1)"
            " AND (origin = 'SEED') = (seed_version IS NOT NULL)",
            name="seed_is_first",
        ),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )


def _current_checks() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint("moved_by <> ''", name="moved_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )


class AIPromptTemplate(Base):
    __tablename__ = "ai_prompt_templates"
    __table_args__ = (
        CheckConstraint("template_key <> ''", name="key_present"),
        CheckConstraint("layer IN ('GLOBAL', 'ROLE', 'TASK')", name="layer_known"),
    )

    template_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    layer: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIPromptTemplateRevision(Base):
    __tablename__ = "ai_prompt_template_revisions"
    __table_args__ = (UniqueConstraint("template_key", "revision_no"), *_revision_checks())

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_prompt_templates.template_key")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16))
    seed_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIPromptTemplateCurrent(Base):
    __tablename__ = "ai_prompt_template_current"
    __table_args__ = _current_checks()

    template_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_prompt_templates.template_key"), primary_key=True
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_prompt_template_revisions.revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIPlatformPolicy(Base):
    __tablename__ = "ai_platform_policies"
    __table_args__ = (CheckConstraint("policy_key <> ''", name="key_present"),)

    policy_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIPlatformPolicyRevision(Base):
    __tablename__ = "ai_platform_policy_revisions"
    __table_args__ = (UniqueConstraint("policy_key", "revision_no"), *_revision_checks())

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_platform_policies.policy_key")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16))
    seed_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIPlatformPolicyCurrent(Base):
    __tablename__ = "ai_platform_policy_current"
    __table_args__ = _current_checks()

    policy_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_platform_policies.policy_key"), primary_key=True
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_platform_policy_revisions.revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)
