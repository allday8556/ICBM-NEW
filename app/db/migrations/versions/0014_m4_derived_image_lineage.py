"""M4 PR-E: derived image lineage, operator image selection and exact-binary QA.

Revision ID: 0014_m4_derived_image_lineage
Revises: 0013_m4_pricing_snapshots
Create Date: 2026-09-19

Issue #80 PR-E (kickoff 5738166312), under ADR-0013 §9 and ADR-0010 §9. It is additive, and
**nothing is backfilled**: an artifact, a selection and a QA verdict are decisions or completed
operations, and no migration can make one. ``source_assets`` and ``product_facts_image_refs`` are
only referenced, never written.

**What the triggers enforce across rows:**
- *Lineage.* A derivation's inputs are recorded in order, only while it is being assembled, and
  each is exactly the one its canonical manifest names. A source input is a CONFIRMED image
  reference of the revision the derivation was validated against. A derived input is the artifact
  its parent derivation produced; that parent is complete and was validated against the same
  revision. A complete derivation cannot gain an input, so a cycle cannot be built. A root is a
  source input of the derivation or a root of one of its parents.
- *Selection.* A selection reviews the revision of the Item's open binding, and it accounts for
  every CONFIRMED source image of it: one decision each, nothing omitted. A replacement derives
  from that very source image under that revision. Each output is exactly the binary its decision
  chose, and an EXCLUDE decision has none.
- *Current selection.* A move names a complete OPERATOR selection of its own Item, extends the
  history in order from the current selection, and never returns to an earlier one.
- *QA.* A verdict names a CONFIRMED source image of its validated revision, or the artifact of a
  derivation validated against that same revision. Its findings are bounded codes. Partial unique
  indexes allow one authoritative verdict per exact input.

Every table rejects UPDATE and DELETE.

**Downgrade fails closed.** While any image lineage, selection or QA row exists it refuses: that
history is never silently destroyed.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_m4_derived_image_lineage"
down_revision: str | None = "0013_m4_pricing_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARTIFACTS = "derived_image_artifacts"
DERIVATIONS = "derived_image_derivations"
INPUTS = "derived_image_derivation_inputs"
ROOTS = "derived_image_derivation_roots"
SELECTIONS = "image_selection_revisions"
DECISIONS = "image_selection_source_decisions"
OUTPUTS = "image_selection_outputs"
MOVES = "current_image_selection_moves"
QA = "image_qa_results"
# Creation order; dropped in reverse.
TABLES = (ARTIFACTS, DERIVATIONS, INPUTS, ROOTS, SELECTIONS, DECISIONS, OUTPUTS, MOVES, QA)
ITEMS = "product_items"
GROUPS = "product_groups"
BINDINGS = "source_bindings"
REVISIONS = "product_facts_revisions"
REFS = "product_facts_image_refs"
SOURCE_ASSETS = "source_assets"

# Vocabularies and bounds frozen with this revision (app.products.image_model).
_KINDS = ("SOURCE_ASSET", "DERIVED_ARTIFACT")
_ROLES = ("REPRESENTATIVE", "DETAIL")
_DECISIONS = ("USE_SOURCE", "USE_DERIVED", "EXCLUDE")
_ORIGINS = ("OPERATOR",)
_MOVE_REASONS = ("INITIAL", "RESELECTED")
_VERDICTS = ("PASS", "REVIEW_REQUIRED", "FAIL")
_MAX_INPUTS = 16
_MAX_OPERATIONS = 16
_MAX_FINDINGS = 16
_MAX_SPEC_BYTES = 4096


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _present(column: str) -> str:
    return f"{column} <> ''"


def _kind(column: str, derivation: str) -> str:
    return f"({column} = 'DERIVED_ARTIFACT') = ({derivation} IS NOT NULL)"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _trigger(name: str, event: str, table: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        ARTIFACTS,
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=40), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("stored_at", sa.DateTime(), nullable=False),
        _check(ARTIFACTS, _hex64("artifact_sha256"), "sha256_hex"),
        _check(ARTIFACTS, "mime_type LIKE 'image/%'", "mime_is_image"),
        _check(ARTIFACTS, "byte_size > 0", "byte_size_positive"),
        _check(ARTIFACTS, "width > 0 AND height > 0", "dimensions_positive"),
        sa.PrimaryKeyConstraint("artifact_sha256", name=op.f(f"pk_{ARTIFACTS}")),
    )
    op.create_table(
        DERIVATIONS,
        sa.Column("derivation_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("validated_source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("derivation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_manifest_json", sa.Text(), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("transformation_spec_json", sa.Text(), nullable=False),
        sa.Column("transformation_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("operations_json", sa.Text(), nullable=False),
        sa.Column("produced_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check(DERIVATIONS, _hex64("derivation_fingerprint"), "fingerprint_hex"),
        _check(
            DERIVATIONS,
            "json_valid(input_manifest_json) AND json_type(input_manifest_json) = 'array'"
            f" AND input_count >= 1 AND input_count <= {_MAX_INPUTS}"
            " AND json_array_length(input_manifest_json) = input_count",
            "inputs_manifest",
        ),
        _check(
            DERIVATIONS,
            "json_valid(transformation_spec_json)"
            " AND json_type(transformation_spec_json) = 'object'"
            f" AND length(transformation_spec_json) <= {_MAX_SPEC_BYTES}"
            " AND instr(transformation_spec_json, '://') = 0",
            "spec_bounded",
        ),
        _check(
            DERIVATIONS,
            "json_valid(operations_json) AND json_type(operations_json) = 'array'"
            f" AND json_array_length(operations_json) BETWEEN 1 AND {_MAX_OPERATIONS}"
            " AND instr(operations_json, '://') = 0",
            "operations_bounded",
        ),
        _check(DERIVATIONS, _present("transformation_version"), "transformation_version_present"),
        _check(
            DERIVATIONS,
            "policy_version IS NULL OR policy_version <> ''",
            "policy_version_present",
        ),
        _check(DERIVATIONS, _present("decided_by"), "decided_by_present"),
        _check(DERIVATIONS, _present("correlation_id"), "correlation_present"),
        _fk(DERIVATIONS, "artifact_sha256", ARTIFACTS, "artifact_sha256"),
        _fk(DERIVATIONS, "validated_source_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("derivation_id", name=op.f(f"pk_{DERIVATIONS}")),
        sa.UniqueConstraint(
            "derivation_fingerprint", name=op.f(f"uq_{DERIVATIONS}_derivation_fingerprint")
        ),
    )
    op.create_index("ix_derived_image_derivations_artifact", DERIVATIONS, ["artifact_sha256"])
    op.create_table(
        INPUTS,
        sa.Column("derivation_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("input_kind", sa.String(length=20), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("parent_artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("parent_derivation_id", sa.String(length=36), nullable=True),
        _check(INPUTS, "ordinal >= 0", "ordinal_non_negative"),
        _check(INPUTS, _in("input_kind", _KINDS), "input_kind_valid"),
        _check(
            INPUTS,
            "(input_kind = 'SOURCE_ASSET' AND source_sha256 IS NOT NULL"
            " AND parent_artifact_sha256 IS NULL AND parent_derivation_id IS NULL)"
            " OR (input_kind = 'DERIVED_ARTIFACT' AND source_sha256 IS NULL"
            " AND parent_artifact_sha256 IS NOT NULL AND parent_derivation_id IS NOT NULL)",
            "input_kind_columns",
        ),
        _check(
            INPUTS,
            "parent_derivation_id IS NULL OR parent_derivation_id <> derivation_id",
            "not_its_own_parent",
        ),
        _fk(INPUTS, "derivation_id", DERIVATIONS, "derivation_id"),
        _fk(INPUTS, "source_sha256", SOURCE_ASSETS, "sha256"),
        _fk(INPUTS, "parent_artifact_sha256", ARTIFACTS, "artifact_sha256"),
        _fk(INPUTS, "parent_derivation_id", DERIVATIONS, "derivation_id"),
        sa.PrimaryKeyConstraint("derivation_id", "ordinal", name=op.f(f"pk_{INPUTS}")),
    )
    op.create_table(
        ROOTS,
        sa.Column("derivation_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        _fk(ROOTS, "derivation_id", DERIVATIONS, "derivation_id"),
        _fk(ROOTS, "source_sha256", SOURCE_ASSETS, "sha256"),
        sa.PrimaryKeyConstraint("derivation_id", "source_sha256", name=op.f(f"pk_{ROOTS}")),
    )
    op.create_table(
        SELECTIONS,
        sa.Column("selection_revision_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source_decision_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("selection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision_origin", sa.String(length=20), nullable=False),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SELECTIONS, "revision_no >= 1", "revision_no_positive"),
        _check(SELECTIONS, _hex64("source_decision_fingerprint"), "source_fingerprint_hex"),
        _check(SELECTIONS, _hex64("selection_fingerprint"), "selection_fingerprint_hex"),
        _check(SELECTIONS, _in("decision_origin", _ORIGINS), "operator_decision"),
        _check(
            SELECTIONS,
            "decision_count >= 0 AND output_count >= 0 AND output_count <= decision_count",
            "counts_valid",
        ),
        _check(SELECTIONS, _present("decided_by"), "decided_by_present"),
        _check(
            SELECTIONS,
            "reason IS NULL OR (reason <> '' AND length(reason) <= 200)",
            "reason_bounded",
        ),
        _check(SELECTIONS, _present("correlation_id"), "correlation_present"),
        _fk(SELECTIONS, "item_id", ITEMS, "item_id"),
        _fk(SELECTIONS, "product_group_id", GROUPS, "product_group_id"),
        _fk(SELECTIONS, "source_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("selection_revision_id", name=op.f(f"pk_{SELECTIONS}")),
        sa.UniqueConstraint(
            "item_id", "revision_no", name=op.f(f"uq_{SELECTIONS}_item_id_revision_no")
        ),
    )
    op.create_table(
        DECISIONS,
        sa.Column("selection_revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_role", sa.String(length=20), nullable=False),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("derivation_id", sa.String(length=36), nullable=True),
        _check(DECISIONS, _in("source_role", _ROLES), "source_role_valid"),
        _check(DECISIONS, "source_ordinal >= 0", "source_ordinal_non_negative"),
        _check(DECISIONS, _in("decision", _DECISIONS), "decision_valid"),
        _check(
            DECISIONS,
            "(decision = 'USE_DERIVED') = (derivation_id IS NOT NULL)",
            "derived_names_derivation",
        ),
        _fk(DECISIONS, "selection_revision_id", SELECTIONS, "selection_revision_id"),
        _fk(DECISIONS, "source_sha256", SOURCE_ASSETS, "sha256"),
        _fk(DECISIONS, "derivation_id", DERIVATIONS, "derivation_id"),
        sa.PrimaryKeyConstraint(
            "selection_revision_id", "source_role", "source_ordinal", name=op.f(f"pk_{DECISIONS}")
        ),
    )
    op.create_table(
        OUTPUTS,
        sa.Column("selection_revision_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source_role", sa.String(length=20), nullable=False),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("asset_kind", sa.String(length=20), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("derivation_id", sa.String(length=36), nullable=True),
        _check(OUTPUTS, "position >= 0", "position_non_negative"),
        _check(OUTPUTS, _in("role", _ROLES), "role_valid"),
        _check(OUTPUTS, _in("asset_kind", _KINDS), "asset_kind_valid"),
        _check(OUTPUTS, _hex64("sha256"), "sha256_hex"),
        _check(OUTPUTS, _kind("asset_kind", "derivation_id"), "derived_names_derivation"),
        _fk(OUTPUTS, "selection_revision_id", SELECTIONS, "selection_revision_id"),
        _fk(OUTPUTS, "derivation_id", DERIVATIONS, "derivation_id"),
        sa.PrimaryKeyConstraint("selection_revision_id", "position", name=op.f(f"pk_{OUTPUTS}")),
        sa.UniqueConstraint(
            "selection_revision_id",
            "source_role",
            "source_ordinal",
            name=op.f(f"uq_{OUTPUTS}_selection_revision_id_source_role_source_ordinal"),
        ),
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("selection_revision_id", sa.String(length=36), nullable=False),
        sa.Column("previous_selection_revision_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(MOVES, _in("reason", _MOVE_REASONS), "reason_valid"),
        _check(
            MOVES,
            "(sequence = 1) = (previous_selection_revision_id IS NULL)",
            "first_move_has_no_previous",
        ),
        _check(MOVES, "(reason = 'INITIAL') = (sequence = 1)", "initial_opens_history"),
        _check(
            MOVES,
            "previous_selection_revision_id IS NULL"
            " OR previous_selection_revision_id <> selection_revision_id",
            "move_changes_selection",
        ),
        _check(MOVES, _present("rule_version"), "rule_version_present"),
        _check(MOVES, _present("decided_by"), "decided_by_present"),
        _check(MOVES, _present("correlation_id"), "correlation_present"),
        _fk(MOVES, "item_id", ITEMS, "item_id"),
        _fk(MOVES, "selection_revision_id", SELECTIONS, "selection_revision_id"),
        _fk(MOVES, "previous_selection_revision_id", SELECTIONS, "selection_revision_id"),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint("item_id", "sequence", name=op.f(f"uq_{MOVES}_item_id_sequence")),
    )
    op.create_table(
        QA,
        sa.Column("qa_result_id", sa.String(length=36), nullable=False),
        sa.Column("asset_kind", sa.String(length=20), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("derivation_id", sa.String(length=36), nullable=True),
        sa.Column("validated_source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("qa_rule_version", sa.String(length=64), nullable=False),
        sa.Column("qa_input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(QA, _in("asset_kind", _KINDS), "asset_kind_valid"),
        _check(QA, _hex64("sha256"), "sha256_hex"),
        _check(QA, _kind("asset_kind", "derivation_id"), "derived_names_derivation"),
        _check(QA, _present("qa_rule_version"), "rule_version_present"),
        _check(QA, _hex64("qa_input_fingerprint"), "input_fingerprint_hex"),
        _check(QA, _in("verdict", _VERDICTS), "verdict_valid"),
        _check(
            QA,
            "json_valid(findings_json) AND json_type(findings_json) = 'array'"
            f" AND json_array_length(findings_json) <= {_MAX_FINDINGS}",
            "findings_bounded",
        ),
        _check(QA, _present("decided_by"), "decided_by_present"),
        _check(QA, _present("correlation_id"), "correlation_present"),
        _fk(QA, "derivation_id", DERIVATIONS, "derivation_id"),
        _fk(QA, "validated_source_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("qa_result_id", name=op.f(f"pk_{QA}")),
    )
    op.create_index(
        "ux_image_qa_results_source_input",
        QA,
        ["sha256", "validated_source_revision_id", "qa_rule_version", "qa_input_fingerprint"],
        unique=True,
        sqlite_where=sa.text("derivation_id IS NULL"),
    )
    op.create_index(
        "ux_image_qa_results_derived_input",
        QA,
        [
            "derivation_id",
            "sha256",
            "validated_source_revision_id",
            "qa_rule_version",
            "qa_input_fingerprint",
        ],
        unique=True,
        sqlite_where=sa.text("derivation_id IS NOT NULL"),
    )
    _install_triggers()


def _install_triggers() -> None:
    for table in TABLES:
        _trigger(f"trg_{table}_no_update", "UPDATE", table, _raise(f"{table} is append-only", "1"))
        _trigger(f"trg_{table}_no_delete", "DELETE", table, _raise(f"{table} is append-only", "1"))

    derivation = f"(SELECT {{column}} FROM {DERIVATIONS} WHERE derivation_id = {{id}})"
    own_count = f"(SELECT COUNT(*) FROM {INPUTS} WHERE derivation_id = NEW.derivation_id)"
    own_total = derivation.format(column="input_count", id="NEW.derivation_id")
    manifest = derivation.format(
        column="json_extract(input_manifest_json, '$[' || NEW.ordinal || '].{field}')",
        id="NEW.derivation_id",
    )
    _trigger(
        f"trg_{INPUTS}_lineage",
        "INSERT",
        INPUTS,
        _raise(
            f"{INPUTS}: inputs are recorded in order while the derivation is assembled",
            f"NEW.ordinal <> {own_count} OR NEW.ordinal >= {own_total}",
        )
        + _raise(
            f"{INPUTS}: an input is the one its derivation manifest names",
            f"{manifest.format(field='kind')} IS NOT NEW.input_kind"
            f" OR {manifest.format(field='sha256')}"
            " IS NOT COALESCE(NEW.source_sha256, NEW.parent_artifact_sha256)"
            f" OR {manifest.format(field='parent_derivation_id')} IS NOT NEW.parent_derivation_id",
        )
        + _raise(
            f"{INPUTS}: a source input is a CONFIRMED image of the validated revision",
            f"NEW.input_kind = 'SOURCE_ASSET' AND NOT EXISTS (SELECT 1 FROM {REFS} r"
            f" JOIN {DERIVATIONS} d ON d.validated_source_revision_id = r.revision_id"
            " WHERE d.derivation_id = NEW.derivation_id AND r.sha256 = NEW.source_sha256"
            " AND r.status = 'CONFIRMED')",
        )
        + _raise(
            f"{INPUTS}: a derived input is the artifact its parent derivation produced",
            f"NEW.input_kind = 'DERIVED_ARTIFACT' AND NOT EXISTS (SELECT 1 FROM {DERIVATIONS} p"
            " WHERE p.derivation_id = NEW.parent_derivation_id"
            " AND p.artifact_sha256 = NEW.parent_artifact_sha256)",
        )
        + _raise(
            f"{INPUTS}: a parent derivation is complete",
            f"NEW.input_kind = 'DERIVED_ARTIFACT' AND (SELECT COUNT(*) FROM {INPUTS}"
            " WHERE derivation_id = NEW.parent_derivation_id)"
            f" IS NOT {derivation.format(column='input_count', id='NEW.parent_derivation_id')}",
        )
        + _raise(
            f"{INPUTS}: a parent derivation was validated against the same source revision",
            "NEW.input_kind = 'DERIVED_ARTIFACT'"
            f" AND {derivation.format(column='validated_source_revision_id', id='NEW.parent_derivation_id')}"
            " IS NOT"
            f" {derivation.format(column='validated_source_revision_id', id='NEW.derivation_id')}",
        ),
    )
    _trigger(
        f"trg_{ROOTS}_traced",
        "INSERT",
        ROOTS,
        _raise(
            f"{ROOTS}: roots follow a complete derivation",
            f"{own_count} IS NOT {own_total}",
        )
        + _raise(
            f"{ROOTS}: a root is a source input or a root of a parent",
            f"NOT EXISTS (SELECT 1 FROM {INPUTS} i WHERE i.derivation_id = NEW.derivation_id"
            " AND i.input_kind = 'SOURCE_ASSET' AND i.source_sha256 = NEW.source_sha256)"
            f" AND NOT EXISTS (SELECT 1 FROM {INPUTS} i JOIN {ROOTS} r"
            " ON r.derivation_id = i.parent_derivation_id"
            " WHERE i.derivation_id = NEW.derivation_id AND i.input_kind = 'DERIVED_ARTIFACT'"
            " AND r.source_sha256 = NEW.source_sha256)",
        ),
    )
    confirmed_refs = (
        f"(SELECT COUNT(*) FROM {REFS} WHERE revision_id = NEW.source_revision_id"
        " AND status = 'CONFIRMED')"
    )
    _trigger(
        f"trg_{SELECTIONS}_scope",
        "INSERT",
        SELECTIONS,
        _raise(
            f"{SELECTIONS}: the group is that of the Item",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.item_id = NEW.item_id"
            " AND i.product_group_id = NEW.product_group_id)",
        )
        + _raise(
            f"{SELECTIONS}: a selection reviews the revision of the open binding of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b WHERE b.item_id = NEW.item_id"
            " AND b.valid_to IS NULL AND b.provenance_revision_id = NEW.source_revision_id)",
        )
        + _raise(
            f"{SELECTIONS}: selection revisions are appended in order",
            f"NEW.revision_no <> (SELECT COALESCE(MAX(revision_no), 0) + 1 FROM {SELECTIONS}"
            " WHERE item_id = NEW.item_id)",
        )
        + _raise(
            f"{SELECTIONS}: a selection accounts for every CONFIRMED source image",
            f"NEW.decision_count <> {confirmed_refs}",
        ),
    )
    selection = f"(SELECT {{column}} FROM {SELECTIONS} WHERE selection_revision_id = {{id}})"
    decisions_made = f"(SELECT COUNT(*) FROM {DECISIONS} WHERE selection_revision_id = NEW.selection_revision_id)"
    _trigger(
        f"trg_{DECISIONS}_accounted",
        "INSERT",
        DECISIONS,
        _raise(
            f"{DECISIONS}: a decision names a CONFIRMED source image of the reviewed revision",
            f"NOT EXISTS (SELECT 1 FROM {REFS} r JOIN {SELECTIONS} s"
            " ON s.source_revision_id = r.revision_id"
            " WHERE s.selection_revision_id = NEW.selection_revision_id"
            " AND r.role = NEW.source_role AND r.ordinal = NEW.source_ordinal"
            " AND r.sha256 = NEW.source_sha256 AND r.status = 'CONFIRMED')",
        )
        + _raise(
            f"{DECISIONS}: decisions are recorded while the selection is assembled",
            f"{decisions_made} >= "
            f"{selection.format(column='decision_count', id='NEW.selection_revision_id')}",
        )
        + _raise(
            f"{DECISIONS}: a replacement derives from that source image under the reviewed revision",
            f"NEW.decision = 'USE_DERIVED' AND NOT EXISTS (SELECT 1 FROM {DERIVATIONS} d"
            f" JOIN {ROOTS} r ON r.derivation_id = d.derivation_id"
            f" JOIN {SELECTIONS} s ON s.selection_revision_id = NEW.selection_revision_id"
            " WHERE d.derivation_id = NEW.derivation_id"
            " AND d.validated_source_revision_id = s.source_revision_id"
            " AND r.source_sha256 = NEW.source_sha256)",
        ),
    )
    outputs_made = (
        f"(SELECT COUNT(*) FROM {OUTPUTS} WHERE selection_revision_id = NEW.selection_revision_id)"
    )
    _trigger(
        f"trg_{OUTPUTS}_chosen",
        "INSERT",
        OUTPUTS,
        _raise(
            f"{OUTPUTS}: outputs follow a complete decision manifest",
            f"{decisions_made} IS NOT "
            f"{selection.format(column='decision_count', id='NEW.selection_revision_id')}",
        )
        + _raise(
            f"{OUTPUTS}: outputs are recorded in order while the selection is assembled",
            f"NEW.position <> {outputs_made} OR NEW.position >= "
            f"{selection.format(column='output_count', id='NEW.selection_revision_id')}",
        )
        + _raise(
            f"{OUTPUTS}: an output is the binary its decision chose",
            f"NOT EXISTS (SELECT 1 FROM {DECISIONS} d"
            " WHERE d.selection_revision_id = NEW.selection_revision_id"
            " AND d.source_role = NEW.source_role AND d.source_ordinal = NEW.source_ordinal"
            " AND ((d.decision = 'USE_SOURCE' AND NEW.asset_kind = 'SOURCE_ASSET'"
            " AND NEW.sha256 = d.source_sha256 AND NEW.derivation_id IS NULL)"
            " OR (d.decision = 'USE_DERIVED' AND NEW.asset_kind = 'DERIVED_ARTIFACT'"
            " AND NEW.derivation_id = d.derivation_id AND NEW.sha256 = (SELECT artifact_sha256"
            f" FROM {DERIVATIONS} WHERE derivation_id = d.derivation_id))))",
        ),
    )
    chain = f"FROM {MOVES} WHERE item_id = NEW.item_id"
    target = "NEW.selection_revision_id"
    _trigger(
        f"trg_{MOVES}_chain",
        "INSERT",
        MOVES,
        _raise(
            f"{MOVES}: the selection is an OPERATOR decision for this Item",
            f"NOT EXISTS (SELECT 1 FROM {SELECTIONS} s WHERE s.selection_revision_id = {target}"
            " AND s.item_id = NEW.item_id AND s.decision_origin = 'OPERATOR')",
        )
        + _raise(
            f"{MOVES}: a current selection is complete",
            f"(SELECT COUNT(*) FROM {DECISIONS} WHERE selection_revision_id = {target})"
            f" IS NOT {selection.format(column='decision_count', id=target)}"
            f" OR (SELECT COUNT(*) FROM {OUTPUTS} WHERE selection_revision_id = {target})"
            f" IS NOT {selection.format(column='output_count', id=target)}"
            f" OR (SELECT COUNT(*) FROM {DECISIONS} WHERE selection_revision_id = {target}"
            f" AND decision <> 'EXCLUDE') IS NOT {selection.format(column='output_count', id=target)}",
        )
        + _raise(
            f"{MOVES}: moves are appended in order",
            f"NEW.sequence <> (SELECT COALESCE(MAX(sequence), 0) + 1 {chain})",
        )
        + _raise(
            f"{MOVES}: a move starts from the current selection",
            "NEW.previous_selection_revision_id IS NOT"
            f" (SELECT selection_revision_id {chain} ORDER BY sequence DESC LIMIT 1)",
        )
        + _raise(
            f"{MOVES}: a move never returns to an earlier selection",
            f"EXISTS (SELECT 1 {chain} AND selection_revision_id = {target})",
        ),
    )
    _trigger(
        f"trg_{QA}_exact",
        "INSERT",
        QA,
        _raise(
            f"{QA}: a source verdict names a CONFIRMED image of its validated revision",
            f"NEW.asset_kind = 'SOURCE_ASSET' AND NOT EXISTS (SELECT 1 FROM {REFS} r"
            " WHERE r.revision_id = NEW.validated_source_revision_id"
            " AND r.sha256 = NEW.sha256 AND r.status = 'CONFIRMED')",
        )
        + _raise(
            f"{QA}: a derived verdict names the artifact of its derivation under that revision",
            f"NEW.asset_kind = 'DERIVED_ARTIFACT' AND NOT EXISTS (SELECT 1 FROM {DERIVATIONS} d"
            " WHERE d.derivation_id = NEW.derivation_id AND d.artifact_sha256 = NEW.sha256"
            " AND d.validated_source_revision_id = NEW.validated_source_revision_id)",
        )
        + _raise(
            f"{QA}: findings are bounded codes",
            "EXISTS (SELECT 1 FROM json_each(NEW.findings_json) j WHERE j.type <> 'text'"
            " OR length(j.value) > 64 OR j.value NOT GLOB '[A-Z]*'"
            " OR j.value GLOB '*[^A-Z0-9_]*')",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if held:
            raise RuntimeError(
                f"cannot drop {table}: {held} row(s) of image lineage, selection or QA history"
                " are held; that history is never silently destroyed"
            )
    for table in reversed(TABLES):
        op.drop_table(table)
