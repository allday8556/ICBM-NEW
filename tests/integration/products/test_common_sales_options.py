"""The durable C1/C2 Common Sales Option owner; no mapping, SKU or marketplace call."""

import contextlib
import sqlite3
import uuid

import pytest
from sqlalchemy import text

from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import NotFoundError
from app.stages.products.common_options import (
    COMMON_OPTION_SIGNATURE_VERSION,
    CommonSalesOptionAxisSpec,
    CommonSalesOptionValueSpec,
)

pytestmark = pytest.mark.integration

AT = "'2026-10-03 00:00:00'"


def _value(value: str, display: str, unit: str | None = None) -> CommonSalesOptionValueSpec:
    return CommonSalesOptionValueSpec(value, display, unit)


def _axes(*, extra_weight: bool = False) -> tuple[CommonSalesOptionAxisSpec, ...]:
    weights = [_value("300", "300mg", "mg")]
    if extra_weight:
        weights.append(_value("500", "500mg", "mg"))
    return (
        CommonSalesOptionAxisSpec("per_unit_weight", "개별 중량/용량", tuple(weights)),
        CommonSalesOptionAxisSpec("quantity", "수량", (_value("60", "60정", "tablet"),)),
    )


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(database_path(config.data_dir))
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def test_records_one_provider_neutral_revision_and_reads_it_back(container: Container) -> None:
    group = container.product_store.create_group(decided_by="test")
    recorded = container.common_sales_options.record_reviewed_revision(
        group, _axes(), reason="owner reviewed", decided_by="owner", correlation_id="corr-1"
    )
    assert recorded.product_group_id == group
    assert recorded.revision_no == 1
    assert recorded.signature_version == COMMON_OPTION_SIGNATURE_VERSION
    assert [(axis.semantic_key, axis.ordinal) for axis in recorded.axes] == [
        ("per_unit_weight", 0),
        ("quantity", 1),
    ]
    assert recorded.axes[0].values[0].unit_code == "mg"
    assert recorded.axes[1].values[0].display_value == "60정"
    assert container.common_sales_options.current(group) == recorded
    assert container.common_sales_options.revision(recorded.revision_id) == recorded


def test_same_structure_is_idempotent_and_a_changed_structure_appends(container: Container) -> None:
    group = container.product_store.create_group(decided_by="test")
    first = container.common_sales_options.record_reviewed_revision(
        group, _axes(), reason="first", decided_by="owner", correlation_id="corr-1"
    )
    same = container.common_sales_options.record_reviewed_revision(
        group, _axes(), reason="same", decided_by="other", correlation_id="corr-2"
    )
    second = container.common_sales_options.record_reviewed_revision(
        group,
        _axes(extra_weight=True),
        reason="reviewed addition",
        decided_by="owner",
        correlation_id="corr-3",
    )
    assert same == first
    assert second.revision_no == 2 and second.revision_id != first.revision_id
    assert container.common_sales_options.current(group) == second
    with container.db.read() as session:
        rows = session.execute(
            text(
                "SELECT sequence, revision_id, previous_revision_id"
                " FROM current_common_sales_option_revision_moves ORDER BY sequence"
            )
        ).all()
    assert rows == [(1, first.revision_id, None), (2, second.revision_id, first.revision_id)]


def test_unknown_product_and_invalid_provenance_fail_before_writing(container: Container) -> None:
    with pytest.raises(NotFoundError, match="no product"):
        container.common_sales_options.record_reviewed_revision(
            str(uuid.uuid4()),
            _axes(),
            reason="reviewed",
            decided_by="owner",
            correlation_id="corr",
        )
    group = container.product_store.create_group(decided_by="test")
    with pytest.raises(ValueError, match="reason"):
        container.common_sales_options.record_reviewed_revision(
            group, _axes(), reason=" ", decided_by="owner", correlation_id="corr"
        )
    assert container.common_sales_options.current(group) is None


def test_current_revision_and_children_are_append_only(
    config: AppConfig, container: Container
) -> None:
    group = container.product_store.create_group(decided_by="test")
    revision = container.common_sales_options.record_reviewed_revision(
        group, _axes(), reason="reviewed", decided_by="owner", correlation_id="corr"
    )
    axis = revision.axes[0]
    with contextlib.closing(_raw(config)) as raw:
        for statement in (
            "UPDATE common_sales_option_revisions SET reason = 'other'",
            "DELETE FROM common_sales_option_axes",
            "UPDATE common_sales_option_values SET display_value = 'other'",
            "DELETE FROM current_common_sales_option_revision_moves",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute(
                "INSERT INTO common_sales_option_values VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), axis.axis_id, "700", "mg", "700mg", 99),
            )


def test_incomplete_revision_cannot_become_current(config: AppConfig, container: Container) -> None:
    group = container.product_store.create_group(decided_by="test")
    revision = str(uuid.uuid4())
    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            "INSERT INTO common_sales_option_revisions VALUES (?, ?, 1, ?, ?, 2, 2,"
            " 'raw test', 'test', 'corr', " + AT + ")",
            (revision, group, "0" * 64, COMMON_OPTION_SIGNATURE_VERSION),
        )
        raw.execute(
            "INSERT INTO common_sales_option_axes VALUES (?, ?, 'quantity', '수량', 0)",
            (str(uuid.uuid4()), revision),
        )
        with pytest.raises(sqlite3.IntegrityError, match="incomplete axes"):
            raw.execute(
                "INSERT INTO current_common_sales_option_revision_moves VALUES"
                " (?, ?, ?, 1, NULL, 'raw', 'test', 'corr', " + AT + ")",
                (str(uuid.uuid4()), group, revision),
            )
