"""Issue #25 Stage 1 (ADR-0008): widen the capability error-class CHECK to the aligned taxonomy.

Revision ID: 0005_error_class_taxonomy
Revises: 0004_m2_permission_attestations
Create Date: 2026-09-14

ADR-0008 adds CONFLICT, DUPLICATE, REVIEW_REQUIRED and FATAL to the runtime ``ErrorClass``. The
only column whose CHECK lists error classes is ``marketplace_capabilities.error_class``. Migration
0003 keeps its original seven-value literal and is never edited. SQLite cannot alter a CHECK in
place, so the table is rebuilt from an explicit definition: 0003's schema, with the aligned list.
The widening is additive, and every existing row is copied unchanged.

``marketplace_workflow_overlays`` holds a foreign key to this table, and the application enables
``PRAGMA foreign_keys`` on every connection, including the migration's. Dropping the old table
would therefore fail while overlay rows exist. Toggling the pragma is not reliable either, because
SQLite ignores it inside an open transaction. So the overlay rows are set aside and restored,
unchanged, in the same transaction, and the migration refuses to finish if
``PRAGMA foreign_key_check`` reports anything.

Downgrade narrows the CHECK back to the seven classes. It refuses while any row holds one of the
four added classes, because a stored value is never rewritten (ADR-0008 D).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_error_class_taxonomy"
down_revision: str | None = "0004_m2_permission_attestations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPABILITIES = "marketplace_capabilities"
OVERLAYS = "marketplace_workflow_overlays"
OVERLAY_COLUMNS = (
    "marketplace_key",
    "workflow_scope",
    "workflow_state",
    "reason_code",
    "session_generation",
    "entered_at",
)

# Frozen with this revision: the v1 set that 0003 allowed, and the ADR-0008 aligned taxonomy.
V1_ERROR_CLASSES = (
    "TRANSIENT",
    "RATE_LIMITED",
    "AUTH",
    "VALIDATION",
    "POLICY_BLOCKED",
    "NOT_FOUND",
    "UNKNOWN",
)
ALIGNED_ERROR_CLASSES = (
    "TRANSIENT",
    "RATE_LIMITED",
    "AUTH",
    "VALIDATION",
    "POLICY_BLOCKED",
    "NOT_FOUND",
    "CONFLICT",
    "DUPLICATE",
    "REVIEW_REQUIRED",
    "FATAL",
    "UNKNOWN",
)
ADDED_ERROR_CLASSES = tuple(c for c in ALIGNED_ERROR_CLASSES if c not in V1_ERROR_CLASSES)


def _sql_list(values: Sequence[str]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _capabilities(error_classes: Sequence[str]) -> sa.Table:
    """``marketplace_capabilities`` exactly as 0003 created it, except for the class list."""
    return sa.Table(
        CAPABILITIES,
        sa.MetaData(),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("auth", sa.String(length=20), nullable=False),
        sa.Column("auth_verified_at", sa.DateTime(), nullable=True),
        sa.Column("write_scope_status", sa.String(length=20), nullable=False),
        sa.Column("evidence_strength", sa.String(length=20), nullable=True),
        sa.Column("write_status", sa.String(length=20), nullable=False),
        sa.Column("contract_freshness", sa.String(length=20), nullable=False),
        sa.Column("freshness_recorded_at", sa.DateTime(), nullable=True),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("remote_outcome", sa.String(length=20), nullable=True),
        sa.Column("session_generation_floor", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "auth IN ('READY', 'NOT_READY', 'AUTH_MISMATCH', 'NOT_BOUND')",
            name="ck_marketplace_capabilities_auth_valid",
        ),
        sa.CheckConstraint(
            "auth <> 'READY' OR auth_verified_at IS NOT NULL",
            name="ck_marketplace_capabilities_ready_is_proven",
        ),
        sa.CheckConstraint(
            "write_scope_status IN ('READY', 'MISSING', 'UNKNOWN')",
            name="ck_marketplace_capabilities_write_scope_valid",
        ),
        sa.CheckConstraint(
            "evidence_strength IS NULL"
            " OR evidence_strength IN ('OPERATOR_ATTESTED', 'MACHINE_VERIFIED')",
            name="ck_marketplace_capabilities_strength_valid",
        ),
        sa.CheckConstraint(
            "(write_scope_status = 'UNKNOWN') = (evidence_strength IS NULL)",
            name="ck_marketplace_capabilities_strength_matches_scope",
        ),
        sa.CheckConstraint(
            "write_status IN ('UNVERIFIED', 'BLOCKED')",
            name="ck_marketplace_capabilities_m2_write_unproven",
        ),
        sa.CheckConstraint(
            "write_scope_status <> 'MISSING' OR write_status = 'BLOCKED'",
            name="ck_marketplace_capabilities_missing_scope_blocks_write",
        ),
        sa.CheckConstraint(
            "contract_freshness IN ('UNRECORDED', 'CURRENT', 'STALE', 'REVIEW_REQUIRED')",
            name="ck_marketplace_capabilities_freshness_valid",
        ),
        sa.CheckConstraint(
            "(contract_freshness = 'UNRECORDED') = (freshness_recorded_at IS NULL)",
            name="ck_marketplace_capabilities_freshness_recorded",
        ),
        sa.CheckConstraint(
            f"error_class IS NULL OR error_class IN {_sql_list(error_classes)}",
            name="ck_marketplace_capabilities_error_class_valid",
        ),
        sa.CheckConstraint(
            "remote_outcome IS NULL"
            " OR remote_outcome IN ('APPLIED_PROVEN', 'NOT_APPLIED_PROVEN', 'UNKNOWN')",
            name="ck_marketplace_capabilities_remote_outcome_valid",
        ),
        sa.PrimaryKeyConstraint("marketplace_key", name="pk_marketplace_capabilities"),
    )


def _rebuild(error_classes: Sequence[str]) -> None:
    bind = op.get_bind()
    columns = ", ".join(OVERLAY_COLUMNS)
    saved = bind.execute(sa.text(f"SELECT {columns} FROM {OVERLAYS}")).fetchall()
    op.execute(f"DELETE FROM {OVERLAYS}")
    with op.batch_alter_table(
        CAPABILITIES, copy_from=_capabilities(error_classes), recreate="always"
    ):
        pass  # the new definition is the whole change
    if saved:
        marks = ", ".join(f":{column}" for column in OVERLAY_COLUMNS)
        bind.execute(
            sa.text(f"INSERT INTO {OVERLAYS} ({columns}) VALUES ({marks})"),
            [dict(zip(OVERLAY_COLUMNS, row, strict=True)) for row in saved],
        )
    problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"foreign key check failed after rebuilding {CAPABILITIES}: {problems}")


def upgrade() -> None:
    _rebuild(ALIGNED_ERROR_CLASSES)


def downgrade() -> None:
    held = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT error_class, COUNT(*) FROM {CAPABILITIES}"
                f" WHERE error_class IN {_sql_list(ADDED_ERROR_CLASSES)} GROUP BY error_class"
            )
        )
        .fetchall()
    )
    if held:
        raise RuntimeError(
            f"cannot narrow {CAPABILITIES}.error_class: rows hold "
            f"{ {str(row[0]): int(row[1]) for row in held} }; "
            "a stored value is never rewritten (ADR-0008 D)"
        )
    _rebuild(V1_ERROR_CLASSES)
