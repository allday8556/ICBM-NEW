"""Adaptive Collector P2: the profile and validation persistence owner.

Revision ID: 0024_adaptive_profile_validation
Revises: 0023_g2_review_coverage_fence
Create Date: 2026-09-25

Issue #110, production slice P2 (authorization `5822024807`, activated by `5822923514`), under
ADR-0017 §3, §5 and §7. It adds seven tables and touches no existing table, row, trigger or index.

**What it holds.**
- ``adaptive_profile_revisions``: immutable, content-addressed ``ExtractionProfileRevision`` and
  ``PageTemplateRevision`` documents with their lineage (parent, origin, author, note). The
  digest is the primary key; the application recomputes it on every load and refuses a mismatch.
- ``adaptive_profile_pins``: the templates an EPR pins, in order, each one the database checks
  against the EPR's own document.
- ``adaptive_profile_lint``: the lint findings recorded when an EPR entered ``DRAFT``
  (ADR-0017 §7.1), with the lint rule-set revision that produced them and their digest. One
  immutable row per EPR; lint is never a verdict and never a mutable flag.
- ``adaptive_profile_transitions``: the append-only lifecycle log of an EPR's designations. Its
  vocabulary is ``DRAFT``, ``SHADOW`` and ``RETIRED`` (authorization ``5822024807``), but P2
  never enters ``SHADOW``: ADR-0017 §7.1 requires ``VALIDATED`` plus the per-supplier shadow
  switch, which a later slice owns (review ``5311392575`` B1). ``VALIDATED`` is never stored: it
  is derived from a ``PASS`` validation run for the exact freshness tuple. ``ACTIVE`` does not
  exist here: ADR-0017 §7.4 does not authorize it.
- ``adaptive_validation_samples``: operator-captured ``ValidationSample`` material, kept only in
  this local database. A sample is retained while any validation run references it; the database
  refuses to delete a referenced one.
- ``adaptive_validation_runs`` and ``adaptive_validation_run_samples``: every validation run with
  its verdict, checks, exact freshness tuple, run digest and sample count, and the ordered samples
  it replayed. The database admits a linked sample only at a position below the run's count and
  only of the run's own supplier; the application recomputes the ordered sample-set digest from
  the links on every read.

**What the database enforces.** Revisions, pins, transitions and runs are never updated or deleted.
A transition follows the one before it, with no gap, and only in the lifecycle's own shape. A pin
names a template of the same supplier, at the position the EPR's document names it. Samples are
never updated, and are deletable only while unreferenced. A sample belongs to one supplier.

**Downgrade fails closed.** It refuses while any of the seven tables holds a row: profile and
validation history is never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_adaptive_profile_validation"
down_revision: str | None = "0023_g2_review_coverage_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "adaptive_profile_revisions"
PINS = "adaptive_profile_pins"
LINT = "adaptive_profile_lint"
TRANSITIONS = "adaptive_profile_transitions"
SAMPLES = "adaptive_validation_samples"
RUNS = "adaptive_validation_runs"
RUN_SAMPLES = "adaptive_validation_run_samples"
CREATED = (RUN_SAMPLES, RUNS, SAMPLES, TRANSITIONS, LINT, PINS, REVISIONS)

KINDS = ("EXTRACTION_PROFILE", "PAGE_TEMPLATE")
ORIGINS = ("OPERATOR", "AI_PROPOSAL", "IMPORT")
STATES = ("DRAFT", "SHADOW", "RETIRED")
VERDICTS = ("PASS", "FAIL", "INCOMPLETE")
NOTE_MAX_CHARS = 500
TRANSITION_SHAPE = (
    "(seq = 1 AND from_state IS NULL AND to_state = 'DRAFT')"
    " OR (seq > 1 AND from_state = 'DRAFT' AND to_state IN ('SHADOW', 'RETIRED'))"
    " OR (seq > 1 AND from_state = 'SHADOW' AND to_state IN ('DRAFT', 'RETIRED'))"
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _present(column: str) -> str:
    return f"{column} <> ''"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str, *, deletable_when: str | None = None) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    if deletable_when is None:
        _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))
    else:
        _trigger(
            table,
            "delete_guard",
            "DELETE",
            _raise(f"a referenced {table} row is retained", f"NOT ({deletable_when})"),
        )


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("parent_digest", sa.String(length=64), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, _hex64("digest"), "digest_hex"),
        _check(REVISIONS, _in("kind", KINDS), "kind_valid"),
        _check(REVISIONS, _present("supplier_key"), "supplier_present"),
        _check(REVISIONS, _present("schema_version"), "schema_present"),
        _check(
            REVISIONS,
            "json_valid(document) AND json_type(document) = 'object'"
            " AND json_extract(document, '$.kind') = kind"
            " AND json_extract(document, '$.supplier_key') = supplier_key"
            " AND json_extract(document, '$.schema_version') = schema_version",
            "document_agrees",
        ),
        _check(
            REVISIONS,
            f"parent_digest IS NULL OR ({_hex64('parent_digest')} AND parent_digest <> digest)",
            "parent_valid",
        ),
        _check(REVISIONS, _in("origin", ORIGINS), "origin_valid"),
        _check(
            REVISIONS,
            f"change_note IS NULL OR (length(change_note) BETWEEN 1 AND {NOTE_MAX_CHARS})",
            "note_bounded",
        ),
        _check(REVISIONS, _present("created_by"), "author_present"),
        _check(REVISIONS, _present("correlation_id"), "correlation_present"),
        sa.ForeignKeyConstraint(
            ["parent_digest"],
            [f"{REVISIONS}.digest"],
            name=op.f(f"fk_{REVISIONS}_parent_digest_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("digest", name=op.f(f"pk_{REVISIONS}")),
    )
    op.create_index(
        "ix_adaptive_profile_revisions_supplier_key_kind", REVISIONS, ["supplier_key", "kind"]
    )

    op.create_table(
        PINS,
        sa.Column("epr_digest", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ptr_digest", sa.String(length=64), nullable=False),
        _check(PINS, "position >= 0", "position_valid"),
        sa.ForeignKeyConstraint(
            ["epr_digest"], [f"{REVISIONS}.digest"], name=op.f(f"fk_{PINS}_epr_digest_{REVISIONS}")
        ),
        sa.ForeignKeyConstraint(
            ["ptr_digest"], [f"{REVISIONS}.digest"], name=op.f(f"fk_{PINS}_ptr_digest_{REVISIONS}")
        ),
        sa.PrimaryKeyConstraint("epr_digest", "position", name=op.f(f"pk_{PINS}")),
        sa.UniqueConstraint(
            "epr_digest", "ptr_digest", name=op.f(f"uq_{PINS}_epr_digest_ptr_digest")
        ),
    )

    op.create_table(
        LINT,
        sa.Column("epr_digest", sa.String(length=64), nullable=False),
        sa.Column("lint_revision", sa.String(length=64), nullable=False),
        sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("lint_digest", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(LINT, _present("lint_revision"), "revision_present"),
        _check(
            LINT,
            "json_valid(findings_json) AND json_type(findings_json) = 'array'",
            "findings_array",
        ),
        _check(LINT, _hex64("lint_digest"), "digest_hex"),
        sa.ForeignKeyConstraint(
            ["epr_digest"], [f"{REVISIONS}.digest"], name=op.f(f"fk_{LINT}_epr_digest_{REVISIONS}")
        ),
        sa.PrimaryKeyConstraint("epr_digest", name=op.f(f"pk_{LINT}")),
    )

    op.create_table(
        TRANSITIONS,
        sa.Column("transition_id", sa.String(length=36), nullable=False),
        sa.Column("epr_digest", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        _check(TRANSITIONS, _in("to_state", STATES), "to_state_valid"),
        _check(TRANSITIONS, TRANSITION_SHAPE, "transition_shape"),
        _check(TRANSITIONS, _present("reason"), "reason_present"),
        _check(TRANSITIONS, _present("actor"), "actor_present"),
        _check(TRANSITIONS, _present("correlation_id"), "correlation_present"),
        sa.ForeignKeyConstraint(
            ["epr_digest"],
            [f"{REVISIONS}.digest"],
            name=op.f(f"fk_{TRANSITIONS}_epr_digest_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("transition_id", name=op.f(f"pk_{TRANSITIONS}")),
        sa.UniqueConstraint("epr_digest", "seq", name=op.f(f"uq_{TRANSITIONS}_epr_digest_seq")),
    )

    op.create_table(
        SAMPLES,
        sa.Column("sample_digest", sa.String(length=64), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("capture_revision", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("structure_json", sa.Text(), nullable=False),
        sa.Column("expected_json", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("stored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("stored_at", sa.DateTime(), nullable=False),
        _check(SAMPLES, _hex64("sample_digest"), "digest_hex"),
        _check(SAMPLES, _present("supplier_key"), "supplier_present"),
        _check(SAMPLES, _present("capture_revision"), "capture_present"),
        _check(
            SAMPLES,
            "json_valid(structure_json) AND json_valid(expected_json)"
            " AND json_valid(provenance_json)"
            " AND json_extract(provenance_json, '$.capture_revision') = capture_revision",
            "documents_agree",
        ),
        _check(SAMPLES, _present("stored_by"), "author_present"),
        _check(SAMPLES, _present("correlation_id"), "correlation_present"),
        sa.PrimaryKeyConstraint("sample_digest", name=op.f(f"pk_{SAMPLES}")),
    )

    op.create_table(
        RUNS,
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("epr_digest", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("profile_schema_version", sa.String(length=32), nullable=False),
        sa.Column("extractor_revision", sa.String(length=64), nullable=False),
        sa.Column("extractor_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("hook_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("sample_set_digest", sa.String(length=64), nullable=False),
        sa.Column("capture_revision", sa.String(length=64), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("run_digest", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(RUNS, _in("verdict", VERDICTS), "verdict_valid"),
        _check(RUNS, _present("profile_schema_version"), "schema_present"),
        _check(RUNS, _present("extractor_revision"), "extractor_present"),
        _check(RUNS, _hex64("extractor_fingerprint"), "extractor_fingerprint_hex"),
        _check(
            RUNS, f"hook_fingerprint = '' OR ({_hex64('hook_fingerprint')})", "hook_fingerprint"
        ),
        _check(RUNS, _hex64("sample_set_digest"), "sample_set_hex"),
        _check(RUNS, _present("capture_revision"), "capture_present"),
        _check(
            RUNS, "json_valid(checks_json) AND json_type(checks_json) = 'array'", "checks_array"
        ),
        _check(RUNS, "sample_count >= 0", "sample_count_valid"),
        _check(RUNS, _hex64("run_digest"), "run_digest_hex"),
        _check(RUNS, _present("recorded_by"), "author_present"),
        _check(RUNS, _present("correlation_id"), "correlation_present"),
        sa.ForeignKeyConstraint(
            ["epr_digest"], [f"{REVISIONS}.digest"], name=op.f(f"fk_{RUNS}_epr_digest_{REVISIONS}")
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f(f"pk_{RUNS}")),
        sa.UniqueConstraint("run_digest", name=op.f(f"uq_{RUNS}_run_digest")),
    )
    op.create_index("ix_adaptive_validation_runs_epr_digest", RUNS, ["epr_digest"])

    op.create_table(
        RUN_SAMPLES,
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sample_digest", sa.String(length=64), nullable=False),
        _check(RUN_SAMPLES, "position >= 0", "position_valid"),
        sa.ForeignKeyConstraint(
            ["run_id"], [f"{RUNS}.run_id"], name=op.f(f"fk_{RUN_SAMPLES}_run_id_{RUNS}")
        ),
        sa.ForeignKeyConstraint(
            ["sample_digest"],
            [f"{SAMPLES}.sample_digest"],
            name=op.f(f"fk_{RUN_SAMPLES}_sample_digest_{SAMPLES}"),
        ),
        sa.PrimaryKeyConstraint("run_id", "position", name=op.f(f"pk_{RUN_SAMPLES}")),
    )
    op.create_index(
        "ix_adaptive_validation_run_samples_sample_digest", RUN_SAMPLES, ["sample_digest"]
    )
    _install_triggers()


def _install_triggers() -> None:
    _immutable(REVISIONS)
    # Lineage stays inside one supplier and one kind.
    _trigger(
        REVISIONS,
        "lineage_same_kind",
        "INSERT",
        _raise(
            f"{REVISIONS}: a parent is a revision of the same kind and supplier",
            f"NEW.parent_digest IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {REVISIONS} p"
            " WHERE p.digest = NEW.parent_digest AND p.kind = NEW.kind"
            " AND p.supplier_key = NEW.supplier_key)",
        ),
    )
    _immutable(PINS)
    _trigger(
        PINS,
        "pins_what_the_epr_names",
        "INSERT",
        _raise(
            f"{PINS}: a pin is a same-supplier template at the position the EPR names it",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} e, {REVISIONS} t"
            " WHERE e.digest = NEW.epr_digest AND e.kind = 'EXTRACTION_PROFILE'"
            " AND t.digest = NEW.ptr_digest AND t.kind = 'PAGE_TEMPLATE'"
            " AND t.supplier_key = e.supplier_key"
            " AND json_extract(e.document, '$.templates[' || NEW.position || ']')"
            " = NEW.ptr_digest)",
        ),
    )
    _immutable(LINT)
    _trigger(
        LINT,
        "of_an_epr",
        "INSERT",
        _raise(
            f"{LINT}: only an EPR records DRAFT lint",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r WHERE r.digest = NEW.epr_digest"
            " AND r.kind = 'EXTRACTION_PROFILE')",
        ),
    )
    _immutable(TRANSITIONS)
    _trigger(
        TRANSITIONS,
        "of_an_epr",
        "INSERT",
        _raise(
            f"{TRANSITIONS}: only an EPR has a lifecycle",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r WHERE r.digest = NEW.epr_digest"
            " AND r.kind = 'EXTRACTION_PROFILE')",
        ),
    )
    _trigger(
        TRANSITIONS,
        "follows",
        "INSERT",
        _raise(
            f"{TRANSITIONS}: a transition follows the one before it",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(seq), 0) FROM {TRANSITIONS} t"
            " WHERE t.epr_digest = NEW.epr_digest)"
            f" OR (NEW.seq > 1 AND NEW.from_state IS NOT (SELECT to_state FROM {TRANSITIONS} t"
            " WHERE t.epr_digest = NEW.epr_digest AND t.seq = NEW.seq - 1))",
        ),
    )
    _immutable(
        SAMPLES,
        deletable_when=(
            f"NOT EXISTS (SELECT 1 FROM {RUN_SAMPLES} s WHERE s.sample_digest = OLD.sample_digest)"
        ),
    )
    _immutable(RUNS)
    _trigger(
        RUNS,
        "of_an_epr",
        "INSERT",
        _raise(
            f"{RUNS}: a validation run validates an EPR",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r WHERE r.digest = NEW.epr_digest"
            " AND r.kind = 'EXTRACTION_PROFILE')",
        ),
    )
    _immutable(RUN_SAMPLES)
    _trigger(
        RUN_SAMPLES,
        "within_the_run",
        "INSERT",
        _raise(
            f"{RUN_SAMPLES}: a linked sample is of the supplier of the run, inside its sample count",
            f"NOT EXISTS (SELECT 1 FROM {RUNS} run, {REVISIONS} e, {SAMPLES} s"
            " WHERE run.run_id = NEW.run_id AND NEW.position < run.sample_count"
            " AND e.digest = run.epr_digest AND s.sample_digest = NEW.sample_digest"
            " AND s.supplier_key = e.supplier_key)",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): Adaptive profile and validation history is never "
                "silently destroyed"
            )
    for table in CREATED:
        op.drop_table(table)
