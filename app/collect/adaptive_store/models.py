"""ORM models of the Adaptive profile and validation owner (migration 0024)."""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

KINDS = ("EXTRACTION_PROFILE", "PAGE_TEMPLATE")
ORIGINS = ("OPERATOR", "AI_PROPOSAL", "IMPORT")
STATES = ("DRAFT", "SHADOW", "RETIRED")
VERDICTS = ("PASS", "FAIL", "INCOMPLETE")
NOTE_MAX_CHARS = 500
TRANSITION_SHAPE = (
    "(seq = 1 AND from_state IS NULL AND to_state = 'DRAFT')"
    " OR (seq > 1 AND from_state = 'DRAFT' AND to_state IN ('SHADOW', 'RETIRED'))"
    " OR (seq > 1 AND from_state = 'SHADOW' AND to_state IN ('DRAFT', 'RETIRED'))"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class AdaptiveProfileRevision(Base):
    __tablename__ = "adaptive_profile_revisions"
    __table_args__ = (
        Index("ix_adaptive_profile_revisions_supplier_key_kind", "supplier_key", "kind"),
        CheckConstraint(_hex64("digest"), name="digest_hex"),
        CheckConstraint(_in("kind", KINDS), name="kind_valid"),
        CheckConstraint("supplier_key <> ''", name="supplier_present"),
        CheckConstraint("schema_version <> ''", name="schema_present"),
        CheckConstraint(
            "json_valid(document) AND json_type(document) = 'object'"
            " AND json_extract(document, '$.kind') = kind"
            " AND json_extract(document, '$.supplier_key') = supplier_key"
            " AND json_extract(document, '$.schema_version') = schema_version",
            name="document_agrees",
        ),
        CheckConstraint(
            f"parent_digest IS NULL OR ({_hex64('parent_digest')} AND parent_digest <> digest)",
            name="parent_valid",
        ),
        CheckConstraint(_in("origin", ORIGINS), name="origin_valid"),
        CheckConstraint(
            f"change_note IS NULL OR (length(change_note) BETWEEN 1 AND {NOTE_MAX_CHARS})",
            name="note_bounded",
        ),
        CheckConstraint("created_by <> ''", name="author_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    supplier_key: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(32))
    document: Mapped[str] = mapped_column(Text)
    parent_digest: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest"), nullable=True
    )
    origin: Mapped[str] = mapped_column(String(16))
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AdaptiveProfilePin(Base):
    __tablename__ = "adaptive_profile_pins"
    __table_args__ = (
        UniqueConstraint("epr_digest", "ptr_digest"),
        CheckConstraint("position >= 0", name="position_valid"),
    )

    epr_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    ptr_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest")
    )


class AdaptiveProfileTransition(Base):
    __tablename__ = "adaptive_profile_transitions"
    __table_args__ = (
        UniqueConstraint("epr_digest", "seq"),
        CheckConstraint(_in("to_state", STATES), name="to_state_valid"),
        CheckConstraint(TRANSITION_SHAPE, name="transition_shape"),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    transition_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epr_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest")
    )
    seq: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AdaptiveValidationSample(Base):
    __tablename__ = "adaptive_validation_samples"
    __table_args__ = (
        CheckConstraint(_hex64("sample_digest"), name="digest_hex"),
        CheckConstraint("supplier_key <> ''", name="supplier_present"),
        CheckConstraint("capture_revision <> ''", name="capture_present"),
        CheckConstraint(
            "json_valid(structure_json) AND json_valid(expected_json)"
            " AND json_valid(provenance_json)"
            " AND json_extract(provenance_json, '$.capture_revision') = capture_revision",
            name="documents_agree",
        ),
        CheckConstraint("stored_by <> ''", name="author_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    sample_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    capture_revision: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(Boolean)
    structure_json: Mapped[str] = mapped_column(Text)
    expected_json: Mapped[str] = mapped_column(Text)
    provenance_json: Mapped[str] = mapped_column(Text)
    stored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    stored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AdaptiveValidationRun(Base):
    __tablename__ = "adaptive_validation_runs"
    __table_args__ = (
        UniqueConstraint("run_digest"),
        Index("ix_adaptive_validation_runs_epr_digest", "epr_digest"),
        CheckConstraint(_in("verdict", VERDICTS), name="verdict_valid"),
        CheckConstraint("profile_schema_version <> ''", name="schema_present"),
        CheckConstraint("extractor_revision <> ''", name="extractor_present"),
        CheckConstraint(_hex64("extractor_fingerprint"), name="extractor_fingerprint_hex"),
        CheckConstraint(
            f"hook_fingerprint = '' OR ({_hex64('hook_fingerprint')})", name="hook_fingerprint"
        ),
        CheckConstraint(_hex64("sample_set_digest"), name="sample_set_hex"),
        CheckConstraint("capture_revision <> ''", name="capture_present"),
        CheckConstraint(
            "json_valid(checks_json) AND json_type(checks_json) = 'array'", name="checks_array"
        ),
        CheckConstraint(_hex64("run_digest"), name="run_digest_hex"),
        CheckConstraint("recorded_by <> ''", name="author_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epr_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest")
    )
    verdict: Mapped[str] = mapped_column(String(16))
    profile_schema_version: Mapped[str] = mapped_column(String(32))
    extractor_revision: Mapped[str] = mapped_column(String(64))
    extractor_fingerprint: Mapped[str] = mapped_column(String(64))
    hook_fingerprint: Mapped[str] = mapped_column(String(64))
    sample_set_digest: Mapped[str] = mapped_column(String(64))
    capture_revision: Mapped[str] = mapped_column(String(64))
    checks_json: Mapped[str] = mapped_column(Text)
    run_digest: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AdaptiveValidationRunSample(Base):
    __tablename__ = "adaptive_validation_run_samples"
    __table_args__ = (
        Index("ix_adaptive_validation_run_samples_sample_digest", "sample_digest"),
        CheckConstraint("position >= 0", name="position_valid"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("adaptive_validation_runs.run_id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_validation_samples.sample_digest")
    )
