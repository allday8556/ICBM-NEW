"""Gate 3 area 1: the LIVE grant, the protected-write brake and the ASSET upload-attempt owner.

Revision ID: 0026_g3_live_authority
Revises: 0025_adaptive_shadow_foundation
Create Date: 2026-09-25

Issue #89 Gate 3, area 1 of ADR-0018 §12, under ADR-0018 §3, §3.4 and §4. It creates three tables
and their triggers, and touches no other table, row, trigger or index.

**What the database enforces.**
- ``live_grants``: one stage and one exact unit per grant (the other stage's binding is empty), a
  finite window and a budget of at least 1 (exactly 1 for CREATE), ``budget_used`` never above
  ``budget_max``, and a complete end record exactly when the state is terminal. Triggers refuse a
  delete, any change to a terminal grant, any change to its binding, window, approval or budget
  bound, and a budget step other than +1.
- ``protected_write_brakes``: at most the one ``GLOBAL`` row; a release names a GitHub comment
  identity; each change moves the generation by exactly one; no delete.
- ``asset_upload_attempts``: the partial unique index on ``replay_key`` over ``STARTED``,
  ``UPLOAD_UNKNOWN`` and ``APPLIED_PROVEN`` is the replay fence — one fencing attempt per key, ever.
  Only ``APPLIED_PROVEN`` carries a provider reference; the content digest is the artifact's own.
  Triggers refuse a delete, any change to a terminal attempt, a change that stays ``STARTED``, and
  any change to the key or provenance.

**Downgrade fails closed.** It refuses while any of the three tables holds a row: grant, brake and
upload-attempt history is never silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_g3_live_authority"
down_revision: str | None = "0025_adaptive_shadow_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTS = "live_grants"
BRAKES = "protected_write_brakes"
ATTEMPTS = "asset_upload_attempts"
ACCOUNTS = "marketplace_accounts"
CREATED = (GRANTS, BRAKES, ATTEMPTS)

_ASSET_BOUND = (
    "preparation_revision_id IS NOT NULL AND candidate_fingerprint IS NOT NULL"
    " AND artifact_set_json IS NOT NULL AND artifact_set_digest IS NOT NULL"
    " AND asset_profile IS NOT NULL AND asset_profile <> ''"
    " AND registration_snapshot_id IS NULL AND intent_id IS NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
)
_CREATE_BOUND = (
    "registration_snapshot_id IS NOT NULL AND intent_id IS NOT NULL"
    " AND idempotency_key IS NOT NULL AND idempotency_key <> ''"
    " AND create_attempt_no IS NOT NULL AND create_attempt_no >= 1"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)
_COMMENT_ID = "length({c}) BETWEEN 6 AND 20 AND {c} NOT GLOB '*[^0-9]*'"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _account(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["marketplace_key", "marketplace_account_id"],
        [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
        name=op.f(f"fk_{table}_marketplace_key_{ACCOUNTS}"),
    )


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _changed(columns: Sequence[str]) -> str:
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)


def upgrade() -> None:
    _create_grants()
    _create_brakes()
    _create_attempts()
    _install_triggers()


def _create_grants() -> None:
    op.create_table(
        GRANTS,
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("endpoint_group", sa.String(length=64), nullable=False),
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("artifact_set_json", sa.Text(), nullable=True),
        sa.Column("artifact_set_digest", sa.String(length=64), nullable=True),
        sa.Column("asset_profile", sa.String(length=64), nullable=True),
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("intent_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("create_attempt_no", sa.Integer(), nullable=True),
        sa.Column("budget_max", sa.Integer(), nullable=False),
        sa.Column("budget_used", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column("authorization_ref", sa.String(length=20), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("ended_by", sa.String(length=64), nullable=True),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        _check(GRANTS, "stage IN ('ASSET', 'CREATE')", "stage_valid"),
        _check(GRANTS, "state IN ('ACTIVE', 'EXPIRED', 'REVOKED', 'EXHAUSTED')", "state_valid"),
        _check(GRANTS, "marketplace_key <> ''", "marketplace_key_present"),
        _check(GRANTS, "endpoint_group <> ''", "endpoint_group_present"),
        _check(
            GRANTS,
            f"(stage = 'ASSET' AND {_ASSET_BOUND}) OR (stage = 'CREATE' AND {_CREATE_BOUND})",
            "stage_binding_exact",
        ),
        _check(
            GRANTS,
            "candidate_fingerprint IS NULL OR " + _hex64("candidate_fingerprint"),
            "candidate_fingerprint_hex",
        ),
        _check(
            GRANTS,
            "artifact_set_digest IS NULL OR " + _hex64("artifact_set_digest"),
            "artifact_set_digest_hex",
        ),
        _check(
            GRANTS,
            "artifact_set_json IS NULL OR (json_valid(artifact_set_json)"
            " AND json_type(artifact_set_json) = 'array'"
            " AND json_array_length(artifact_set_json) >= 1)",
            "artifact_set_is_array",
        ),
        _check(GRANTS, "budget_max >= 1", "budget_finite_and_positive"),
        _check(GRANTS, "stage <> 'CREATE' OR budget_max = 1", "create_budget_is_one"),
        _check(GRANTS, "budget_used >= 0 AND budget_used <= budget_max", "budget_bounded"),
        _check(GRANTS, "expires_at > not_before", "window_finite"),
        _check(GRANTS, "state <> 'EXHAUSTED' OR budget_used = budget_max", "exhausted_means_spent"),
        _check(
            GRANTS,
            "(state = 'ACTIVE') = (ended_at IS NULL)"
            " AND (ended_at IS NULL) = (ended_by IS NULL)"
            " AND (ended_at IS NULL) = (end_reason IS NULL)",
            "end_complete",
        ),
        _check(GRANTS, "approved_by <> ''", "approved_by_present"),
        _check(
            GRANTS,
            _COMMENT_ID.format(c="authorization_ref"),
            "authorization_ref_is_comment_id",
        ),
        _check(GRANTS, "correlation_id <> ''", "correlation_present"),
        _account(GRANTS),
        sa.PrimaryKeyConstraint("grant_id", name=op.f(f"pk_{GRANTS}")),
    )


def _create_brakes() -> None:
    op.create_table(
        BRAKES,
        sa.Column("brake_id", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("authorization_ref", sa.String(length=20), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check(BRAKES, "brake_id = 'GLOBAL'", "one_global_brake"),
        _check(BRAKES, "state IN ('ENGAGED', 'RELEASED')", "state_valid"),
        _check(BRAKES, "generation >= 1", "generation_positive"),
        _check(BRAKES, "changed_by <> ''", "changed_by_present"),
        _check(BRAKES, "reason_code <> ''", "reason_code_present"),
        _check(
            BRAKES,
            "state <> 'RELEASED' OR (authorization_ref IS NOT NULL"
            " AND length(authorization_ref) BETWEEN 6 AND 20"
            " AND authorization_ref NOT GLOB '*[^0-9]*')",
            "release_is_authorized",
        ),
        _check(BRAKES, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("brake_id", name=op.f(f"pk_{BRAKES}")),
    )


def _create_attempts() -> None:
    op.create_table(
        ATTEMPTS,
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("wire_method", sa.String(length=8), nullable=False),
        sa.Column("wire_host", sa.String(length=253), nullable=False),
        sa.Column("wire_path", sa.String(length=255), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("replay_key", sa.String(length=64), nullable=False),
        sa.Column("replay_key_version", sa.String(length=40), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("asset_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("derivation_id", sa.String(length=36), nullable=True),
        sa.Column("asset_profile", sa.String(length=64), nullable=False),
        sa.Column("endpoint_group", sa.String(length=64), nullable=False),
        sa.Column("contract_label", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("process_run_id", sa.String(length=36), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("provider_asset_ref", sa.Text(), nullable=True),
        sa.Column("outcome_reason", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        _check(
            ATTEMPTS,
            "state IN ('STARTED', 'APPLIED_PROVEN', 'NOT_APPLIED_PROVEN', 'UPLOAD_UNKNOWN')",
            "state_valid",
        ),
        _check(ATTEMPTS, _hex64("replay_key"), "replay_key_hex"),
        _check(ATTEMPTS, _hex64("content_sha256"), "content_sha256_hex"),
        _check(ATTEMPTS, _hex64("candidate_fingerprint"), "candidate_fingerprint_hex"),
        _check(ATTEMPTS, "content_sha256 = artifact_sha256", "content_is_the_artifact"),
        _check(ATTEMPTS, "wire_method IN ('POST', 'PUT', 'PATCH', 'DELETE')", "method_valid"),
        _check(ATTEMPTS, "wire_host <> '' AND wire_host = lower(wire_host)", "host_normalized"),
        _check(ATTEMPTS, "substr(wire_path, 1, 1) = '/'", "path_absolute"),
        _check(ATTEMPTS, "attempt_no >= 1", "attempt_no_positive"),
        _check(ATTEMPTS, "asset_kind IN ('SOURCE_ASSET', 'DERIVED_ARTIFACT')", "asset_kind_valid"),
        _check(ATTEMPTS, "(state = 'STARTED') = (finished_at IS NULL)", "started_until_terminal"),
        _check(
            ATTEMPTS,
            "(state = 'APPLIED_PROVEN') = (provider_asset_ref IS NOT NULL)",
            "only_applied_yields_an_asset",
        ),
        _check(
            ATTEMPTS,
            "state IN ('STARTED', 'APPLIED_PROVEN') OR outcome_reason IS NOT NULL",
            "unapplied_outcome_names_why",
        ),
        _check(
            ATTEMPTS,
            "evidence_json IS NULL OR (json_valid(evidence_json)"
            " AND json_type(evidence_json) = 'object')",
            "evidence_is_object",
        ),
        _check(ATTEMPTS, "correlation_id <> ''", "correlation_present"),
        _check(ATTEMPTS, "process_run_id <> ''", "process_run_present"),
        _account(ATTEMPTS),
        sa.ForeignKeyConstraint(
            ["grant_id"], [f"{GRANTS}.grant_id"], name=op.f(f"fk_{ATTEMPTS}_grant_id_{GRANTS}")
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f(f"pk_{ATTEMPTS}")),
        sa.UniqueConstraint(
            "replay_key", "attempt_no", name=op.f(f"uq_{ATTEMPTS}_replay_key_attempt_no")
        ),
    )
    op.create_index(
        "ix_asset_upload_attempts_replay_fence",
        ATTEMPTS,
        ["replay_key"],
        unique=True,
        sqlite_where=sa.text("state IN ('APPLIED_PROVEN', 'STARTED', 'UPLOAD_UNKNOWN')"),
    )
    op.create_index("ix_asset_upload_attempts_grant_id", ATTEMPTS, ["grant_id"])


_GRANT_FROZEN = (
    "stage",
    "marketplace_key",
    "marketplace_account_id",
    "endpoint_group",
    "preparation_revision_id",
    "candidate_fingerprint",
    "artifact_set_json",
    "artifact_set_digest",
    "asset_profile",
    "registration_snapshot_id",
    "intent_id",
    "idempotency_key",
    "create_attempt_no",
    "budget_max",
    "not_before",
    "expires_at",
    "approved_by",
    "authorization_ref",
    "correlation_id",
    "created_at",
)
_ATTEMPT_FROZEN = (
    "grant_id",
    "marketplace_key",
    "marketplace_account_id",
    "wire_method",
    "wire_host",
    "wire_path",
    "content_sha256",
    "replay_key",
    "replay_key_version",
    "attempt_no",
    "preparation_revision_id",
    "candidate_fingerprint",
    "asset_kind",
    "artifact_sha256",
    "derivation_id",
    "asset_profile",
    "endpoint_group",
    "contract_label",
    "file_name",
    "media_type",
    "process_run_id",
    "correlation_id",
    "started_at",
)


def _install_triggers() -> None:
    for table in CREATED:
        _trigger(table, "no_delete", "DELETE", _raise(f"{table} is append-only", "1"))
    _trigger(
        GRANTS,
        "forward_only",
        "UPDATE",
        _raise(f"{GRANTS}: a terminal grant never changes", "OLD.state <> 'ACTIVE'")
        + _raise(
            f"{GRANTS}: a grant binding, window, approval and budget bound never change",
            _changed(_GRANT_FROZEN) + " OR NEW.grant_id IS NOT OLD.grant_id",
        )
        + _raise(
            f"{GRANTS}: budget moves by one started attempt at a time",
            "NEW.budget_used NOT IN (OLD.budget_used, OLD.budget_used + 1)",
        ),
    )
    _trigger(
        BRAKES,
        "one_generation_per_change",
        "UPDATE",
        _raise(
            f"{BRAKES}: each change moves the generation by exactly one",
            "NEW.generation <> OLD.generation + 1 OR NEW.brake_id IS NOT OLD.brake_id",
        ),
    )
    _trigger(
        ATTEMPTS,
        "terminal_exactly_once",
        "UPDATE",
        _raise(
            f"{ATTEMPTS}: an attempt is terminalized exactly once",
            "OLD.state <> 'STARTED' OR NEW.state = 'STARTED'",
        )
        + _raise(
            f"{ATTEMPTS}: the replay key and provenance of an attempt never change",
            _changed(_ATTEMPT_FROZEN) + " OR NEW.attempt_id IS NOT OLD.attempt_id",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): LIVE grant, brake and upload-attempt history is"
                " never silently destroyed"
            )
    op.execute(f"DROP TRIGGER trg_{ATTEMPTS}_terminal_exactly_once")
    op.execute(f"DROP TRIGGER trg_{BRAKES}_one_generation_per_change")
    op.execute(f"DROP TRIGGER trg_{GRANTS}_forward_only")
    for table in CREATED:
        op.execute(f"DROP TRIGGER trg_{table}_no_delete")
    op.drop_index("ix_asset_upload_attempts_grant_id", table_name=ATTEMPTS)
    op.drop_index("ix_asset_upload_attempts_replay_fence", table_name=ATTEMPTS)
    for table in reversed(CREATED):
        op.drop_table(table)
