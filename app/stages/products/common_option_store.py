"""Persistence owner for immutable Common Sales Option authoring revisions."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.core.clock import Clock
from app.platform.core.errors import NotFoundError
from app.platform.db.database import Database
from app.stages.products.common_option_models import (
    CommonSalesOptionAxis,
    CommonSalesOptionRevision,
    CommonSalesOptionValue,
    CurrentCommonSalesOptionRevisionMove,
)
from app.stages.products.common_options import (
    COMMON_OPTION_SIGNATURE_VERSION,
    CanonicalCommonSalesOptionAxis,
    CommonSalesOptionAxisSpec,
    canonical_common_sales_options,
    common_sales_option_signature,
)
from app.stages.products.models import ProductGroup


@dataclass(frozen=True)
class CommonSalesOptionValueRecord:
    value_id: str
    canonical_value: str
    display_value: str
    unit_code: str | None
    ordinal: int


@dataclass(frozen=True)
class CommonSalesOptionAxisRecord:
    axis_id: str
    semantic_key: str
    display_name: str
    ordinal: int
    values: tuple[CommonSalesOptionValueRecord, ...]


@dataclass(frozen=True)
class CommonSalesOptionRevisionRecord:
    revision_id: str
    product_group_id: str
    revision_no: int
    structure_signature: str
    signature_version: str
    reason: str
    decided_by: str
    correlation_id: str
    created_at: datetime
    axes: tuple[CommonSalesOptionAxisRecord, ...]


class CommonSalesOptionStore:
    """The only production writer of Common Sales Option authoring revisions.

    There is deliberately no API/UI route in this slice. A later reviewed Product Fact mapping
    supplies the inputs; this owner persists and reads them without marketplace-specific state.
    """

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def record_reviewed_revision(
        self,
        product_group_id: str,
        axes: tuple[CommonSalesOptionAxisSpec, ...],
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> CommonSalesOptionRevisionRecord:
        canonical = canonical_common_sales_options(axes)
        signature = common_sales_option_signature(canonical)
        reason = self._required("reason", reason, 200)
        decided_by = self._required("decided_by", decided_by, 64)
        correlation_id = self._required("correlation_id", correlation_id, 64)
        with self._db.write() as session:
            if session.get(ProductGroup, product_group_id) is None:
                raise NotFoundError("COMMON_OPTION_PRODUCT_UNKNOWN", "no product has that id")
            current = self._current_row(session, product_group_id)
            if current is not None and current.structure_signature == signature:
                return self._record(session, current)
            revision_no = (
                int(
                    session.scalar(
                        select(func.max(CommonSalesOptionRevision.revision_no)).where(
                            CommonSalesOptionRevision.product_group_id == product_group_id
                        )
                    )
                    or 0
                )
                + 1
            )
            revision = CommonSalesOptionRevision(
                revision_id=str(uuid.uuid4()),
                product_group_id=product_group_id,
                revision_no=revision_no,
                structure_signature=signature,
                signature_version=COMMON_OPTION_SIGNATURE_VERSION,
                axis_count=len(canonical),
                value_count=sum(len(axis.values) for axis in canonical),
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
            )
            session.add(revision)
            session.flush()
            self._add_axes(session, revision.revision_id, canonical)
            session.flush()
            previous = None if current is None else current.revision_id
            sequence = (
                1 if current is None else self._current_sequence(session, product_group_id) + 1
            )
            session.add(
                CurrentCommonSalesOptionRevisionMove(
                    move_id=str(uuid.uuid4()),
                    product_group_id=product_group_id,
                    revision_id=revision.revision_id,
                    sequence=sequence,
                    previous_revision_id=previous,
                    reason=reason,
                    decided_by=decided_by,
                    correlation_id=correlation_id,
                    moved_at=self._clock.now(),
                )
            )
            session.flush()
            return self._record(session, revision)

    def current(self, product_group_id: str) -> CommonSalesOptionRevisionRecord | None:
        with self._db.read() as session:
            row = self._current_row(session, product_group_id)
            return None if row is None else self._record(session, row)

    def revision(self, revision_id: str) -> CommonSalesOptionRevisionRecord | None:
        with self._db.read() as session:
            row = session.get(CommonSalesOptionRevision, revision_id)
            return None if row is None else self._record(session, row)

    @staticmethod
    def _required(name: str, value: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return normalized

    @staticmethod
    def _current_row(session: Session, product_group_id: str) -> CommonSalesOptionRevision | None:
        return session.scalars(
            select(CommonSalesOptionRevision)
            .join(
                CurrentCommonSalesOptionRevisionMove,
                CurrentCommonSalesOptionRevisionMove.revision_id
                == CommonSalesOptionRevision.revision_id,
            )
            .where(CurrentCommonSalesOptionRevisionMove.product_group_id == product_group_id)
            .order_by(CurrentCommonSalesOptionRevisionMove.sequence.desc())
        ).first()

    @staticmethod
    def _current_sequence(session: Session, product_group_id: str) -> int:
        return int(
            session.scalar(
                select(func.max(CurrentCommonSalesOptionRevisionMove.sequence)).where(
                    CurrentCommonSalesOptionRevisionMove.product_group_id == product_group_id
                )
            )
            or 0
        )

    @staticmethod
    def _add_axes(
        session: Session,
        revision_id: str,
        axes: tuple[CanonicalCommonSalesOptionAxis, ...],
    ) -> None:
        for axis_ordinal, axis in enumerate(axes):
            axis_id = str(uuid.uuid4())
            session.add(
                CommonSalesOptionAxis(
                    axis_id=axis_id,
                    revision_id=revision_id,
                    semantic_key=axis.semantic_key,
                    display_name=axis.display_name,
                    ordinal=axis_ordinal,
                )
            )
            session.flush()
            for value_ordinal, value in enumerate(axis.values):
                session.add(
                    CommonSalesOptionValue(
                        value_id=str(uuid.uuid4()),
                        axis_id=axis_id,
                        canonical_value=value.canonical_value,
                        unit_code=value.unit_code or "",
                        display_value=value.display_value,
                        ordinal=value_ordinal,
                    )
                )

    @staticmethod
    def _record(
        session: Session, revision: CommonSalesOptionRevision
    ) -> CommonSalesOptionRevisionRecord:
        axes: list[CommonSalesOptionAxisRecord] = []
        for axis in session.scalars(
            select(CommonSalesOptionAxis)
            .where(CommonSalesOptionAxis.revision_id == revision.revision_id)
            .order_by(CommonSalesOptionAxis.ordinal)
        ):
            values = tuple(
                CommonSalesOptionValueRecord(
                    value.value_id,
                    value.canonical_value,
                    value.display_value,
                    value.unit_code or None,
                    value.ordinal,
                )
                for value in session.scalars(
                    select(CommonSalesOptionValue)
                    .where(CommonSalesOptionValue.axis_id == axis.axis_id)
                    .order_by(CommonSalesOptionValue.ordinal)
                )
            )
            axes.append(
                CommonSalesOptionAxisRecord(
                    axis.axis_id, axis.semantic_key, axis.display_name, axis.ordinal, values
                )
            )
        return CommonSalesOptionRevisionRecord(
            revision.revision_id,
            revision.product_group_id,
            revision.revision_no,
            revision.structure_signature,
            revision.signature_version,
            revision.reason,
            revision.decided_by,
            revision.correlation_id,
            revision.created_at,
            tuple(axes),
        )


__all__ = [
    "CommonSalesOptionAxisRecord",
    "CommonSalesOptionRevisionRecord",
    "CommonSalesOptionStore",
    "CommonSalesOptionValueRecord",
]
