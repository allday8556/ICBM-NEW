"""M2 PR-C: SMARTSTORE-A0-PERMISSION operator-attested permission evidence.

Revision ID: 0004_m2_permission_attestations
Revises: 0003_m2_marketplace_capabilities
Create Date: 2026-09-14

Additive only. One append-only row per operator attestation (PERMISSIONS_SCOPES.md §5, §7): the
evidence envelope bound to a keyed application fingerprint and an endpoint-mapping revision.
Strength and source are pinned so an attestation can never be stored as MACHINE_VERIFIED
(CAPABILITY_MAPPING §17 #18). No client_id, secret or token is stored here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_m2_permission_attestations"
down_revision: str | None = "0003_m2_marketplace_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "marketplace_permission_attestations"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("evidence_source", sa.String(length=40), nullable=False),
        sa.Column("evidence_strength", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("application_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("required_groups", sa.String(length=200), nullable=False),
        sa.Column("observed_groups", sa.String(length=200), nullable=False),
        sa.Column("endpoint_mapping_revision", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=100), nullable=False),
        sa.CheckConstraint(
            "evidence_source = 'OPERATOR_ATTESTED_PROVIDER_ADMIN'",
            name=op.f("ck_marketplace_permission_attestations_source_is_provider_admin"),
        ),
        sa.CheckConstraint(
            "evidence_strength = 'OPERATOR_ATTESTED'",
            name=op.f("ck_marketplace_permission_attestations_a0_never_promoted"),
        ),
        sa.CheckConstraint(
            "application_fingerprint <> ''",
            name=op.f("ck_marketplace_permission_attestations_bound_to_application"),
        ),
        sa.CheckConstraint(
            "required_groups <> ''",
            name=op.f("ck_marketplace_permission_attestations_required_groups_present"),
        ),
        sa.CheckConstraint(
            "endpoint_mapping_revision <> ''",
            name=op.f("ck_marketplace_permission_attestations_bound_to_mapping_revision"),
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_marketplace_permission_attestations")),
    )
    op.create_index(
        "ix_marketplace_permission_attestations_marketplace_key", TABLE, ["marketplace_key"]
    )
    # Append-only is enforced by the database itself, as for audit_events.
    op.execute(
        f"CREATE TRIGGER trg_{TABLE}_no_update BEFORE UPDATE ON {TABLE} "
        f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{TABLE}_no_delete BEFORE DELETE ON {TABLE} "
        f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_update")
    op.drop_index("ix_marketplace_permission_attestations_marketplace_key", table_name=TABLE)
    op.drop_table(TABLE)
