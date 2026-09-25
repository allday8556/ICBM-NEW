"""ORM models of the Adaptive shadow owner (migration 0025)."""

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

SWITCH_ACTIONS = ("ENABLE", "DISABLE")
RUN_VERDICTS = (
    "MATCH",
    "MISMATCH",
    "IDENTITY_MISMATCH",
    "TEMPLATE_UNMATCHED",
    "TEMPLATE_AMBIGUOUS",
    "IMAGE_UNMATCHABLE",
    "SHADOW_FAILED",
)
SEVERITIES = ("CONFIDENT_DISAGREEMENT", "ADAPTIVE_OVERCONFIDENT", "ADAPTIVE_CONSERVATIVE")
EVENT_KINDS = ("OUTCOME_RECORDED", "RESOLUTION_RECORDED", "RAW_PRUNED_UNRESOLVED")
COUNTS_AS = ("SUCCESS", "FAILURE", "INCOMPLETE")
CAUSES = (
    "UNRESOLVED_MISMATCH",
    "PRUNED_BEFORE_RESOLUTION",
    "IMAGE_UNMATCHABLE",
    "SHADOW_MISSING",
    "SHADOW_MISSING_AFTER_RECOVERY",
)
RESOLUTIONS = ("CURRENT_CORRECT", "ADAPTIVE_CORRECT", "BOTH_WRONG", "SOURCE_AMBIGUOUS")
WINDOW_EVENTS = ("DECLARED", "ENDED", "CLOSED", "SUPERSEDED")
MIN_WINDOW_SIZE = 3


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


BUNDLE_KEY_SHAPE = (
    "json_valid({c}) AND json_type({c}) = 'array' AND json_array_length({c}) = 4"
    " AND json_extract({c}, '$[0]') = 'ADAPTIVE'"
)


def bundle_key_check(column: str) -> str:
    return BUNDLE_KEY_SHAPE.format(c=column)


class ShadowSwitchEntry(Base):
    """One append-only change of a supplier's shadow switch (ADR-0017 §10.2 S7)."""

    __tablename__ = "adaptive_shadow_switch_entries"
    __table_args__ = (
        UniqueConstraint("supplier_key", "seq"),
        CheckConstraint(_in("action", SWITCH_ACTIONS), name="action_valid"),
        CheckConstraint("seq >= 1", name="seq_valid"),
        CheckConstraint(
            "(action = 'ENABLE' AND epr_digest IS NOT NULL AND bundle_key IS NOT NULL"
            " AND freshness_json IS NOT NULL)"
            " OR (action = 'DISABLE' AND epr_digest IS NULL AND bundle_key IS NULL"
            " AND freshness_json IS NULL)",
            name="action_shape",
        ),
        CheckConstraint(
            f"bundle_key IS NULL OR ({bundle_key_check('bundle_key')})", name="bundle_key_shape"
        ),
        CheckConstraint(
            "freshness_json IS NULL OR (json_valid(freshness_json)"
            " AND json_type(freshness_json) = 'array' AND json_array_length(freshness_json) = 7)",
            name="freshness_shape",
        ),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(8))
    epr_digest: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("adaptive_profile_revisions.digest"), nullable=True
    )
    bundle_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    freshness_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ShadowRecord(Base):
    """The raw shadow comparison of one run: non-canonical, at most one per run (§10.5)."""

    __tablename__ = "adaptive_shadow_records"
    __table_args__ = (
        Index("ix_adaptive_shadow_records_supplier_key_recorded_at", "supplier_key", "recorded_at"),
        CheckConstraint(_in("run_verdict", RUN_VERDICTS), name="verdict_valid"),
        CheckConstraint(
            f"severity IS NULL OR {_in('severity', SEVERITIES)}", name="severity_valid"
        ),
        CheckConstraint(bundle_key_check("bundle_key"), name="bundle_key_shape"),
        CheckConstraint(
            "json_valid(comparison_json) AND json_type(comparison_json) = 'object'",
            name="comparison_object",
        ),
        CheckConstraint(_hex64("record_digest"), name="digest_hex"),
        CheckConstraint("process_run_id <> ''", name="process_present"),
    )

    collection_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_runs.collection_run_id"), primary_key=True
    )
    supplier_key: Mapped[str] = mapped_column(String(40))
    revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id"), nullable=True
    )
    bundle_key: Mapped[str] = mapped_column(Text)
    run_verdict: Mapped[str] = mapped_column(String(24))
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comparison_json: Mapped[str] = mapped_column(Text)
    record_digest: Mapped[str] = mapped_column(String(64))
    process_run_id: Mapped[str] = mapped_column(String(36))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EvidenceWindow(Base):
    """One declared Phase C evidence window of one supplier and one exact bundle (§11.1)."""

    __tablename__ = "adaptive_evidence_windows"
    __table_args__ = (
        Index("ix_adaptive_evidence_windows_supplier_key", "supplier_key"),
        CheckConstraint(bundle_key_check("bundle_key"), name="bundle_key_shape"),
        CheckConstraint(f"min_size >= {MIN_WINDOW_SIZE}", name="min_size_valid"),
        CheckConstraint("declared_by <> ''", name="author_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    window_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    bundle_key: Mapped[str] = mapped_column(Text)
    min_size: Mapped[int] = mapped_column(Integer)
    declared_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    declared_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EvidenceWindowEvent(Base):
    """``DECLARED``, ``ENDED``, ``CLOSED`` or ``SUPERSEDED``: append-only (§11.1, §11.2)."""

    __tablename__ = "adaptive_evidence_window_events"
    __table_args__ = (
        CheckConstraint(_in("kind", WINDOW_EVENTS), name="kind_valid"),
        CheckConstraint("(seq = 1) = (kind = 'DECLARED')", name="declared_first"),
        CheckConstraint(
            "detail_json IS NULL OR (json_valid(detail_json)"
            " AND json_type(detail_json) = 'object')",
            name="detail_object",
        ),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("reason <> ''", name="reason_present"),
    )

    window_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("adaptive_evidence_windows.window_id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LedgerEvent(Base):
    """One immutable evidence-ledger event of one eligible run (§10.5)."""

    __tablename__ = "adaptive_shadow_ledger_events"
    __table_args__ = (
        UniqueConstraint("collection_run_id", "seq"),
        Index("ix_adaptive_shadow_ledger_events_window_id", "window_id"),
        CheckConstraint(_in("kind", EVENT_KINDS), name="kind_valid"),
        CheckConstraint(_in("count_as", COUNTS_AS), name="count_as_valid"),
        CheckConstraint(f"cause IS NULL OR {_in('cause', CAUSES)}", name="cause_valid"),
        CheckConstraint(
            "(count_as = 'INCOMPLETE') = (cause IS NOT NULL)", name="cause_iff_incomplete"
        ),
        CheckConstraint(
            f"run_verdict IS NULL OR {_in('run_verdict', RUN_VERDICTS)}", name="verdict_valid"
        ),
        CheckConstraint(
            f"resolution IS NULL OR {_in('resolution', RESOLUTIONS)}", name="resolution_valid"
        ),
        CheckConstraint(
            "(kind = 'OUTCOME_RECORDED' AND seq = 1 AND resolution IS NULL)"
            " OR (kind = 'RESOLUTION_RECORDED' AND seq > 1 AND resolution IS NOT NULL"
            " AND evidence_ref IS NOT NULL AND evidence_ref <> ''"
            " AND adaptive_failed_closed IS NOT NULL)"
            " OR (kind = 'RAW_PRUNED_UNRESOLVED' AND seq > 1 AND resolution IS NULL"
            " AND count_as = 'INCOMPLETE' AND cause = 'PRUNED_BEFORE_RESOLUTION')",
            name="kind_shape",
        ),
        CheckConstraint("process_run_id <> ''", name="process_present"),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collection_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_runs.collection_run_id")
    )
    seq: Mapped[int] = mapped_column(Integer)
    window_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("adaptive_evidence_windows.window_id")
    )
    kind: Mapped[str] = mapped_column(String(24))
    count_as: Mapped[str] = mapped_column(String(12))
    cause: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_verdict: Mapped[str | None] = mapped_column(String(24), nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    adaptive_failed_closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    process_run_id: Mapped[str] = mapped_column(String(36))
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
