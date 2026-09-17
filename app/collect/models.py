"""Persistence of COLLECT source truth (M3 PR-B, ADR-0010 §6–§9).

Five append-only tables — the migration installs triggers rejecting UPDATE and DELETE on each: a
revision, its fields, their evidence, its ordered image references, and the content-addressed
source assets those references point to. CHECK constraints repeat the single-row invariants of
``app.collect.facts``, so no write path can store a forbidden value even if it bypasses the
domain. There is no canonical Product here; that is M4 (ADR-0010 §1).
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.collect.facts import (
    EVIDENCE_OBSERVED_MAX_BYTES,
    EvidenceKind,
    FactsStatus,
    FieldLevel,
    FieldStatus,
    ImageIssue,
    ImageRole,
)
from app.db.base import Base
from app.db.types import UTCDateTime


def _in(column: str, values: Iterable[str], *, nullable: bool = False) -> str:
    clause = f"{column} IN ({', '.join(repr(str(v)) for v in values)})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


def _hex64(column: str, *, nullable: bool = False) -> str:
    clause = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    return f"{column} IS NULL OR ({clause})" if nullable else clause


class ProductFactsRevision(Base):
    """One immutable revision per successful collection, numbered per source identity."""

    __tablename__ = "product_facts_revisions"
    __table_args__ = (
        UniqueConstraint("supplier_key", "source_product_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("supplier_key <> ''", name="supplier_key_present"),
        CheckConstraint("source_product_id <> ''", name="source_identity_present"),
        CheckConstraint("source_url LIKE 'https://%'", name="source_url_https"),
        CheckConstraint("currency = 'KRW'", name="currency_krw"),
        CheckConstraint("extractor_revision <> ''", name="extractor_revision_present"),
        CheckConstraint(_hex64("extractor_fingerprint"), name="extractor_fingerprint_hex"),
        CheckConstraint(_hex64("source_fingerprint"), name="source_fingerprint_hex"),
        CheckConstraint("collection_run_id <> ''", name="collection_run_present"),
        # Review 5231130447 P0: a run appends its revision and then settles. If it dies between
        # the two, the retry must recover this row — never append a second one beside it.
        Index("ux_product_facts_revisions_collection_run", "collection_run_id", unique=True),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint(_in("facts_status", FactsStatus), name="facts_status_valid"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_product_id: Mapped[str] = mapped_column(String(200))
    sequence: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    currency: Mapped[str] = mapped_column(String(3))
    extractor_revision: Mapped[str] = mapped_column(String(64))
    extractor_fingerprint: Mapped[str] = mapped_column(String(64))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    collection_run_id: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    facts_status: Mapped[str] = mapped_column(String(20))


class ProductFactsField(Base):
    """One field of a revision: its level at recording, status, canonical value and fingerprint."""

    __tablename__ = "product_facts_fields"
    __table_args__ = (
        CheckConstraint("field_key <> ''", name="field_key_present"),
        CheckConstraint(_in("level", FieldLevel), name="level_valid"),
        CheckConstraint(_in("status", FieldStatus), name="status_valid"),
        CheckConstraint("value_json IS NULL OR json_valid(value_json)", name="value_is_json"),
        CheckConstraint("status <> 'ABSENT' OR value_json IS NULL", name="absent_has_no_value"),
        CheckConstraint(
            "status <> 'CONFIRMED' OR value_json IS NOT NULL", name="confirmed_has_value"
        ),
        CheckConstraint(_hex64("field_fingerprint"), name="field_fingerprint_hex"),
    )

    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id"), primary_key=True
    )
    field_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    level: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    value_json: Mapped[str | None] = mapped_column(Text)
    field_fingerprint: Mapped[str] = mapped_column(String(64))


class ProductFactsEvidence(Base):
    """Product-scoped evidence of one field, in order (ADR-0010 §8)."""

    __tablename__ = "product_facts_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(_in("kind", EvidenceKind), name="kind_valid"),
        CheckConstraint("locator <> ''", name="locator_present"),
        CheckConstraint(
            f"observed IS NULL OR length(CAST(observed AS BLOB)) <= {EVIDENCE_OBSERVED_MAX_BYTES}",
            name="observed_bounded",
        ),
        CheckConstraint(_in("status", FieldStatus), name="status_valid"),
        CheckConstraint(_hex64("digest"), name="digest_hex"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    field_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    locator: Mapped[str] = mapped_column(Text)
    observed: Mapped[str | None] = mapped_column(Text)
    normalized: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    digest: Mapped[str] = mapped_column(String(64))


class SourceAsset(Base):
    """Content-addressed source bytes (ADR-0010 §9): stored once, never rewritten. Width, height
    and MIME come from decoding the stored bytes."""

    __tablename__ = "source_assets"
    __table_args__ = (
        CheckConstraint(_hex64("sha256"), name="sha256_hex"),
        CheckConstraint("mime_type LIKE 'image/%'", name="mime_is_image"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("width > 0 AND height > 0", name="dimensions_positive"),
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    mime_type: Mapped[str] = mapped_column(String(40))
    byte_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    stored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProductFactsImageRef(Base):
    """One ordered image reference of a revision. It holds a sanitized stable locator only when
    one is proven, and never a secret-bearing URL (ADR-0010 §9)."""

    __tablename__ = "product_facts_image_refs"
    __table_args__ = (
        CheckConstraint(_in("role", ImageRole), name="role_valid"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("host <> ''", name="host_present"),
        CheckConstraint("provenance <> ''", name="provenance_present"),
        CheckConstraint("locator IS NULL OR locator LIKE 'https://%'", name="locator_https"),
        CheckConstraint(_hex64("sha256", nullable=True), name="sha256_hex"),
        CheckConstraint(
            _in("status", (FieldStatus.CONFIRMED, FieldStatus.REVIEW_REQUIRED)),
            name="status_valid",
        ),
        CheckConstraint(_in("issue", ImageIssue, nullable=True), name="issue_valid"),
        CheckConstraint(
            "(status = 'CONFIRMED' AND sha256 IS NOT NULL AND issue IS NULL)"
            " OR (status = 'REVIEW_REQUIRED' AND issue IS NOT NULL)",
            name="status_matches_issue",
        ),
    )

    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(253))
    provenance: Mapped[str] = mapped_column(Text)
    locator: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), ForeignKey("source_assets.sha256"))
    status: Mapped[str] = mapped_column(String(20))
    issue: Mapped[str | None] = mapped_column(String(30))
    http_etag: Mapped[str | None] = mapped_column(Text)
    http_last_modified: Mapped[str | None] = mapped_column(Text)


class CollectionOutcome(StrEnum):
    """What one collection run ended as (ADR-0010 §6; Issue #52 ruling 5706133893).

    ``NO_REVISION`` is a success, not a failure: the run read the product and the source stated no
    stable identity to record against, so there is nothing to append and nothing a retry could
    change. A run that could not finish its work at all is ``FAILED``.
    """

    PENDING = "PENDING"
    RECORDED = "RECORDED"
    NO_REVISION = "NO_REVISION"
    FAILED = "FAILED"


class CollectionRun(Base):
    """The durable identity of one operator-submitted collection, and what it produced.

    A run row is opened in the same unit of work as its job, so the operator holds a result
    identity from the moment the request is accepted. It is the only place a caller has to look to
    learn whether a revision exists; it never holds page content, only a code.
    """

    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint(_in("outcome", CollectionOutcome), name="outcome_valid"),
        CheckConstraint(_in("facts_status", FactsStatus, nullable=True), name="facts_status_valid"),
        CheckConstraint(
            "(outcome = 'RECORDED' AND revision_id IS NOT NULL AND facts_status IS NOT NULL)"
            " OR (outcome <> 'RECORDED' AND revision_id IS NULL AND facts_status IS NULL)",
            name="revision_only_when_recorded",
        ),
        CheckConstraint(
            "(outcome IN ('PENDING')) = (finished_at IS NULL)", name="finished_when_terminal"
        ),
        CheckConstraint("source_url LIKE 'https://%'", name="source_url_https"),
        Index("ix_collection_runs_job_id", "job_id"),
        Index("ix_collection_runs_supplier", "supplier_key", "requested_at"),
        Index("ix_collection_runs_product_read", "supplier_key", "source_url", "product_read_at"),
    )

    collection_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36))
    correlation_id: Mapped[str] = mapped_column(String(64))
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(20))
    revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    facts_status: Mapped[str | None] = mapped_column(String(20))
    # Why there is no revision, or which error ended the run: a code of ours, never page content.
    detail: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # When this run durably reserved its one product read. It is what the next run for the same
    # product is measured against, so a restart cannot read the same page twice inside the
    # interval ADR-0010 §4 fixes.
    product_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
