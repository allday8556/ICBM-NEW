"""Persistence owner for source-proven Atomic SKU sets."""

import re
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
from app.stages.products.atomic_sku import (
    ATOMIC_SKU_SIGNATURE_VERSION,
    AtomicSKUConfigurationSpec,
    atomic_sku_selection_signature,
    atomic_sku_set_signature,
    canonical_json_text,
    source_configuration,
    source_selection,
    validate_configuration_spec,
)
from app.stages.products.atomic_sku_models import (
    AtomicSKU,
    AtomicSKURevisionMember,
    AtomicSKURevisionSelectionEvidence,
    AtomicSKUSelection,
    AtomicSKUSetRevision,
    CurrentAtomicSKUSetMove,
)
from app.stages.products.common_option_mapping_models import (
    CommonOptionFactAxisMapping,
    CommonOptionFactMappingRevision,
    CommonOptionFactValueMapping,
    CurrentCommonOptionFactMappingMove,
)
from app.stages.products.common_option_models import (
    CommonSalesOptionAxis,
    CommonSalesOptionValue,
    CurrentCommonSalesOptionRevisionMove,
)
from app.stages.products.store import current_revision_for_confirmed_member

_SELECTION_SUFFIX = re.compile(r"\.selections\[([0-9]+)\]$")


@dataclass(frozen=True)
class AtomicSKUSelectionRecord:
    selection_id: str
    axis_id: str
    value_id: str
    semantic_key: str
    canonical_value: str
    unit_code: str | None
    source_json_path: str
    source_value_json: str
    ordinal: int


@dataclass(frozen=True)
class AtomicSKURecord:
    atomic_sku_id: str
    revision_member_id: str
    selection_signature: str
    source_revision_id: str
    source_field_key: str
    source_configuration_path: str
    source_configuration_json: str
    source_field_fingerprint: str
    supplier_sku_id: str | None
    ordinal: int
    selections: tuple[AtomicSKUSelectionRecord, ...]


@dataclass(frozen=True)
class AtomicSKUSetRecord:
    sku_set_revision_id: str
    product_group_id: str
    common_option_revision_id: str
    fact_mapping_revision_id: str
    revision_no: int
    set_signature: str
    signature_version: str
    reason: str
    decided_by: str
    correlation_id: str
    created_at: datetime
    atomic_skus: tuple[AtomicSKURecord, ...]


@dataclass(frozen=True)
class _ResolvedSelection:
    axis: CommonSalesOptionAxis
    value: CommonSalesOptionValue
    source_json_path: str
    source_value_json: str


@dataclass(frozen=True)
class _ResolvedSKU:
    source_revision_id: str
    source_field_key: str
    source_configuration_path: str
    source_configuration_json: str
    source_field_fingerprint: str
    supplier_sku_id: str | None
    selection_signature: str
    selections: tuple[_ResolvedSelection, ...]


class AtomicSKUStore:
    """Materializes only explicitly observed source configurations, never a Cartesian product."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def record_source_proven_set(
        self,
        product_group_id: str,
        configurations: tuple[AtomicSKUConfigurationSpec, ...],
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> AtomicSKUSetRecord:
        reason = self._required("reason", reason, 200)
        decided_by = self._required("decided_by", decided_by, 64)
        correlation_id = self._required("correlation_id", correlation_id, 64)
        if not configurations:
            raise InputValidationError(
                "ATOMIC_SKU_SOURCE_CONFIG_REQUIRED",
                "at least one explicit source configuration is required",
            )
        with self._db.write() as session:
            mapping = self._current_mapping(session, product_group_id)
            if mapping is None:
                raise InputValidationError(
                    "ATOMIC_SKU_FACT_MAPPING_REQUIRED",
                    "a current reviewed Product Fact mapping is required",
                )
            if (
                self._current_common_option_revision(session, product_group_id)
                != mapping.common_option_revision_id
            ):
                raise InputValidationError(
                    "ATOMIC_SKU_COMMON_OPTIONS_STALE",
                    "the reviewed fact mapping does not target the current Common Sales Options",
                )
            axes = tuple(
                session.scalars(
                    select(CommonSalesOptionAxis)
                    .where(CommonSalesOptionAxis.revision_id == mapping.common_option_revision_id)
                    .order_by(CommonSalesOptionAxis.ordinal)
                )
            )
            values_by_axis = {
                axis.axis_id: tuple(
                    session.scalars(
                        select(CommonSalesOptionValue)
                        .where(CommonSalesOptionValue.axis_id == axis.axis_id)
                        .order_by(CommonSalesOptionValue.ordinal)
                    )
                )
                for axis in axes
            }
            mapped_source_values = {
                row.common_option_value_id: row.source_value_json
                for row in session.scalars(
                    select(CommonOptionFactValueMapping).where(
                        CommonOptionFactValueMapping.mapping_revision_id
                        == mapping.mapping_revision_id
                    )
                )
            }
            resolved = tuple(
                self._resolve_configuration(
                    session,
                    product_group_id,
                    spec,
                    axes,
                    values_by_axis,
                    mapped_source_values,
                )
                for spec in configurations
            )
            signatures = [sku.selection_signature for sku in resolved]
            if len(set(signatures)) != len(signatures):
                raise InputValidationError(
                    "ATOMIC_SKU_DUPLICATE_SELECTION",
                    "one Atomic SKU selection can have only one proving source configuration",
                )
            payload: list[dict[str, object]] = [
                {
                    "selection_signature": sku.selection_signature,
                    "source_revision_id": sku.source_revision_id,
                    "source_field_key": sku.source_field_key,
                    "source_configuration_path": sku.source_configuration_path,
                    "source_configuration_json": sku.source_configuration_json,
                    "source_field_fingerprint": sku.source_field_fingerprint,
                    "selections": [
                        {
                            "semantic_key": item.axis.semantic_key,
                            "canonical_value": item.value.canonical_value,
                            "unit_code": item.value.unit_code,
                            "source_json_path": item.source_json_path,
                            "source_value_json": item.source_value_json,
                        }
                        for item in sku.selections
                    ],
                }
                for sku in resolved
            ]
            signature = atomic_sku_set_signature(payload)
            current = self._current_row(session, product_group_id)
            if (
                current is not None
                and current.fact_mapping_revision_id == mapping.mapping_revision_id
                and current.set_signature == signature
            ):
                return self._record(session, current)

            revision_no = (
                int(
                    session.scalar(
                        select(func.max(AtomicSKUSetRevision.revision_no)).where(
                            AtomicSKUSetRevision.product_group_id == product_group_id
                        )
                    )
                    or 0
                )
                + 1
            )
            revision = AtomicSKUSetRevision(
                sku_set_revision_id=str(uuid.uuid4()),
                product_group_id=product_group_id,
                common_option_revision_id=mapping.common_option_revision_id,
                fact_mapping_revision_id=mapping.mapping_revision_id,
                revision_no=revision_no,
                set_signature=signature,
                signature_version=ATOMIC_SKU_SIGNATURE_VERSION,
                atomic_sku_count=len(resolved),
                selection_count=len(resolved) * len(axes),
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
            )
            session.add(revision)
            session.flush()
            for sku_ordinal, sku in enumerate(resolved):
                identity = session.scalars(
                    select(AtomicSKU).where(
                        AtomicSKU.product_group_id == product_group_id,
                        AtomicSKU.selection_signature == sku.selection_signature,
                    )
                ).first()
                if identity is None:
                    identity = AtomicSKU(
                        atomic_sku_id=str(uuid.uuid4()),
                        product_group_id=product_group_id,
                        selection_signature=sku.selection_signature,
                        created_at=self._clock.now(),
                    )
                    session.add(identity)
                    session.flush()
                    canonical_selections: list[AtomicSKUSelection] = []
                    for selection_ordinal, item in enumerate(sku.selections):
                        selection = AtomicSKUSelection(
                            selection_id=str(uuid.uuid4()),
                            atomic_sku_id=identity.atomic_sku_id,
                            semantic_key=item.axis.semantic_key,
                            canonical_value=item.value.canonical_value,
                            unit_code=item.value.unit_code,
                            ordinal=selection_ordinal,
                        )
                        session.add(selection)
                        canonical_selections.append(selection)
                    session.flush()
                else:
                    canonical_selections = list(
                        session.scalars(
                            select(AtomicSKUSelection)
                            .where(AtomicSKUSelection.atomic_sku_id == identity.atomic_sku_id)
                            .order_by(AtomicSKUSelection.ordinal)
                        )
                    )
                    expected = [
                        (item.axis.semantic_key, item.value.canonical_value, item.value.unit_code)
                        for item in sku.selections
                    ]
                    actual = [
                        (item.semantic_key, item.canonical_value, item.unit_code)
                        for item in canonical_selections
                    ]
                    if actual != expected:
                        raise InputValidationError(
                            "ATOMIC_SKU_IDENTITY_COLLISION",
                            "an existing Atomic SKU identity has different canonical selections",
                        )
                member = AtomicSKURevisionMember(
                    revision_member_id=str(uuid.uuid4()),
                    sku_set_revision_id=revision.sku_set_revision_id,
                    atomic_sku_id=identity.atomic_sku_id,
                    source_revision_id=sku.source_revision_id,
                    source_field_key=sku.source_field_key,
                    source_configuration_path=sku.source_configuration_path,
                    source_configuration_json=sku.source_configuration_json,
                    source_field_fingerprint=sku.source_field_fingerprint,
                    supplier_sku_id=sku.supplier_sku_id,
                    ordinal=sku_ordinal,
                )
                session.add(member)
                session.flush()
                canonical_by_semantics = {
                    (item.semantic_key, item.canonical_value, item.unit_code): item
                    for item in canonical_selections
                }
                for item in sku.selections:
                    canonical = canonical_by_semantics[
                        (item.axis.semantic_key, item.value.canonical_value, item.value.unit_code)
                    ]
                    session.add(
                        AtomicSKURevisionSelectionEvidence(
                            evidence_id=str(uuid.uuid4()),
                            revision_member_id=member.revision_member_id,
                            atomic_sku_selection_id=canonical.selection_id,
                            common_option_axis_id=item.axis.axis_id,
                            common_option_value_id=item.value.value_id,
                            source_revision_id=sku.source_revision_id,
                            source_field_key=sku.source_field_key,
                            source_json_path=item.source_json_path,
                            source_value_json=item.source_value_json,
                            source_field_fingerprint=sku.source_field_fingerprint,
                        )
                    )
            session.flush()
            previous = None if current is None else current.sku_set_revision_id
            sequence = (
                1 if current is None else self._current_sequence(session, product_group_id) + 1
            )
            session.add(
                CurrentAtomicSKUSetMove(
                    move_id=str(uuid.uuid4()),
                    product_group_id=product_group_id,
                    sku_set_revision_id=revision.sku_set_revision_id,
                    sequence=sequence,
                    previous_sku_set_revision_id=previous,
                    reason=reason,
                    decided_by=decided_by,
                    correlation_id=correlation_id,
                    moved_at=self._clock.now(),
                )
            )
            session.flush()
            return self._record(session, revision)

    def current(self, product_group_id: str) -> AtomicSKUSetRecord | None:
        """Return the historical latest set move, even when its proof is now stale."""
        with self._db.read() as session:
            row = self._current_row(session, product_group_id)
            return None if row is None else self._record(session, row)

    def current_for_use(self, product_group_id: str) -> AtomicSKUSetRecord | None:
        """Return the latest set only while every canonical and source dependency is current."""
        with self._db.read() as session:
            row = self.current_for_use_row(session, product_group_id)
            return None if row is None else self._record(session, row)

    @classmethod
    def is_current_member_for_use(
        cls, session: Session, product_group_id: str, atomic_sku_id: str
    ) -> bool:
        """Authoritative transaction-local guard for downstream identity owners."""
        row = cls.current_for_use_row(session, product_group_id)
        if row is None:
            return False
        return (
            session.scalar(
                select(func.count())
                .select_from(AtomicSKURevisionMember)
                .where(
                    AtomicSKURevisionMember.sku_set_revision_id == row.sku_set_revision_id,
                    AtomicSKURevisionMember.atomic_sku_id == atomic_sku_id,
                )
            )
            == 1
        )

    @classmethod
    def current_for_use_row(
        cls, session: Session, product_group_id: str
    ) -> AtomicSKUSetRevision | None:
        """Resolve current-use validity; callers must not infer it from the latest move alone."""
        row = cls._current_row(session, product_group_id)
        if row is None:
            return None
        current_common = cls._current_common_option_revision(session, product_group_id)
        mapping = cls._current_mapping(session, product_group_id)
        if (
            row.common_option_revision_id != current_common
            or mapping is None
            or row.fact_mapping_revision_id != mapping.mapping_revision_id
            or mapping.product_group_id != product_group_id
            or mapping.common_option_revision_id != row.common_option_revision_id
        ):
            return None
        members = tuple(
            session.scalars(
                select(AtomicSKURevisionMember).where(
                    AtomicSKURevisionMember.sku_set_revision_id == row.sku_set_revision_id
                )
            )
        )
        if not members:
            return None
        dependency_revision_ids = {member.source_revision_id for member in members}
        dependency_revision_ids.update(
            session.scalars(
                select(CommonOptionFactAxisMapping.source_revision_id).where(
                    CommonOptionFactAxisMapping.mapping_revision_id == mapping.mapping_revision_id
                )
            )
        )
        dependency_revision_ids.update(
            session.scalars(
                select(CommonOptionFactValueMapping.source_revision_id).where(
                    CommonOptionFactValueMapping.mapping_revision_id == mapping.mapping_revision_id
                )
            )
        )
        for source_revision_id in dependency_revision_ids:
            fact_revision = session.get(ProductFactsRevision, source_revision_id)
            if fact_revision is None:
                return None
            is_member, current_revision = current_revision_for_confirmed_member(
                session,
                product_group_id,
                fact_revision.supplier_key,
                fact_revision.source_product_id,
            )
            if not is_member or current_revision != source_revision_id:
                return None
        return row

    def revision(self, sku_set_revision_id: str) -> AtomicSKUSetRecord | None:
        with self._db.read() as session:
            row = session.get(AtomicSKUSetRevision, sku_set_revision_id)
            return None if row is None else self._record(session, row)

    @staticmethod
    def _resolve_configuration(
        session: Session,
        product_group_id: str,
        original: AtomicSKUConfigurationSpec,
        axes: tuple[CommonSalesOptionAxis, ...],
        values_by_axis: dict[str, tuple[CommonSalesOptionValue, ...]],
        mapped_source_values: dict[str, str],
    ) -> _ResolvedSKU:
        spec = validate_configuration_spec(original)
        result = session.execute(
            select(ProductFactsField, ProductFactsRevision)
            .join(
                ProductFactsRevision,
                ProductFactsRevision.revision_id == ProductFactsField.revision_id,
            )
            .where(
                ProductFactsField.revision_id == spec.source_revision_id,
                ProductFactsField.field_key == spec.source_field_key,
            )
        ).one_or_none()
        if result is None:
            raise NotFoundError(
                "ATOMIC_SKU_SOURCE_FACT_UNKNOWN", "the source configuration field does not exist"
            )
        field, fact_revision = result
        if field.status != FieldStatus.CONFIRMED.value or field.value_json is None:
            raise InputValidationError(
                "ATOMIC_SKU_SOURCE_FACT_UNCONFIRMED",
                "an Atomic SKU requires a CONFIRMED source configuration field",
            )
        is_member, current_revision = current_revision_for_confirmed_member(
            session,
            product_group_id,
            fact_revision.supplier_key,
            fact_revision.source_product_id,
        )
        if not is_member:
            raise InputValidationError(
                "ATOMIC_SKU_SOURCE_OUTSIDE_PRODUCT",
                "the source configuration must belong to a confirmed product member",
            )
        if current_revision != spec.source_revision_id:
            raise InputValidationError(
                "ATOMIC_SKU_SOURCE_NOT_CURRENT",
                "the source configuration revision must be current",
            )
        configuration = source_configuration(field.value_json, spec.source_configuration_path)
        if len(configuration.selections) != len(axes):
            raise InputValidationError(
                "ATOMIC_SKU_CONFIGURATION_AXIS_COUNT",
                "a source configuration must select exactly one value for every common axis",
            )
        supplied = {item.axis_id: item for item in spec.selections}
        if len(supplied) != len(spec.selections) or set(supplied) != {
            axis.axis_id for axis in axes
        }:
            raise InputValidationError(
                "ATOMIC_SKU_INCOMPLETE_SELECTIONS",
                "an Atomic SKU must name every Common Sales Option axis exactly once",
            )
        resolved: list[_ResolvedSelection] = []
        indexes: set[int] = set()
        for axis in axes:
            selection = supplied[axis.axis_id]
            target_values = {value.value_id: value for value in values_by_axis[axis.axis_id]}
            if selection.value_id not in target_values:
                raise InputValidationError(
                    "ATOMIC_SKU_VALUE_OUTSIDE_AXIS",
                    "an Atomic SKU value must belong to its named common axis",
                )
            prefix = f"{spec.source_configuration_path}.selections["
            match = _SELECTION_SUFFIX.search(selection.source_json_path)
            if not selection.source_json_path.startswith(prefix) or match is None:
                raise InputValidationError(
                    "ATOMIC_SKU_SELECTION_OUTSIDE_CONFIGURATION",
                    "every selection path must belong to the one source configuration",
                )
            index = int(match.group(1))
            if selection.source_json_path != (
                f"{spec.source_configuration_path}.selections[{index}]"
            ):
                raise InputValidationError(
                    "ATOMIC_SKU_SELECTION_OUTSIDE_CONFIGURATION",
                    "every selection path must directly belong to the one source configuration",
                )
            indexes.add(index)
            value = source_selection(field.value_json, selection.source_json_path)
            source_value_json = canonical_json_text(value)
            if mapped_source_values.get(selection.value_id) != source_value_json:
                raise InputValidationError(
                    "ATOMIC_SKU_SELECTION_NOT_MAPPED",
                    "a source selection must exactly match its reviewed common value mapping",
                )
            target = target_values[selection.value_id]
            resolved.append(
                _ResolvedSelection(axis, target, selection.source_json_path, source_value_json)
            )
        resolved.sort(
            key=lambda item: (
                item.axis.semantic_key,
                item.value.canonical_value,
                item.value.unit_code,
            )
        )
        semantic_selections = tuple(
            (item.axis.semantic_key, item.value.canonical_value, item.value.unit_code)
            for item in resolved
        )
        if indexes != set(range(len(axes))):
            raise InputValidationError(
                "ATOMIC_SKU_SELECTION_INDEX_COVERAGE",
                "one source selection index must prove each common axis",
            )
        return _ResolvedSKU(
            spec.source_revision_id,
            spec.source_field_key,
            spec.source_configuration_path,
            configuration.canonical_json,
            field.field_fingerprint,
            configuration.supplier_sku_id,
            atomic_sku_selection_signature(semantic_selections),
            tuple(resolved),
        )

    @staticmethod
    def _required(name: str, value: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return normalized

    @staticmethod
    def _current_mapping(
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
    def _current_common_option_revision(session: Session, product_group_id: str) -> str | None:
        return session.scalar(
            select(CurrentCommonSalesOptionRevisionMove.revision_id)
            .where(CurrentCommonSalesOptionRevisionMove.product_group_id == product_group_id)
            .order_by(CurrentCommonSalesOptionRevisionMove.sequence.desc())
        )

    @staticmethod
    def _current_row(session: Session, product_group_id: str) -> AtomicSKUSetRevision | None:
        return session.scalars(
            select(AtomicSKUSetRevision)
            .join(
                CurrentAtomicSKUSetMove,
                CurrentAtomicSKUSetMove.sku_set_revision_id
                == AtomicSKUSetRevision.sku_set_revision_id,
            )
            .where(CurrentAtomicSKUSetMove.product_group_id == product_group_id)
            .order_by(CurrentAtomicSKUSetMove.sequence.desc())
        ).first()

    @staticmethod
    def _current_sequence(session: Session, product_group_id: str) -> int:
        return int(
            session.scalar(
                select(func.max(CurrentAtomicSKUSetMove.sequence)).where(
                    CurrentAtomicSKUSetMove.product_group_id == product_group_id
                )
            )
            or 0
        )

    @classmethod
    def _record(cls, session: Session, revision: AtomicSKUSetRevision) -> AtomicSKUSetRecord:
        skus: list[AtomicSKURecord] = []
        for member, sku in session.execute(
            select(AtomicSKURevisionMember, AtomicSKU)
            .join(AtomicSKU, AtomicSKU.atomic_sku_id == AtomicSKURevisionMember.atomic_sku_id)
            .where(AtomicSKURevisionMember.sku_set_revision_id == revision.sku_set_revision_id)
            .order_by(AtomicSKURevisionMember.ordinal)
        ).tuples():
            selections = tuple(
                AtomicSKUSelectionRecord(
                    row.selection_id,
                    evidence.common_option_axis_id,
                    evidence.common_option_value_id,
                    row.semantic_key,
                    row.canonical_value,
                    row.unit_code or None,
                    evidence.source_json_path,
                    evidence.source_value_json,
                    row.ordinal,
                )
                for row, evidence in session.execute(
                    select(AtomicSKUSelection, AtomicSKURevisionSelectionEvidence)
                    .join(
                        AtomicSKURevisionSelectionEvidence,
                        AtomicSKURevisionSelectionEvidence.atomic_sku_selection_id
                        == AtomicSKUSelection.selection_id,
                    )
                    .where(
                        AtomicSKUSelection.atomic_sku_id == sku.atomic_sku_id,
                        AtomicSKURevisionSelectionEvidence.revision_member_id
                        == member.revision_member_id,
                    )
                    .order_by(AtomicSKUSelection.ordinal)
                ).tuples()
            )
            skus.append(
                AtomicSKURecord(
                    sku.atomic_sku_id,
                    member.revision_member_id,
                    sku.selection_signature,
                    member.source_revision_id,
                    member.source_field_key,
                    member.source_configuration_path,
                    member.source_configuration_json,
                    member.source_field_fingerprint,
                    member.supplier_sku_id,
                    member.ordinal,
                    selections,
                )
            )
        return AtomicSKUSetRecord(
            revision.sku_set_revision_id,
            revision.product_group_id,
            revision.common_option_revision_id,
            revision.fact_mapping_revision_id,
            revision.revision_no,
            revision.set_signature,
            revision.signature_version,
            revision.reason,
            revision.decided_by,
            revision.correlation_id,
            revision.created_at,
            tuple(skus),
        )


__all__ = ["AtomicSKURecord", "AtomicSKUSelectionRecord", "AtomicSKUSetRecord", "AtomicSKUStore"]
