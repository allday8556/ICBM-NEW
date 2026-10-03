"""Reviewed, complete Product Fact correspondence for Common Sales Options."""

import contextlib
import sqlite3
import uuid

import pytest

from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.collect.facts import OptionAxis, OptionConfiguration, OptionsValue
from app.stages.products.common_option_mapping import (
    FACT_MAPPING_SIGNATURE_VERSION,
    CommonOptionAxisFactMappingSpec,
    CommonOptionValueFactMappingSpec,
    ProductFactPointer,
)
from app.stages.products.common_options import (
    CommonSalesOptionAxisSpec,
    CommonSalesOptionValueSpec,
)
from app.stages.products.model import MoveReason
from tests.support.collect_support import base_fields, collected, confirmed

pytestmark = pytest.mark.integration


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(database_path(config.data_dir))
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def _source_truth(container: Container, *, source_product_id: str = "1234") -> tuple[str, str]:
    fields = base_fields()
    fields["options"] = confirmed(
        OptionsValue(
            axes=(
                OptionAxis(name="개별 중량/용량", values=("300mg",)),
                OptionAxis(name="수량", values=("60정",)),
            ),
            configurations=(
                OptionConfiguration(selections=("300mg", "60정"), supplier_sku_id="sku-1"),
            ),
        ),
        ".options",
    )
    revision = container.revisions.append(
        collected(fields=fields, images=(), source_product_id=source_product_id)
    )
    source = container.product_store.source_product("kmretail", source_product_id)
    container.product_store.record_move(
        source.source_product_uid,
        revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="test",
        correlation_id="corr-source",
    )
    return source.source_product_uid, revision.revision_id


def _target(container: Container, source_uid: str):  # type: ignore[no-untyped-def]
    group = container.product_store.create_group(decided_by="test")
    container.product_store.confirm_new_member(
        group,
        source_uid,
        reason="reviewed member",
        decided_by="owner",
        correlation_id="corr-member",
    )
    options = container.common_sales_options.record_reviewed_revision(
        group,
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (CommonSalesOptionValueSpec("300", "300mg", "mg"),),
            ),
            CommonSalesOptionAxisSpec(
                "quantity",
                "수량",
                (CommonSalesOptionValueSpec("60", "60정", "tablet"),),
            ),
        ),
        reason="owner reviewed",
        decided_by="owner",
        correlation_id="corr-option",
    )
    return group, options


def _mapping(options, revision_id: str):  # type: ignore[no-untyped-def]
    return tuple(
        CommonOptionAxisFactMappingSpec(
            axis.axis_id,
            ProductFactPointer(revision_id, "options", f"$.axes[{axis.ordinal}].name"),
            tuple(
                CommonOptionValueFactMappingSpec(
                    value.value_id,
                    ProductFactPointer(
                        revision_id,
                        "options",
                        f"$.axes[{axis.ordinal}].values[{value.ordinal}]",
                    ),
                )
                for value in axis.values
            ),
        )
        for axis in options.axes
    )


def test_records_complete_exact_mapping_and_is_idempotent(container: Container) -> None:
    source_uid, source_revision = _source_truth(container)
    group, options = _target(container, source_uid)
    mapping = _mapping(options, source_revision)

    recorded = container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        options.revision_id,
        mapping,
        evidence_reference="owner review 2026-10-03",
        reason="facts prove common options",
        reviewed_by="owner",
        correlation_id="corr-map-1",
    )
    same = container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        options.revision_id,
        mapping,
        evidence_reference="repeat",
        reason="repeat",
        reviewed_by="reviewer",
        correlation_id="corr-map-2",
    )

    assert same == recorded
    assert recorded.signature_version == FACT_MAPPING_SIGNATURE_VERSION
    assert [axis.source.source_value_json for axis in recorded.axes] == [
        '"개별 중량/용량"',
        '"수량"',
    ]
    assert [axis.values[0].source.source_value_json for axis in recorded.axes] == [
        '"300mg"',
        '"60정"',
    ]
    assert container.common_option_fact_mappings.current(group) == recorded


def test_rejects_incomplete_mapping_and_a_noncurrent_fact_revision(
    container: Container,
) -> None:
    source_uid, source_revision = _source_truth(container)
    group, options = _target(container, source_uid)
    with pytest.raises(InputValidationError, match="every Common Sales Option axis"):
        container.common_option_fact_mappings.record_reviewed_mapping(
            group,
            options.revision_id,
            _mapping(options, source_revision)[:1],
            evidence_reference="review",
            reason="incomplete",
            reviewed_by="owner",
            correlation_id="corr-map",
        )

    fields = base_fields()
    newer = container.revisions.append(collected(fields=fields, images=()))
    container.product_store.record_move(
        source_uid,
        newer.revision_id,
        reason=MoveReason.EXPLICIT_DECISION,
        decided_by="owner",
        correlation_id="corr-move",
    )
    with pytest.raises(InputValidationError, match="must be current"):
        container.common_option_fact_mappings.record_reviewed_mapping(
            group,
            options.revision_id,
            _mapping(options, source_revision),
            evidence_reference="review",
            reason="stale",
            reviewed_by="owner",
            correlation_id="corr-map",
        )


def test_mapping_rows_are_append_only_and_incomplete_raw_mapping_cannot_be_current(
    config: AppConfig, container: Container
) -> None:
    source_uid, source_revision = _source_truth(container)
    group, options = _target(container, source_uid)
    recorded = container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        options.revision_id,
        _mapping(options, source_revision),
        evidence_reference="review",
        reason="complete",
        reviewed_by="owner",
        correlation_id="corr-map",
    )
    with contextlib.closing(_raw(config)) as raw:
        for statement in (
            "UPDATE common_option_fact_mapping_revisions SET reason = 'other'",
            "DELETE FROM common_option_fact_axis_mappings",
            "UPDATE common_option_fact_value_mappings SET ordinal = 9",
            "DELETE FROM current_common_option_fact_mapping_moves",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)

        raw.execute(
            "INSERT INTO common_option_fact_mapping_revisions VALUES"
            " (?, ?, ?, 2, ?, ?, 2, 2, 'raw', 'raw', 'test', 'corr',"
            " '2026-10-03 00:00:00')",
            (
                str(uuid.uuid4()),
                group,
                options.revision_id,
                "0" * 64,
                FACT_MAPPING_SIGNATURE_VERSION,
            ),
        )
        raw_revision = raw.execute(
            "SELECT mapping_revision_id FROM common_option_fact_mapping_revisions"
            " WHERE revision_no = 2"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="incomplete axes"):
            raw.execute(
                "INSERT INTO current_common_option_fact_mapping_moves VALUES"
                " (?, ?, ?, 2, ?, 'raw', 'test', 'corr', '2026-10-03 00:00:00')",
                (
                    str(uuid.uuid4()),
                    group,
                    raw_revision,
                    recorded.mapping_revision_id,
                ),
            )
