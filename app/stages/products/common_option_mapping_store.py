"""Persistence owner for reviewed Product Fact -> Common Sales Option mappings."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.collect.facts import FieldStatus
from app.stages.collect.models import ProductFactsField, ProductFactsRevision
from app.stages.products.common_option_mapping import (
    FACT_MAPPING_SIGNATURE_VERSION,
    CommonOptionAxisFactMappingSpec,
    ProductFactPointer,
    canonical_fact_leaf_json,
    common_option_fact_mapping_signature,
    fact_leaf,
    validate_fact_pointer,
)
from app.stages.products.common_option_mapping_models import (
    CommonOptionFactAxisMapping,
    CommonOptionFactMappingRevision,
    CommonOptionFactValueMapping,
    CurrentCommonOptionFactMappingMove,
)
from app.stages.products.common_option_models import (
    CommonSalesOptionAxis,
    CommonSalesOptionRevision,
    CommonSalesOptionValue,
    CurrentCommonSalesOptionRevisionMove,
)
from app.stages.products.store import current_revision_for_confirmed_member


@dataclass(frozen=True)
class FactPointerRecord:
    revision_id: str
    field_key: str
    json_path: str
    source_value_json: str
    field_fingerprint: str


@dataclass(frozen=True)
class ValueFactMappingRecord:
    value_mapping_id: str
    value_id: str
    ordinal: int
    source: FactPointerRecord


@dataclass(frozen=True)
class AxisFactMappingRecord:
    axis_mapping_id: str
    axis_id: str
    ordinal: int
    source: FactPointerRecord
    values: tuple[ValueFactMappingRecord, ...]


@dataclass(frozen=True)
class CommonOptionFactMappingRecord:
    mapping_revision_id: str
    product_group_id: str
    common_option_revision_id: str
    revision_no: int
    mapping_signature: str
    signature_version: str
    evidence_reference: str
    reason: str
    reviewed_by: str
    correlation_id: str
    reviewed_at: datetime
    axes: tuple[AxisFactMappingRecord, ...]


class CommonOptionFactMappingStore:
    """Records one complete explicit correspondence for a current Common Option revision."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def record_reviewed_mapping(
        self,
        product_group_id: str,
        common_option_revision_id: str,
        axes: tuple[CommonOptionAxisFactMappingSpec, ...],
        *,
        evidence_reference: str,
        reason: str,
        reviewed_by: str,
        correlation_id: str,
    ) -> CommonOptionFactMappingRecord:
        evidence_reference = self._required("evidence_reference", evidence_reference, 300)
        reason = self._required("reason", reason, 200)
        reviewed_by = self._required("reviewed_by", reviewed_by, 64)
        correlation_id = self._required("correlation_id", correlation_id, 64)
        with self._db.write() as session:
            option_revision = session.get(CommonSalesOptionRevision, common_option_revision_id)
            if option_revision is None or option_revision.product_group_id != product_group_id:
                raise NotFoundError(
                    "COMMON_OPTION_REVISION_UNKNOWN",
                    "no Common Sales Option revision belongs to that product",
                )
            if (
                self._current_option_revision(session, product_group_id)
                != common_option_revision_id
            ):
                raise InputValidationError(
                    "COMMON_OPTION_MAPPING_STALE_TARGET",
                    "a reviewed mapping must target the current Common Sales Option revision",
                )
            target_axes = tuple(
                session.scalars(
                    select(CommonSalesOptionAxis)
                    .where(CommonSalesOptionAxis.revision_id == common_option_revision_id)
                    .order_by(CommonSalesOptionAxis.ordinal)
                )
            )
            supplied = {spec.axis_id: spec for spec in axes}
            if len(supplied) != len(axes) or set(supplied) != {
                axis.axis_id for axis in target_axes
            }:
                raise InputValidationError(
                    "COMMON_OPTION_MAPPING_INCOMPLETE_AXES",
                    "a reviewed mapping names every Common Sales Option axis exactly once",
                )

            resolved: list[
                tuple[
                    CommonSalesOptionAxis,
                    FactPointerRecord,
                    list[tuple[CommonSalesOptionValue, FactPointerRecord]],
                ]
            ] = []
            signature_axes: list[dict[str, object]] = []
            seen_sources: set[tuple[str, str, str]] = set()
            for axis in target_axes:
                spec = supplied[axis.axis_id]
                source = self._resolve_fact(session, product_group_id, spec.source)
                self._unique_source(seen_sources, source)
                target_values = tuple(
                    session.scalars(
                        select(CommonSalesOptionValue)
                        .where(CommonSalesOptionValue.axis_id == axis.axis_id)
                        .order_by(CommonSalesOptionValue.ordinal)
                    )
                )
                supplied_values = {value.value_id: value for value in spec.values}
                if len(supplied_values) != len(spec.values) or set(supplied_values) != {
                    value.value_id for value in target_values
                }:
                    raise InputValidationError(
                        "COMMON_OPTION_MAPPING_INCOMPLETE_VALUES",
                        "a reviewed mapping names every value of axis"
                        f" {axis.semantic_key} exactly once",
                    )
                resolved_values: list[tuple[CommonSalesOptionValue, FactPointerRecord]] = []
                signature_values: list[dict[str, object]] = []
                for value in target_values:
                    value_source = self._resolve_fact(
                        session, product_group_id, supplied_values[value.value_id].source
                    )
                    self._unique_source(seen_sources, value_source)
                    resolved_values.append((value, value_source))
                    signature_values.append(self._signature_pointer(value.value_id, value_source))
                resolved.append((axis, source, resolved_values))
                signature_axes.append(
                    {
                        "axis": self._signature_pointer(axis.axis_id, source),
                        "values": signature_values,
                    }
                )

            signature = common_option_fact_mapping_signature(signature_axes)
            current = self._current_row(session, product_group_id)
            if (
                current is not None
                and current.common_option_revision_id == common_option_revision_id
                and current.mapping_signature == signature
            ):
                return self._record(session, current)

            revision_no = (
                int(
                    session.scalar(
                        select(func.max(CommonOptionFactMappingRevision.revision_no)).where(
                            CommonOptionFactMappingRevision.product_group_id == product_group_id
                        )
                    )
                    or 0
                )
                + 1
            )
            row = CommonOptionFactMappingRevision(
                mapping_revision_id=str(uuid.uuid4()),
                product_group_id=product_group_id,
                common_option_revision_id=common_option_revision_id,
                revision_no=revision_no,
                mapping_signature=signature,
                signature_version=FACT_MAPPING_SIGNATURE_VERSION,
                axis_mapping_count=len(resolved),
                value_mapping_count=sum(len(values) for _, _, values in resolved),
                evidence_reference=evidence_reference,
                reason=reason,
                reviewed_by=reviewed_by,
                correlation_id=correlation_id,
                reviewed_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            for axis_ordinal, (axis, source, values) in enumerate(resolved):
                axis_mapping_id = str(uuid.uuid4())
                session.add(
                    CommonOptionFactAxisMapping(
                        axis_mapping_id=axis_mapping_id,
                        mapping_revision_id=row.mapping_revision_id,
                        common_option_axis_id=axis.axis_id,
                        source_revision_id=source.revision_id,
                        source_field_key=source.field_key,
                        source_json_path=source.json_path,
                        source_value_json=source.source_value_json,
                        source_field_fingerprint=source.field_fingerprint,
                        ordinal=axis_ordinal,
                    )
                )
                session.flush()
                for value_ordinal, (value, value_source) in enumerate(values):
                    session.add(
                        CommonOptionFactValueMapping(
                            value_mapping_id=str(uuid.uuid4()),
                            mapping_revision_id=row.mapping_revision_id,
                            axis_mapping_id=axis_mapping_id,
                            common_option_value_id=value.value_id,
                            source_revision_id=value_source.revision_id,
                            source_field_key=value_source.field_key,
                            source_json_path=value_source.json_path,
                            source_value_json=value_source.source_value_json,
                            source_field_fingerprint=value_source.field_fingerprint,
                            ordinal=value_ordinal,
                        )
                    )
            session.flush()
            previous = None if current is None else current.mapping_revision_id
            sequence = (
                1 if current is None else self._current_sequence(session, product_group_id) + 1
            )
            session.add(
                CurrentCommonOptionFactMappingMove(
                    move_id=str(uuid.uuid4()),
                    product_group_id=product_group_id,
                    mapping_revision_id=row.mapping_revision_id,
                    sequence=sequence,
                    previous_mapping_revision_id=previous,
                    reason=reason,
                    reviewed_by=reviewed_by,
                    correlation_id=correlation_id,
                    moved_at=self._clock.now(),
                )
            )
            session.flush()
            return self._record(session, row)

    def current(self, product_group_id: str) -> CommonOptionFactMappingRecord | None:
        with self._db.read() as session:
            row = self._current_row(session, product_group_id)
            return None if row is None else self._record(session, row)

    def revision(self, mapping_revision_id: str) -> CommonOptionFactMappingRecord | None:
        with self._db.read() as session:
            row = session.get(CommonOptionFactMappingRevision, mapping_revision_id)
            return None if row is None else self._record(session, row)

    @staticmethod
    def _required(name: str, value: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return normalized

    @staticmethod
    def _unique_source(seen: set[tuple[str, str, str]], source: FactPointerRecord) -> None:
        identity = (source.revision_id, source.field_key, source.json_path)
        if identity in seen:
            raise InputValidationError(
                "COMMON_OPTION_MAPPING_REUSES_FACT",
                "one Product Fact leaf cannot prove two Common Sales Option targets",
            )
        seen.add(identity)

    @staticmethod
    def _signature_pointer(target_id: str, source: FactPointerRecord) -> dict[str, object]:
        return {
            "target_id": target_id,
            "source_revision_id": source.revision_id,
            "source_field_key": source.field_key,
            "source_json_path": source.json_path,
            "source_value_json": source.source_value_json,
            "source_field_fingerprint": source.field_fingerprint,
        }

    @staticmethod
    def _resolve_fact(
        session: Session, product_group_id: str, pointer: ProductFactPointer
    ) -> FactPointerRecord:
        pointer = validate_fact_pointer(pointer)
        result = session.execute(
            select(ProductFactsField, ProductFactsRevision)
            .join(
                ProductFactsRevision,
                ProductFactsRevision.revision_id == ProductFactsField.revision_id,
            )
            .where(
                ProductFactsField.revision_id == pointer.revision_id,
                ProductFactsField.field_key == pointer.field_key,
            )
        ).one_or_none()
        if result is None:
            raise NotFoundError(
                "COMMON_OPTION_MAPPING_FACT_UNKNOWN", "the mapped Product Fact field does not exist"
            )
        field, revision = result
        if field.status != FieldStatus.CONFIRMED.value or field.value_json is None:
            raise InputValidationError(
                "COMMON_OPTION_MAPPING_FACT_UNCONFIRMED",
                "only a CONFIRMED Product Fact value can be mapped",
            )
        is_member, current_revision = current_revision_for_confirmed_member(
            session,
            product_group_id,
            revision.supplier_key,
            revision.source_product_id,
        )
        if not is_member:
            raise InputValidationError(
                "COMMON_OPTION_MAPPING_FACT_OUTSIDE_PRODUCT",
                "the mapped Product Fact must belong to a confirmed member of the product",
            )
        if current_revision != pointer.revision_id:
            raise InputValidationError(
                "COMMON_OPTION_MAPPING_FACT_NOT_CURRENT",
                "the mapped Product Fact revision must be current for its source product",
            )
        leaf = fact_leaf(field.value_json, pointer.json_path)
        return FactPointerRecord(
            pointer.revision_id,
            pointer.field_key,
            pointer.json_path,
            canonical_fact_leaf_json(leaf),
            field.field_fingerprint,
        )

    @staticmethod
    def _current_option_revision(session: Session, product_group_id: str) -> str | None:
        return session.scalar(
            select(CurrentCommonSalesOptionRevisionMove.revision_id)
            .where(CurrentCommonSalesOptionRevisionMove.product_group_id == product_group_id)
            .order_by(CurrentCommonSalesOptionRevisionMove.sequence.desc())
        )

    @staticmethod
    def _current_row(
        session: Session, product_group_id: str
    ) -> CommonOptionFactMappingRevision | None:
        return session.scalars(
            select(CommonOptionFactMappingRevision)
            .join(
                CurrentCommonOptionFactMappingMove,
                CurrentCommonOptionFactMappingMove.mapping_revision_id
                == CommonOptionFactMappingRevision.mapping_revision_id,
            )
            .where(CurrentCommonOptionFactMappingMove.product_group_id == product_group_id)
            .order_by(CurrentCommonOptionFactMappingMove.sequence.desc())
        ).first()

    @staticmethod
    def _current_sequence(session: Session, product_group_id: str) -> int:
        return int(
            session.scalar(
                select(func.max(CurrentCommonOptionFactMappingMove.sequence)).where(
                    CurrentCommonOptionFactMappingMove.product_group_id == product_group_id
                )
            )
            or 0
        )

    @staticmethod
    def _pointer(
        row: CommonOptionFactAxisMapping | CommonOptionFactValueMapping,
    ) -> FactPointerRecord:
        return FactPointerRecord(
            row.source_revision_id,
            row.source_field_key,
            row.source_json_path,
            row.source_value_json,
            row.source_field_fingerprint,
        )

    @classmethod
    def _record(
        cls, session: Session, revision: CommonOptionFactMappingRevision
    ) -> CommonOptionFactMappingRecord:
        axes: list[AxisFactMappingRecord] = []
        for axis in session.scalars(
            select(CommonOptionFactAxisMapping)
            .where(CommonOptionFactAxisMapping.mapping_revision_id == revision.mapping_revision_id)
            .order_by(CommonOptionFactAxisMapping.ordinal)
        ):
            values = tuple(
                ValueFactMappingRecord(
                    value.value_mapping_id,
                    value.common_option_value_id,
                    value.ordinal,
                    cls._pointer(value),
                )
                for value in session.scalars(
                    select(CommonOptionFactValueMapping)
                    .where(CommonOptionFactValueMapping.axis_mapping_id == axis.axis_mapping_id)
                    .order_by(CommonOptionFactValueMapping.ordinal)
                )
            )
            axes.append(
                AxisFactMappingRecord(
                    axis.axis_mapping_id,
                    axis.common_option_axis_id,
                    axis.ordinal,
                    cls._pointer(axis),
                    values,
                )
            )
        return CommonOptionFactMappingRecord(
            revision.mapping_revision_id,
            revision.product_group_id,
            revision.common_option_revision_id,
            revision.revision_no,
            revision.mapping_signature,
            revision.signature_version,
            revision.evidence_reference,
            revision.reason,
            revision.reviewed_by,
            revision.correlation_id,
            revision.reviewed_at,
            tuple(axes),
        )


__all__ = [
    "AxisFactMappingRecord",
    "CommonOptionFactMappingRecord",
    "CommonOptionFactMappingStore",
    "FactPointerRecord",
    "ValueFactMappingRecord",
]
