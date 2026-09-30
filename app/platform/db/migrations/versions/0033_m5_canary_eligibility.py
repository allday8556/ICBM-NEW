"""M5 canary eligibility: the durable, append-only eligibility record of one exact canary lineage.

Revision ID: 0033_m5_canary_eligibility
Revises: 0032_m5_registration_authoring_revisions
Create Date: 2026-09-30

ADR-0018 §5.1, authorized concretely by the architect resolution on Issue #89 (comment
``5910018106``). It creates one table with its unique index and its triggers. It changes no
existing row, table, trigger, index or constraint, and it backfills nothing.

``canary_eligibility_records`` — one row per eligibility review of one exact lineage:
- the lineage is the account, the preparation revision, its candidate fingerprint, the taxonomy,
  the category, the category-metadata revision and the scope version, with the SHA-256 of the
  review packet the server built for it;
- ``seq`` is the previous maximum plus one of the exact scope ``marketplace_key ×
  marketplace_account_id × preparation_revision_id × candidate_fingerprint × scope_version``, so
  the current record of a scope is its highest ``seq`` and no mutable pointer exists;
- ``checks_json`` is the closed v1 checklist: exactly the five scope keys, each with a finding and
  either no evidence or an admitted evidence kind with a non-empty reference;
- ``verdict`` is ``PROVEN_OUTSIDE`` exactly when every key is ``OUTSIDE_SCOPE`` with evidence,
  and ``UNPROVEN`` otherwise;
- a row is never updated and never deleted.

It is evidence for the first-canary restriction only. It is never a ``COMPLIANCE PASS`` and it
adds no compliance meaning to the category metadata.

**Downgrade fails closed.** It refuses while any record exists: eligibility evidence is never
silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_m5_canary_eligibility"
down_revision: str | None = "0032_m5_registration_authoring_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORDS = "canary_eligibility_records"
ACCOUNTS = "marketplace_accounts"
REVISIONS = "registration_preparation_revisions"
UX_SCOPE = f"ux_{RECORDS}_scope_seq"
TRIGGERS: tuple[str, ...] = (
    f"trg_{RECORDS}_append",
    f"trg_{RECORDS}_closed_checklist",
    f"trg_{RECORDS}_no_update",
    f"trg_{RECORDS}_no_delete",
)
SCOPE_KEYS: tuple[str, ...] = (
    "HEALTH_FUNCTIONAL_FOOD",
    "KC_CERTIFICATION_REQUIRED",
    "MFDS_NOTICE_OR_APPROVAL",
    "PROHIBITED_OR_RESTRICTED_WORDING",
    "OTHER_REGULATED_OR_RESTRICTED_CATEGORY",
)
CHECK_MEMBERS: tuple[str, ...] = ("finding", "evidence_kind", "evidence_ref")


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{RECORDS}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _well_formed(key: str) -> str:
    path = f"json_extract(checks_json, '$.{key}"
    return (
        f"json_type(checks_json, '$.{key}') IS 'object'"
        f" AND {path}.finding') IS NOT NULL"
        f" AND {path}.finding') IN ('OUTSIDE_SCOPE', 'IN_SCOPE', 'UNKNOWN')"
        f" AND ({path}.evidence_kind') IS NULL) = ({path}.evidence_ref') IS NULL)"
        f" AND ({path}.evidence_kind') IS NULL OR ("
        f"{path}.evidence_kind') IN ('CATEGORY_METADATA', 'OFFICIAL_RULE', 'LISTING_REVIEW_PACKET')"
        f" AND json_type(checks_json, '$.{key}.evidence_ref') IS 'text'"
        f" AND length({path}.evidence_ref')) > 0))"
    )


def _excluded(key: str) -> str:
    path = f"json_extract(checks_json, '$.{key}"
    return f"({path}.finding') IS 'OUTSIDE_SCOPE' AND {path}.evidence_kind') IS NOT NULL)"


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {RECORDS} BEGIN {body} END")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        RECORDS,
        sa.Column("eligibility_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("taxonomy_revision", sa.String(length=64), nullable=False),
        sa.Column("category_id", sa.String(length=64), nullable=False),
        sa.Column("category_metadata_revision", sa.String(length=64), nullable=False),
        sa.Column("scope_version", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("review_packet_digest", sa.String(length=64), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check("verdict IN ('PROVEN_OUTSIDE', 'UNPROVEN')", "verdict_valid"),
        _check("seq >= 1", "seq_positive"),
        _check(_hex64("candidate_fingerprint"), "candidate_fingerprint_hex"),
        _check(_hex64("review_packet_digest"), "review_packet_digest_hex"),
        _check(
            "taxonomy_revision <> '' AND category_id <> '' AND category_metadata_revision <> ''"
            " AND scope_version <> '' AND recorded_by <> ''",
            "identity_present",
        ),
        _check("json_valid(checks_json) AND json_type(checks_json) = 'object'", "checks_is_object"),
        _check(" AND ".join(f"({_well_formed(key)})" for key in SCOPE_KEYS), "checks_well_formed"),
        _check(
            "(verdict = 'PROVEN_OUTSIDE') = ("
            + " AND ".join(_excluded(key) for key in SCOPE_KEYS)
            + ")",
            "verdict_agrees_with_checks",
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
            name=op.f(f"fk_{RECORDS}_marketplace_key_{ACCOUNTS}"),
        ),
        sa.ForeignKeyConstraint(
            ["preparation_revision_id"],
            [f"{REVISIONS}.preparation_revision_id"],
            name=op.f(f"fk_{RECORDS}_preparation_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("eligibility_id", name=op.f(f"pk_{RECORDS}")),
    )
    op.create_index(
        UX_SCOPE,
        RECORDS,
        [
            "marketplace_key",
            "marketplace_account_id",
            "preparation_revision_id",
            "candidate_fingerprint",
            "scope_version",
            "seq",
        ],
        unique=True,
    )
    _trigger(
        f"trg_{RECORDS}_append",
        "INSERT",
        _raise(
            f"{RECORDS}: records are appended in order within their scope",
            f"NEW.seq <> (SELECT COALESCE(MAX(seq), 0) + 1 FROM {RECORDS}"
            " WHERE marketplace_key = NEW.marketplace_key"
            " AND marketplace_account_id = NEW.marketplace_account_id"
            " AND preparation_revision_id = NEW.preparation_revision_id"
            " AND candidate_fingerprint = NEW.candidate_fingerprint"
            " AND scope_version = NEW.scope_version)",
        ),
    )
    # A document that is not valid JSON, or a check that is not an object, is refused by the CHECK
    # constraints; the trigger reads it as empty so that it never fails on the parse itself.
    checks = "json_each(CASE WHEN json_valid(NEW.checks_json) THEN NEW.checks_json ELSE '{}' END)"
    members = "json_each(CASE WHEN c.type = 'object' THEN c.value ELSE '{}' END)"
    _trigger(
        f"trg_{RECORDS}_closed_checklist",
        "INSERT",
        _raise(
            f"{RECORDS}: the checklist is exactly the v1 scope keys",
            f"json_valid(NEW.checks_json) AND ((SELECT COUNT(*) FROM {checks}) <> "
            f"{len(SCOPE_KEYS)} OR EXISTS (SELECT 1 FROM {checks} AS c"
            f" WHERE c.key NOT IN ({_listed(SCOPE_KEYS)})))",
        )
        + _raise(
            f"{RECORDS}: a check holds a finding and its evidence only",
            f"EXISTS (SELECT 1 FROM {checks} AS c, {members} AS m"
            f" WHERE m.key NOT IN ({_listed(CHECK_MEMBERS)}))",
        ),
    )
    _trigger(f"trg_{RECORDS}_no_update", "UPDATE", _raise(f"{RECORDS} is append-only", "1"))
    _trigger(f"trg_{RECORDS}_no_delete", "DELETE", _raise(f"{RECORDS} is never deleted", "1"))


def downgrade() -> None:
    count = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {RECORDS}")).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} canary eligibility record(s) exist: eligibility evidence is never silently"
            " destroyed"
        )
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(UX_SCOPE, table_name=RECORDS)
    op.drop_table(RECORDS)
