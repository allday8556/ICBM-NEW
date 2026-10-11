"""ADR-0036 O1: the supplier onboarding owner — identities, states, hosts, words and trials.

Revision ID: 0060_supplier_onboarding
Revises: 0059_detail_guidance_placement
Create Date: 2026-10-12

ADR-0036 §3. It adds seven tables and touches no existing table, row, trigger or index.

- `onboarded_suppliers`: the immutable identity of one onboarded site — its key (`site-<slug>`),
  display name, `https` origin (one supplier per origin) and template (`cafe24`, `godomall`).
- `onboarding_transitions`: the append-only state log. The numbering opens at one with `DRAFT`
  and moves by exactly one; each row leaves the state the row before it reached, along an allowed
  edge only (`DRAFT → RECON`, `RECON → ACTIVE`, `ACTIVE → RECON`, any other state `→ DISABLED`).
  The newest row is the current state. **`ACTIVE` is entered only when proven** (§7 D2,
  OB-04): at least 3 distinct sample URLs have a trial under the current words revision with
  every core field `CONFIRMED`, no structural problem and an operator verification `CORRECT` of
  exactly what the trial showed (its facts fingerprint), and no `WRONG` one; and no trial under the
  current words has an open structural problem. Until O3 and O5 write trials and verifications,
  `ACTIVE` is therefore unreachable.
- `onboarding_hosts`: one row per confirmation or disable of a `(host, role)`, numbered per
  supplier. A confirmed host names the trial that observed it, which must be the same supplier's;
  only the storefront role may have none, for the bootstrap host (OB-01).
- `onboarding_word_revisions`: words per template slot (a JSON object), numbered from one, with
  their fingerprint.
- `onboarding_policy_reads`, `onboarding_trials`, `onboarding_verifications`: the policy reads,
  trial reads and the operator's verifications (§3, §5, §7). Their writers land with O2, O3 and
  O5; a verification names a trial of its own supplier. A trial carries the fingerprint of the
  facts it showed, and a verification the fingerprint of what the operator was shown.

The database refuses an update or a delete of any row in the seven tables.

**Every canonical table starts empty** (M0 acceptance): nothing is seeded.

**Downgrade fails closed** while any row exists in any of the seven tables: an operator's
onboarding history is never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_supplier_onboarding"
down_revision: str | None = "0059_detail_guidance_placement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUPPLIERS = "onboarded_suppliers"
TRANSITIONS = "onboarding_transitions"
TRIALS = "onboarding_trials"
HOSTS = "onboarding_hosts"
WORDS = "onboarding_word_revisions"
POLICY_READS = "onboarding_policy_reads"
VERIFICATIONS = "onboarding_verifications"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (SUPPLIERS, TRANSITIONS, TRIALS, HOSTS, WORDS, POLICY_READS, VERIFICATIONS)

# Frozen with this revision (``app.stages.collect.onboarding.models``).
KEY_FORM = (
    "length(supplier_key) BETWEEN 6 AND 40 AND supplier_key GLOB 'site-[a-z0-9]*'"
    " AND supplier_key NOT GLOB '*[^a-z0-9-]*'"
)
HOST_FORM = "length(host) BETWEEN 3 AND 253 AND host NOT GLOB '*[^a-z0-9.-]*' AND host GLOB '*.*'"
TEMPLATES = "template IN ('cafe24', 'godomall')"
STATES = "('DRAFT', 'RECON', 'ACTIVE', 'DISABLED')"
ROLES = "role IN ('storefront', 'login', 'image')"
# The CORE fields of the fact registry (``app.stages.collect.facts.FIELD_REGISTRY``), frozen with
# this revision: an ACTIVE site's proving trials read every one of them (ADR-0036 §2 D2).
CORE_FIELDS = ("original_name", "prices", "options", "images", "stock")
ACTIVATION_SAMPLES = 3
# ADR-0036 §3, §7: the edges of the state log.
EDGES = (
    "(NEW.from_state = 'DRAFT' AND NEW.to_state IN ('RECON', 'DISABLED'))"
    " OR (NEW.from_state = 'RECON' AND NEW.to_state IN ('ACTIVE', 'DISABLED'))"
    " OR (NEW.from_state = 'ACTIVE' AND NEW.to_state IN ('RECON', 'DISABLED'))"
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json(column: str, kind: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = '{kind}'"


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _supplier_fk(table: str) -> sa.ForeignKeyConstraint:
    return _fk(table, "supplier_key", SUPPLIERS, "supplier_key")


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))


def _numbered(table: str) -> None:
    _trigger(
        table,
        "seq_follows",
        "INSERT",
        _raise(
            f"{table}: a row follows the one before it",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(r.seq), 0) FROM {table} r"
            " WHERE r.supplier_key = NEW.supplier_key)",
        ),
    )


def upgrade() -> None:
    op.create_table(
        SUPPLIERS,
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=272), nullable=False),
        sa.Column("template", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SUPPLIERS, KEY_FORM, "key_form"),
        _check(SUPPLIERS, "display_name <> ''", "name_present"),
        _check(SUPPLIERS, "origin GLOB 'https://?*' AND origin NOT GLOB '*/*/*/*'", "origin"),
        _check(SUPPLIERS, TEMPLATES, "template_known"),
        _check(SUPPLIERS, "created_by <> ''", "created_by_present"),
        sa.PrimaryKeyConstraint("supplier_key", name=op.f(f"pk_{SUPPLIERS}")),
        sa.UniqueConstraint("origin", name=op.f(f"uq_{SUPPLIERS}_origin")),
    )
    op.create_table(
        TRANSITIONS,
        sa.Column("transition_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=8), nullable=True),
        sa.Column("to_state", sa.String(length=8), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(TRANSITIONS, "seq >= 1", "seq_positive"),
        _check(TRANSITIONS, f"to_state IN {STATES}", "to_state_known"),
        _check(
            TRANSITIONS,
            f"(seq = 1 AND from_state IS NULL AND to_state = 'DRAFT')"
            f" OR (seq > 1 AND from_state IN {STATES})",
            "first_is_draft",
        ),
        _check(TRANSITIONS, "actor <> ''", "actor_present"),
        _check(TRANSITIONS, _json("evidence_json", "array"), "evidence_is_array"),
        _supplier_fk(TRANSITIONS),
        sa.PrimaryKeyConstraint("transition_id", name=op.f(f"pk_{TRANSITIONS}")),
        sa.UniqueConstraint("supplier_key", "seq", name=op.f(f"uq_{TRANSITIONS}_supplier_key_seq")),
    )
    op.create_table(
        TRIALS,
        sa.Column("trial_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("sample_url_key", sa.String(length=512), nullable=False),
        sa.Column("template_revision", sa.String(length=64), nullable=False),
        sa.Column("words_seq", sa.Integer(), nullable=False),
        sa.Column("capture_sha256", sa.String(length=64), nullable=False),
        sa.Column("facts_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("field_status_json", sa.Text(), nullable=False),
        sa.Column("unread_rows_json", sa.Text(), nullable=False),
        sa.Column("observed_hosts_json", sa.Text(), nullable=False),
        sa.Column("structural_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(TRIALS, "sample_url_key <> ''", "sample_present"),
        _check(TRIALS, "template_revision <> ''", "template_revision_present"),
        _check(TRIALS, "words_seq >= 0", "words_seq_known"),
        _check(TRIALS, _hex64("capture_sha256"), "capture_sha256_hex"),
        _check(TRIALS, _hex64("facts_fingerprint"), "facts_fingerprint_hex"),
        _check(TRIALS, _json("field_status_json", "object"), "field_status_is_object"),
        _check(TRIALS, _json("unread_rows_json", "array"), "unread_rows_is_array"),
        _check(TRIALS, _json("observed_hosts_json", "array"), "observed_hosts_is_array"),
        _check(TRIALS, _json("structural_json", "array"), "structural_is_array"),
        _check(TRIALS, "created_by <> ''", "created_by_present"),
        _supplier_fk(TRIALS),
        sa.PrimaryKeyConstraint("trial_id", name=op.f(f"pk_{TRIALS}")),
    )
    op.create_table(
        HOSTS,
        sa.Column("host_row_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("observed_trial_id", sa.String(length=36), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        _check(HOSTS, "seq >= 1", "seq_positive"),
        _check(HOSTS, HOST_FORM, "host_form"),
        _check(HOSTS, ROLES, "role_known"),
        _check(HOSTS, "disabled IN (0, 1)", "disabled_flag"),
        _check(
            HOSTS,
            "observed_trial_id IS NOT NULL OR role = 'storefront' OR disabled = 1",
            "observed_or_bootstrap",
        ),
        _check(HOSTS, "confirmed_by <> ''", "confirmed_by_present"),
        _supplier_fk(HOSTS),
        _fk(HOSTS, "observed_trial_id", TRIALS, "trial_id"),
        sa.PrimaryKeyConstraint("host_row_id", name=op.f(f"pk_{HOSTS}")),
        sa.UniqueConstraint("supplier_key", "seq", name=op.f(f"uq_{HOSTS}_supplier_key_seq")),
    )
    op.create_table(
        WORDS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("words_json", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(WORDS, "seq >= 1", "seq_positive"),
        _check(WORDS, _json("words_json", "object"), "words_is_object"),
        _check(WORDS, _hex64("fingerprint"), "fingerprint_hex"),
        _check(WORDS, "authored_by <> ''", "authored_by_present"),
        _supplier_fk(WORDS),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{WORDS}")),
        sa.UniqueConstraint("supplier_key", "seq", name=op.f(f"uq_{WORDS}_supplier_key_seq")),
    )
    op.create_table(
        POLICY_READS,
        sa.Column("read_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("document", sa.String(length=8), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_by", sa.String(length=64), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=False),
        _check(POLICY_READS, "document IN ('ROBOTS', 'TERMS')", "document_known"),
        _check(POLICY_READS, "path GLOB '/*'", "path_absolute"),
        _check(POLICY_READS, "http_status BETWEEN 100 AND 599", "http_status_range"),
        _check(
            POLICY_READS,
            f"content_sha256 IS NULL OR ({_hex64('content_sha256')})",
            "content_sha256_hex",
        ),
        _check(POLICY_READS, "acknowledged_by <> ''", "acknowledged_by_present"),
        _supplier_fk(POLICY_READS),
        sa.PrimaryKeyConstraint("read_id", name=op.f(f"pk_{POLICY_READS}")),
    )
    op.create_table(
        VERIFICATIONS,
        sa.Column("verification_id", sa.String(length=36), nullable=False),
        sa.Column("trial_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("shown_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=8), nullable=False),
        sa.Column("verified_by", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        _check(VERIFICATIONS, _hex64("shown_fingerprint"), "shown_fingerprint_hex"),
        _check(VERIFICATIONS, "verdict IN ('CORRECT', 'WRONG')", "verdict_known"),
        _check(VERIFICATIONS, "verified_by <> ''", "verified_by_present"),
        _fk(VERIFICATIONS, "trial_id", TRIALS, "trial_id"),
        _supplier_fk(VERIFICATIONS),
        sa.PrimaryKeyConstraint("verification_id", name=op.f(f"pk_{VERIFICATIONS}")),
    )
    _install_triggers()


_CURRENT_WORDS = (
    f"(SELECT COALESCE(MAX(w.seq), 0) FROM {WORDS} w WHERE w.supplier_key = NEW.supplier_key)"
)
_CORE_READ = " AND ".join(
    f"json_extract(t.field_status_json, '$.{field}') = 'CONFIRMED'" for field in CORE_FIELDS
)
# ADR-0036 §7: the conditions of approval, as the database can judge them.
_UNPROVEN = (
    "NEW.to_state = 'ACTIVE' AND ("
    f"(SELECT COUNT(DISTINCT t.sample_url_key) FROM {TRIALS} t"
    f" WHERE t.supplier_key = NEW.supplier_key AND t.words_seq = {_CURRENT_WORDS}"
    f" AND json_array_length(t.structural_json) = 0 AND {_CORE_READ}"
    f" AND EXISTS (SELECT 1 FROM {VERIFICATIONS} v WHERE v.trial_id = t.trial_id"
    " AND v.verdict = 'CORRECT' AND v.shown_fingerprint = t.facts_fingerprint)"
    f" AND NOT EXISTS (SELECT 1 FROM {VERIFICATIONS} v WHERE v.trial_id = t.trial_id"
    " AND v.verdict = 'WRONG' AND v.shown_fingerprint = t.facts_fingerprint)"
    f") < {ACTIVATION_SAMPLES}"
    f" OR EXISTS (SELECT 1 FROM {TRIALS} t WHERE t.supplier_key = NEW.supplier_key"
    f" AND t.words_seq = {_CURRENT_WORDS} AND json_array_length(t.structural_json) > 0))"
)


def _install_triggers() -> None:
    _numbered(TRANSITIONS)
    _trigger(
        TRANSITIONS,
        "leaves_current_state",
        "INSERT",
        _raise(
            f"{TRANSITIONS}: a transition leaves the state the one before it reached",
            "NEW.seq > 1 AND NEW.from_state IS NOT"
            f" (SELECT t.to_state FROM {TRANSITIONS} t WHERE t.supplier_key = NEW.supplier_key"
            " AND t.seq = NEW.seq - 1)",
        ),
    )
    _trigger(
        TRANSITIONS,
        "allowed_edge",
        "INSERT",
        _raise(
            f"{TRANSITIONS}: the transition is not an allowed edge",
            f"NEW.seq > 1 AND NOT ({EDGES})",
        ),
    )
    _trigger(
        TRANSITIONS,
        "active_is_proven",
        "INSERT",
        _raise(f"{TRANSITIONS}: ACTIVE is entered only when proven", _UNPROVEN),
    )
    _numbered(HOSTS)
    _trigger(
        HOSTS,
        "trial_is_own",
        "INSERT",
        _raise(
            f"{HOSTS}: a host is observed by a trial of its own supplier",
            "NEW.observed_trial_id IS NOT NULL AND NEW.supplier_key IS NOT"
            f" (SELECT t.supplier_key FROM {TRIALS} t WHERE t.trial_id = NEW.observed_trial_id)",
        ),
    )
    _numbered(WORDS)
    _trigger(
        VERIFICATIONS,
        "trial_is_own",
        "INSERT",
        _raise(
            f"{VERIFICATIONS}: a verification names a trial of its own supplier",
            "NEW.supplier_key IS NOT"
            f" (SELECT t.supplier_key FROM {TRIALS} t WHERE t.trial_id = NEW.trial_id)",
        ),
    )
    for table in CREATED:
        _immutable(table)


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        counts = ", ".join(f"{table}={count}" for table, count in held.items() if count)
        raise RuntimeError(
            f"cannot drop the supplier onboarding owner: rows are held ({counts});"
            " an operator's onboarding history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)
