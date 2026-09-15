"""Append-only ProductFactsRevision storage and read-back (M3 PR-B, ADR-0010 §6).

``append`` stores exactly what :func:`app.collect.facts.evaluate` computed — fields, evidence,
image references, fingerprints and ``facts_status`` — in one write transaction, as the next
sequence number of its source identity. Every successful call creates a new immutable revision,
even when nothing changed (Issue #52 §4). Nothing here updates or deletes, and the database
refuses both anyway.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collect.facts import (
    CURRENCY,
    FIELD_REGISTRY,
    CollectedFacts,
    EvaluatedFacts,
    Evidence,
    EvidenceKind,
    FactsStatus,
    FactValue,
    FieldLevel,
    FieldStatus,
    ImageIssue,
    ImageReference,
    ImageRole,
    evaluate,
    evidence_digest,
    field_fingerprint,
    image_order,
    source_fingerprint,
    value_from_json,
)
from app.collect.models import (
    ProductFactsEvidence,
    ProductFactsField,
    ProductFactsImageRef,
    ProductFactsRevision,
    SourceAsset,
)
from app.core.clock import Clock
from app.core.errors import InputValidationError
from app.db.database import Database

_FIELD_ORDER = {key: index for index, key in enumerate(FIELD_REGISTRY)}


@dataclass(frozen=True)
class StoredEvidence:
    evidence: Evidence
    digest: str


@dataclass(frozen=True)
class StoredField:
    key: str
    level: FieldLevel
    status: FieldStatus
    value: FactValue | None
    value_json: str | None
    fingerprint: str
    evidence: tuple[StoredEvidence, ...]


@dataclass(frozen=True)
class StoredRevision:
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
    fields: Mapping[str, StoredField]  # registry order
    images: tuple[ImageReference, ...]  # representative first, then source order

    def fingerprints_intact(self) -> bool:
        """Recompute every evidence digest and fingerprint from the stored content alone."""
        for field in self.fields.values():
            digests = [evidence_digest(stored.evidence) for stored in field.evidence]
            if digests != [stored.digest for stored in field.evidence]:
                return False
            recomputed = field_fingerprint(field.key, field.status, field.value_json, digests)
            if recomputed != field.fingerprint:
                return False
        return self.source_fingerprint == source_fingerprint(
            ((field.key, field.fingerprint) for field in self.fields.values()),
            ((ref.role, ref.ordinal, ref.sha256) for ref in self.images),
        )


class ProductFactsRevisionStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def append(self, collected: CollectedFacts) -> StoredRevision:
        """Validate, evaluate and append one revision; return it as read back."""
        evaluated = evaluate(collected)
        revision_id = str(uuid.uuid4())
        with self._db.write() as session:
            self._require_stored_assets(session, evaluated)
            last = session.scalar(
                select(func.max(ProductFactsRevision.sequence)).where(
                    ProductFactsRevision.supplier_key == collected.supplier_key,
                    ProductFactsRevision.source_product_id == collected.source_product_id,
                )
            )
            session.add(
                ProductFactsRevision(
                    revision_id=revision_id,
                    supplier_key=collected.supplier_key,
                    source_product_id=collected.source_product_id,
                    sequence=(last or 0) + 1,
                    source_url=collected.source_url,
                    captured_at=collected.captured_at,
                    recorded_at=self._clock.now(),
                    currency=CURRENCY,
                    extractor_revision=collected.extractor_revision,
                    extractor_fingerprint=collected.extractor_fingerprint,
                    source_fingerprint=evaluated.source_fingerprint,
                    collection_run_id=collected.collection_run_id,
                    correlation_id=collected.correlation_id,
                    facts_status=evaluated.facts_status.value,
                )
            )
            session.flush()
            session.add_all(
                ProductFactsField(
                    revision_id=revision_id,
                    field_key=field.key,
                    level=field.level.value,
                    status=field.status.value,
                    value_json=field.value_json,
                    field_fingerprint=field.fingerprint,
                )
                for field in evaluated.fields
            )
            session.flush()
            session.add_all(
                ProductFactsEvidence(
                    revision_id=revision_id,
                    field_key=field.key,
                    ordinal=ordinal,
                    kind=entry.evidence.kind.value,
                    locator=entry.evidence.locator,
                    observed=entry.evidence.observed,
                    normalized=entry.evidence.normalized,
                    status=entry.evidence.status.value,
                    digest=entry.digest,
                )
                for field in evaluated.fields
                for ordinal, entry in enumerate(field.evidence)
            )
            session.add_all(
                ProductFactsImageRef(
                    revision_id=revision_id,
                    role=ref.role.value,
                    ordinal=ref.ordinal,
                    host=ref.host,
                    provenance=ref.provenance,
                    locator=ref.locator,
                    sha256=ref.sha256,
                    status=ref.status.value,
                    issue=None if ref.issue is None else ref.issue.value,
                    http_etag=ref.etag,
                    http_last_modified=ref.last_modified,
                )
                for ref in evaluated.images
            )
        stored = self.get(revision_id)
        assert stored is not None
        return stored

    def get(self, revision_id: str) -> StoredRevision | None:
        with self._db.read() as session:
            row = session.get(ProductFactsRevision, revision_id)
            return None if row is None else self._load(session, row)

    def history(self, supplier_key: str, source_product_id: str) -> tuple[StoredRevision, ...]:
        """Every revision of one source identity, oldest first."""
        with self._db.read() as session:
            rows = session.scalars(
                select(ProductFactsRevision)
                .where(
                    ProductFactsRevision.supplier_key == supplier_key,
                    ProductFactsRevision.source_product_id == source_product_id,
                )
                .order_by(ProductFactsRevision.sequence)
            ).all()
            return tuple(self._load(session, row) for row in rows)

    @staticmethod
    def _require_stored_assets(session: Session, evaluated: EvaluatedFacts) -> None:
        named = {ref.sha256 for ref in evaluated.images if ref.sha256 is not None}
        if not named:
            return
        stored = set(
            session.scalars(select(SourceAsset.sha256).where(SourceAsset.sha256.in_(named)))
        )
        if named - stored:
            raise InputValidationError(
                "COLLECT_IMAGE_ASSET_MISSING",
                "an image reference names bytes that were not stored as a source asset",
            )

    @staticmethod
    def _load(session: Session, row: ProductFactsRevision) -> StoredRevision:
        evidence: dict[str, list[StoredEvidence]] = {}
        for entry in session.scalars(
            select(ProductFactsEvidence)
            .where(ProductFactsEvidence.revision_id == row.revision_id)
            .order_by(ProductFactsEvidence.field_key, ProductFactsEvidence.ordinal)
        ):
            evidence.setdefault(entry.field_key, []).append(
                StoredEvidence(
                    Evidence(
                        kind=EvidenceKind(entry.kind),
                        locator=entry.locator,
                        status=FieldStatus(entry.status),
                        observed=entry.observed,
                        normalized=entry.normalized,
                    ),
                    entry.digest,
                )
            )
        field_rows = session.scalars(
            select(ProductFactsField).where(ProductFactsField.revision_id == row.revision_id)
        ).all()
        fields = {
            field.field_key: StoredField(
                key=field.field_key,
                level=FieldLevel(field.level),
                status=FieldStatus(field.status),
                value=value_from_json(field.field_key, field.value_json),
                value_json=field.value_json,
                fingerprint=field.field_fingerprint,
                evidence=tuple(evidence.get(field.field_key, ())),
            )
            for field in sorted(field_rows, key=lambda field: _FIELD_ORDER[field.field_key])
        }
        images = tuple(
            sorted(
                (
                    ImageReference(
                        role=ImageRole(ref.role),
                        ordinal=ref.ordinal,
                        host=ref.host,
                        provenance=ref.provenance,
                        status=FieldStatus(ref.status),
                        sha256=ref.sha256,
                        locator=ref.locator,
                        issue=None if ref.issue is None else ImageIssue(ref.issue),
                        etag=ref.http_etag,
                        last_modified=ref.http_last_modified,
                    )
                    for ref in session.scalars(
                        select(ProductFactsImageRef).where(
                            ProductFactsImageRef.revision_id == row.revision_id
                        )
                    )
                ),
                key=image_order,
            )
        )
        return StoredRevision(
            revision_id=row.revision_id,
            supplier_key=row.supplier_key,
            source_product_id=row.source_product_id,
            sequence=row.sequence,
            source_url=row.source_url,
            captured_at=row.captured_at,
            recorded_at=row.recorded_at,
            currency=row.currency,
            extractor_revision=row.extractor_revision,
            extractor_fingerprint=row.extractor_fingerprint,
            source_fingerprint=row.source_fingerprint,
            collection_run_id=row.collection_run_id,
            correlation_id=row.correlation_id,
            facts_status=FactsStatus(row.facts_status),
            fields=fields,
            images=images,
        )
