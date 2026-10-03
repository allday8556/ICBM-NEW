"""An operator's synthetic test product (owner decision 2026-10-03).

The owner decided that the first SmartStore canaries run on test products of their own, made with
the same conditions as collected products, rather than on a real collected product, and that the
test listings are deleted afterwards. This owner makes one: a copy of one collected revision's
facts, **unchanged**, under a new identity in the reserved ``icbm-synthetic`` supplier namespace.

**Nothing is invented.** Every field, every evidence entry and every image reference is the
collected revision's own, so every fingerprint recomputes; only the identity is new. The product
then materializes, is priced and is prepared exactly like a collected one. The listing name a
test listing carries ("[테스트] 테스트상품1") is the operator's authored value at preparation,
never a source fact.

**It is labelled, durably and first.** ``synthetic_test_products`` (migration 0038) records which
revision was copied, the operator's label, who made it and why — in the same unit that opens the
copy's collection run, before anything else of the copy exists. The copy's run states no
transport, because no document was acquired. The reserved namespace has no registered supplier,
so no acquisition entry point (URL, extension, list queue) can ever write into it, and a real
collection can never share an identity with a test product. A copy of a copy is refused.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.collect.facts import IMAGES_FIELD, CollectedFacts, FieldFact
from app.stages.collect.models import SYNTHETIC_SUPPLIER_KEY, SyntheticTestProduct
from app.stages.collect.revisions import ProductFactsRevisionStore
from app.stages.collect.runs import CollectionRunStore

LABEL_MAX = 40


@dataclass(frozen=True)
class SyntheticRecord:
    synthetic_id: str
    supplier_key: str
    source_product_id: str
    template_revision_id: str
    template_supplier_key: str
    template_source_product_id: str
    collection_run_id: str
    label: str
    reason: str | None
    created_by: str
    created_at: datetime


def _record(row: SyntheticTestProduct) -> SyntheticRecord:
    return SyntheticRecord(
        synthetic_id=row.synthetic_id,
        supplier_key=row.supplier_key,
        source_product_id=row.source_product_id,
        template_revision_id=row.template_revision_id,
        template_supplier_key=row.template_supplier_key,
        template_source_product_id=row.template_source_product_id,
        collection_run_id=row.collection_run_id,
        label=row.label,
        reason=row.reason,
        created_by=row.created_by,
        created_at=row.created_at,
    )


class SyntheticTestProductService:
    def __init__(
        self,
        *,
        db: Database,
        runs: CollectionRunStore,
        revisions: ProductFactsRevisionStore,
        audit: AuditLog,
        clock: Clock,
        after_recorded: Callable[[str], None],
    ) -> None:
        self._db = db
        self._runs = runs
        self._revisions = revisions
        self._audit = audit
        self._clock = clock
        # What follows any durably RECORDED run: the Product, then the review fast path.
        self._after_recorded = after_recorded

    def list(self) -> tuple[SyntheticRecord, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(SyntheticTestProduct).order_by(SyntheticTestProduct.created_at)
            )
            return tuple(_record(row) for row in rows)

    def create(
        self,
        *,
        template_revision_id: str,
        label: str,
        reason: str | None,
        actor: str,
        correlation_id: str,
    ) -> SyntheticRecord:
        """Copy one collected revision's facts, unchanged, as a labelled synthetic test product."""
        label = label.strip()
        if not label or len(label) > LABEL_MAX:
            raise InputValidationError(
                "SYNTHETIC_LABEL_INVALID", f"a label has 1 to {LABEL_MAX} characters"
            )
        if not actor:
            raise InputValidationError(
                "SYNTHETIC_ACTOR_REQUIRED", "a test product names its operator"
            )
        template = self._revisions.get(template_revision_id)
        if template is None:
            raise NotFoundError("SYNTHETIC_TEMPLATE_UNKNOWN", "no collected revision has that id")
        if template.supplier_key == SYNTHETIC_SUPPLIER_KEY:
            raise InputValidationError(
                "SYNTHETIC_TEMPLATE_IS_SYNTHETIC", "a test product copies a collected revision"
            )
        synthetic_id = str(uuid.uuid4())
        source_product_id = f"synthetic-{synthetic_id}"
        with self._db.write() as session:
            if session.scalars(
                select(SyntheticTestProduct).where(SyntheticTestProduct.label == label)
            ).first():
                raise InputValidationError("SYNTHETIC_LABEL_TAKEN", "that label is already used")
            # No document is acquired, so the run states no transport (``provenance=None``).
            run_id = self._runs.open(
                session,
                job_id=f"synthetic:{synthetic_id}",
                correlation_id=correlation_id,
                supplier_key=SYNTHETIC_SUPPLIER_KEY,
                source_url=template.source_url,
                provenance=None,
            )
            row = SyntheticTestProduct(
                synthetic_id=synthetic_id,
                supplier_key=SYNTHETIC_SUPPLIER_KEY,
                source_product_id=source_product_id,
                template_revision_id=template.revision_id,
                template_supplier_key=template.supplier_key,
                template_source_product_id=template.source_product_id,
                collection_run_id=run_id,
                label=label,
                reason=reason,
                created_by=actor,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            record = _record(row)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.SYNTHETIC_TEST_PRODUCT_RECORDED,
                    action="create_synthetic_test_product",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=f"source_product:{SYNTHETIC_SUPPLIER_KEY}/{source_product_id}",
                    details={
                        "synthetic_id": synthetic_id,
                        "label": label,
                        "template_revision_id": template.revision_id,
                        "collection_run_id": run_id,
                    },
                    correlation_id=correlation_id,
                ),
                session=session,
            )
        self._runs.note_identity(run_id, source_product_id=source_product_id)
        revision = self._revisions.append(
            CollectedFacts(
                supplier_key=SYNTHETIC_SUPPLIER_KEY,
                source_product_id=source_product_id,
                source_url=template.source_url,
                captured_at=template.captured_at,
                extractor_revision=template.extractor_revision,
                extractor_fingerprint=template.extractor_fingerprint,
                collection_run_id=run_id,
                correlation_id=correlation_id,
                fields={
                    key: FieldFact(
                        field.status,
                        field.value,
                        tuple(stored.evidence for stored in field.evidence),
                    )
                    for key, field in template.fields.items()
                    if key != IMAGES_FIELD
                },
                images=template.images,
            )
        )
        self._runs.recorded(
            run_id, revision_id=revision.revision_id, facts_status=revision.facts_status
        )
        self._after_recorded(run_id)
        return record


__all__ = ["SYNTHETIC_SUPPLIER_KEY", "SyntheticRecord", "SyntheticTestProductService"]
