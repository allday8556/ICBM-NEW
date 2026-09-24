"""Persistence of the durable ReviewItem owner (Gate 2 G2-A, ADR-0016).

Two tables, created by migration 0021:

- ``review_items``: one row per **review key** (§3). Its identity references — kind, producer,
  canonical scope, subject, owner reason code, source identity and both keys — never change after
  the row is written. Only its lifecycle moves: ``state``, ``generation`` and ``changed_at``. At
  most one ``OPEN`` row exists per **condition key**, which the database enforces.
- ``review_item_events``: the append-only history of every transition and every human
  resolution (§4, §5, §9), numbered per item with no gap. A human resolution carries a bounded
  disposition, an optional sanitized note and an optional owner-identifier evidence reference;
  there is at most one per item generation.

A ReviewItem stores references only. There is no owner value, no readiness, no verdict and no
provider or page content here (§2, §9).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.review.model import (
    NOTE_MAX_CHARS,
    ReviewBasis,
    ReviewDisposition,
    ReviewEvent,
    ReviewKind,
    ReviewState,
)


def _in(column: str, values: type[ReviewKind] | type[ReviewState] | type[ReviewEvent]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


HUMAN_BASES = (ReviewBasis.HUMAN_RESOLUTION.value, ReviewBasis.CONDITION_PERSISTS.value)

# Each event has exactly one shape (§4). A human disposition appears on, and only on, a human
# resolution: a RESOLVED by HUMAN_RESOLUTION, or a RESOLUTION_RECORDED that did not close the item.
EVENT_SHAPE = (
    "(event = 'OPENED' AND from_state IS NULL AND to_state = 'OPEN'"
    " AND basis = 'OWNER_CONDITION_DERIVED' AND successor_item_id IS NULL)"
    " OR (event = 'REOPENED' AND from_state IN ('RESOLVED', 'SUPERSEDED') AND to_state = 'OPEN'"
    " AND basis = 'OWNER_CONDITION_DERIVED' AND successor_item_id IS NULL)"
    " OR (event = 'SUPERSEDED' AND from_state = 'OPEN' AND to_state = 'SUPERSEDED'"
    " AND basis = 'OWNER_SOURCE_MOVED' AND successor_item_id IS NOT NULL)"
    " OR (event = 'RESOLVED' AND from_state = 'OPEN' AND to_state = 'RESOLVED'"
    " AND basis IN ('OWNER_CONDITION_CLEARED', 'HUMAN_RESOLUTION') AND successor_item_id IS NULL)"
    " OR (event = 'RESOLUTION_RECORDED' AND successor_item_id IS NULL AND ("
    "(from_state = 'OPEN' AND to_state = 'OPEN' AND basis = 'CONDITION_PERSISTS')"
    " OR (from_state = 'SUPERSEDED' AND to_state = 'SUPERSEDED' AND basis = 'OWNER_SOURCE_MOVED')))"
)
HUMAN_FIELDS = (
    "(disposition IS NOT NULL) = (event = 'RESOLUTION_RECORDED' OR basis = 'HUMAN_RESOLUTION')"
    " AND (disposition IS NOT NULL OR (note IS NULL AND evidence_reference IS NULL))"
)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("review_key"),
        # §3: at most one OPEN item per condition key.
        Index(
            "ux_review_items_one_open_per_condition",
            "condition_key",
            unique=True,
            sqlite_where=sql_text("state = 'OPEN'"),
        ),
        Index("ix_review_items_producer_state", "producer", "state"),
        CheckConstraint(_in("kind", ReviewKind), name="kind_valid"),
        CheckConstraint(_in("state", ReviewState), name="state_valid"),
        CheckConstraint("producer <> ''", name="producer_present"),
        CheckConstraint(
            "json_valid(scope_json) AND json_type(scope_json) = 'object' AND scope_json <> '{}'",
            name="scope_is_object",
        ),
        CheckConstraint("subject <> ''", name="subject_present"),
        CheckConstraint("reason_code <> ''", name="reason_code_present"),
        CheckConstraint("source_identity <> ''", name="source_identity_present"),
        CheckConstraint(_hex64("condition_key"), name="condition_key_hex"),
        CheckConstraint(_hex64("review_key"), name="review_key_hex"),
        CheckConstraint("generation >= 1", name="generation_positive"),
    )

    review_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    producer: Mapped[str] = mapped_column(String(64))
    scope_json: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(128))
    reason_code: Mapped[str] = mapped_column(String(128))
    source_identity: Mapped[str] = mapped_column(String(128))
    condition_key: Mapped[str] = mapped_column(String(64))
    review_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ReviewCoverage(Base):
    """One producer's coverage watermark and its known indexing failure (G2-B, ADR-0016 §4, §7).

    ``watermark_at`` is when the producer's last **complete** full pass ended, and
    ``process_run_id`` is the application process run that completed it; a pass that did not reach
    its end never touches either. ``failure_at`` is the newest known indexing failure still
    unrecovered: only a full pass that *started after* it clears it, which ``failures_recorded``
    proves. Whether coverage is current is
    derived from these, the process run and the freshness bound — it is never stored.
    """

    __tablename__ = "review_coverage"
    __table_args__ = (
        CheckConstraint("producer <> ''", name="producer_present"),
        CheckConstraint(
            "(watermark_at IS NULL) = (process_run_id IS NULL)"
            " AND (watermark_at IS NULL) = (pass_started_at IS NULL)",
            name="watermark_complete",
        ),
        CheckConstraint(
            "pass_started_at IS NULL OR pass_started_at <= watermark_at", name="pass_ordered"
        ),
        CheckConstraint("full_passes >= 0", name="full_passes_counted"),
        CheckConstraint("(failure_at IS NULL) = (failure_code IS NULL)", name="failure_complete"),
        CheckConstraint(
            "failures_recorded >= 0 AND (failures_recorded > 0 OR failure_at IS NULL)",
            name="failures_counted",
        ),
    )

    producer: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_run_id: Mapped[str | None] = mapped_column(String(36))
    pass_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    watermark_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    full_passes: Mapped[int] = mapped_column(Integer)
    failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    # Every known failure ever recorded, counted: a pass may clear only a failure that this count
    # already included when the pass began (review 5807477351 B2), never by comparing times.
    failures_recorded: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ReviewItemEvent(Base):
    __tablename__ = "review_item_events"
    __table_args__ = (
        UniqueConstraint("review_item_id", "event_no"),
        # §8: a human resolution is recorded at most once per item generation.
        Index(
            "ux_review_item_events_one_resolution_per_generation",
            "review_item_id",
            "generation",
            unique=True,
            sqlite_where=sql_text("disposition IS NOT NULL"),
        ),
        CheckConstraint("event_no >= 1", name="event_no_positive"),
        CheckConstraint("generation >= 1", name="generation_positive"),
        CheckConstraint(_in("event", ReviewEvent), name="event_valid"),
        CheckConstraint(
            f"from_state IS NULL OR {_in('from_state', ReviewState)}", name="from_state_valid"
        ),
        CheckConstraint(_in("to_state", ReviewState), name="to_state_valid"),
        CheckConstraint(
            f"basis IN ({', '.join(repr(b.value) for b in ReviewBasis)})", name="basis_valid"
        ),
        CheckConstraint(
            "disposition IS NULL OR disposition IN"
            f" ({', '.join(repr(d.value) for d in ReviewDisposition)})",
            name="disposition_valid",
        ),
        CheckConstraint(EVENT_SHAPE, name="event_shape"),
        CheckConstraint(HUMAN_FIELDS, name="human_fields"),
        CheckConstraint(
            f"note IS NULL OR (length(note) BETWEEN 1 AND {NOTE_MAX_CHARS})", name="note_bounded"
        ),
        CheckConstraint("actor <> ''", name="actor_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_items.review_item_id")
    )
    event_no: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(24))
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(String(32))
    successor_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("review_items.review_item_id")
    )
    disposition: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    evidence_reference: Mapped[str | None] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
