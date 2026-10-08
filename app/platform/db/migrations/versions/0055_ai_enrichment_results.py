"""ADR-0026 AIF-3: the structured enrichment results, owned by PRODUCT DB.

Revision ID: 0055_ai_enrichment_results
Revises: 0054_ai_prompt_registry
Create Date: 2026-10-09

ADR-0026 §5 (owner decision Issue #219 `6057252039`). It adds one table and touches no existing
table, row, trigger or index.

**One append-only table.** `product_enrichment_results` holds one row per recorded result of one
task's result key for one canonical product and one optional target (`marketplace_key`,
`marketplace_account_id`). The rows of one subject are numbered from one by exactly one, and the
newest is the current result: there is no pointer to move. A row is never updated or deleted.

**What a row holds.** An `OK` row holds the value (a JSON object), its evidence, its confidence
(0..1) and whether it requires review. A `FAILED` row holds the classified error and no value.
Every row holds the input fingerprint computed before the call, the inputs it was computed from
(the relevant facts, the prompt and policy versions, the requested identity and the schema
version), and the provider's execution provenance. A value is never written into a source fact or a
product field (AIF-01).

**Every table starts empty** (M0 acceptance). **Downgrade fails closed** while any result exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_ai_enrichment_results"
down_revision: str | None = "0054_ai_prompt_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESULTS = "product_enrichment_results"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{RESULTS}_{name}"))


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{RESULTS}_{name} BEFORE {event} ON {RESULTS} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _json_object(column: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = 'object'"


def upgrade() -> None:
    op.create_table(
        RESULTS,
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("subject_key", sa.String(length=255), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("result_key", sa.String(length=64), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=True),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=False),
        sa.Column("enrichment_schema_version", sa.String(length=64), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check("sequence >= 1", "sequence_positive"),
        _check("status IN ('OK', 'FAILED')", "status_known"),
        _check(
            "length(input_fingerprint) = 64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'",
            "input_fingerprint_hex",
        ),
        _check(_json_object("inputs_json"), "inputs_is_object"),
        _check(_json_object("provenance_json"), "provenance_is_object"),
        _check("(marketplace_key IS NULL) = (marketplace_account_id IS NULL)", "target_is_whole"),
        _check("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", "confidence_range"),
        # An OK row is a value and no error; a FAILED row is a classified error and no value.
        _check(
            "(status = 'OK' AND value_json IS NOT NULL AND json_valid(value_json)"
            " AND json_type(value_json) = 'object' AND error_class IS NULL AND error_code IS NULL)"
            " OR (status = 'FAILED' AND value_json IS NULL AND evidence_json IS NULL"
            " AND confidence IS NULL AND requires_review IS NULL"
            " AND error_class IS NOT NULL AND error_code IS NOT NULL)",
            "status_shape",
        ),
        _check("correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["product_group_id"],
            ["product_groups.product_group_id"],
            name=op.f(f"fk_{RESULTS}_product_group_id_product_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            ["marketplace_accounts.marketplace_key", "marketplace_accounts.marketplace_account_id"],
            name=op.f(f"fk_{RESULTS}_marketplace_key_marketplace_accounts"),
        ),
        sa.PrimaryKeyConstraint("result_id", name=op.f(f"pk_{RESULTS}")),
        sa.UniqueConstraint(
            "subject_key", "sequence", name=op.f(f"uq_{RESULTS}_subject_key_sequence")
        ),
    )
    op.create_index(op.f(f"ix_{RESULTS}_product_group_id"), RESULTS, ["product_group_id"])
    # The subject key is exactly its parts, so two subjects never share a numbering.
    _trigger(
        "subject_is_its_parts",
        "INSERT",
        _raise(
            f"{RESULTS}: the subject key is its product, task, result key and target",
            "NEW.subject_key <> NEW.product_group_id || '|' || NEW.task_key || '|'"
            " || NEW.result_key || '|' || COALESCE(NEW.marketplace_key, '') || '|'"
            " || COALESCE(NEW.marketplace_account_id, '')",
        ),
    )
    _trigger(
        "sequence_follows",
        "INSERT",
        _raise(
            f"{RESULTS}: a result follows the one before it",
            f"NEW.sequence <> 1 + (SELECT COALESCE(MAX(sequence), 0) FROM {RESULTS} r"
            " WHERE r.subject_key = NEW.subject_key)",
        ),
    )
    _trigger("no_update", "UPDATE", _raise(f"a {RESULTS} row is never updated", "1"))
    _trigger("no_delete", "DELETE", _raise(f"a {RESULTS} row is never deleted", "1"))


def downgrade() -> None:
    held = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {RESULTS}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the enrichment results: {held} row(s) are held; a recorded result is"
            " never silently destroyed"
        )
    op.drop_index(op.f(f"ix_{RESULTS}_product_group_id"), table_name=RESULTS)
    op.drop_table(RESULTS)
