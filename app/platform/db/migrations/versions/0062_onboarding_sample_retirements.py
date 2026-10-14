"""ADR-0036 O5b: retiring an onboarding sample (owner amendment 6160586049).

Revision ID: 0062_onboarding_sample_retirements
Revises: 0061_supplier_sales_policy
Create Date: 2026-10-14

ADR-0036 §3, §7 as amended on 2026-10-14 (Issue #219 ``6160586049``). Two changes, nothing else.

- **``onboarding_sample_retirements``.** One row per retired sample of an onboarded supplier: the
  sample URL key, the operator's stated reason, who retired it and when. A sample is retired at
  most once, and only a sample a trial of its own supplier read can be retired. The database
  refuses an update or a delete: a retirement is never undone or erased.
- **The ACTIVE gate** (``trg_onboarding_transitions_active_is_proven`` of 0060) is replaced by the
  same gate that also leaves retired samples out: a retired sample neither proves the site nor
  keeps a structural problem open. Every other condition — 3 distinct samples under the current
  words and the ACTIVE row's template revision, every core field read, a ``CORRECT`` and no
  ``WRONG`` of the shown fingerprint, no open structural problem — is exactly 0060's.

**Downgrade fails closed** while any retirement exists: an operator's decision is never silently
dropped. Without one it restores 0060's gate and drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062_onboarding_sample_retirements"
down_revision: str | None = "0061_supplier_sales_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIREMENTS = "onboarding_sample_retirements"
SUPPLIERS = "onboarded_suppliers"
TRANSITIONS = "onboarding_transitions"
TRIALS = "onboarding_trials"
WORDS = "onboarding_word_revisions"
VERIFICATIONS = "onboarding_verifications"
GATE = f"trg_{TRANSITIONS}_active_is_proven"

# Frozen with 0060 (``app.stages.collect.onboarding.store``): the gate's own terms.
CORE_FIELDS = ("original_name", "prices", "options", "images", "stock")
READ_STATUSES = {
    field: "'CONFIRMED', 'ABSENT'" if field == "options" else "'CONFIRMED'" for field in CORE_FIELDS
}
ACTIVATION_SAMPLES = 3
_CURRENT_WORDS = (
    f"(SELECT COALESCE(MAX(w.seq), 0) FROM {WORDS} w WHERE w.supplier_key = NEW.supplier_key)"
)
_CORE_READ = " AND ".join(
    f"json_extract(t.field_status_json, '$.{field}') IN ({READ_STATUSES[field]})"
    for field in CORE_FIELDS
)


def _unproven(current_trial: str) -> str:
    """0060's ACTIVE gate over the trials ``current_trial`` selects."""
    return (
        "NEW.to_state = 'ACTIVE' AND (NEW.template_revision IS NULL"
        f" OR (SELECT COUNT(DISTINCT t.sample_url_key) FROM {TRIALS} t"
        f" WHERE {current_trial}"
        f" AND json_array_length(t.structural_json) = 0 AND {_CORE_READ}"
        f" AND EXISTS (SELECT 1 FROM {VERIFICATIONS} v WHERE v.trial_id = t.trial_id"
        " AND v.verdict = 'CORRECT' AND v.shown_fingerprint = t.facts_fingerprint)"
        f" AND NOT EXISTS (SELECT 1 FROM {VERIFICATIONS} v WHERE v.trial_id = t.trial_id"
        " AND v.verdict = 'WRONG' AND v.shown_fingerprint = t.facts_fingerprint)"
        f") < {ACTIVATION_SAMPLES}"
        f" OR EXISTS (SELECT 1 FROM {TRIALS} t WHERE {current_trial}"
        " AND json_array_length(t.structural_json) > 0))"
    )


# 0060's trials of the proof: this supplier's, under the current words and the ACTIVE row's
# template revision.
_CURRENT_TRIAL = (
    f"t.supplier_key = NEW.supplier_key AND t.words_seq = {_CURRENT_WORDS}"
    " AND t.template_revision = NEW.template_revision"
)
# This revision's: the same, less every retired sample.
_LIVE_TRIAL = (
    f"{_CURRENT_TRIAL} AND t.sample_url_key NOT IN (SELECT r.sample_url_key FROM {RETIREMENTS} r"
    " WHERE r.supplier_key = NEW.supplier_key)"
)


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _gate(current_trial: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {GATE}")
    _trigger(
        TRANSITIONS,
        "active_is_proven",
        "INSERT",
        _raise(f"{TRANSITIONS}: ACTIVE is entered only when proven", _unproven(current_trial)),
    )


def upgrade() -> None:
    op.create_table(
        RETIREMENTS,
        sa.Column("retirement_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("sample_url_key", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("retired_by", sa.String(length=64), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 200", name=op.f(f"ck_{RETIREMENTS}_reason_stated")
        ),
        sa.CheckConstraint("retired_by <> ''", name=op.f(f"ck_{RETIREMENTS}_retired_by_present")),
        sa.CheckConstraint("sample_url_key <> ''", name=op.f(f"ck_{RETIREMENTS}_sample_present")),
        sa.ForeignKeyConstraint(
            ["supplier_key"],
            [f"{SUPPLIERS}.supplier_key"],
            name=op.f(f"fk_{RETIREMENTS}_supplier_key_{SUPPLIERS}"),
        ),
        sa.PrimaryKeyConstraint("retirement_id", name=op.f(f"pk_{RETIREMENTS}")),
        sa.UniqueConstraint(
            "supplier_key",
            "sample_url_key",
            name=op.f(f"uq_{RETIREMENTS}_supplier_key_sample_url_key"),
        ),
    )
    _trigger(
        RETIREMENTS,
        "sample_was_read",
        "INSERT",
        _raise(
            f"{RETIREMENTS}: only a sample a trial of its own supplier read is retired",
            f"NOT EXISTS (SELECT 1 FROM {TRIALS} t WHERE t.supplier_key = NEW.supplier_key"
            " AND t.sample_url_key = NEW.sample_url_key)",
        ),
    )
    _trigger(
        RETIREMENTS, "no_update", "UPDATE", _raise(f"a {RETIREMENTS} row is never updated", "1")
    )
    _trigger(
        RETIREMENTS, "no_delete", "DELETE", _raise(f"a {RETIREMENTS} row is never deleted", "1")
    )
    _gate(_LIVE_TRIAL)


def downgrade() -> None:
    held = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {RETIREMENTS}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the onboarding sample retirements: {held} rows are held;"
            " an operator's decision is never silently destroyed"
        )
    _gate(_CURRENT_TRIAL)
    op.drop_table(RETIREMENTS)
