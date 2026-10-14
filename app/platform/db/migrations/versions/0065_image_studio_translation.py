"""ADR-0041 I1: the Image Studio translation owner — OCR observations, translation candidates,
edit revisions and their current pointer.

Revision ID: 0065_image_studio_translation
Revises: 0064_fx_snapshots
Create Date: 2026-10-14

ADR-0041 §3. It adds four tables and touches no existing table, row, trigger or index. No source
row is ever written (IT-01): every table only names a ``source_assets`` SHA-256.

- `image_ocr_observations`: one OCR reading of one source image, content-addressed by the source
  SHA-256, the OCR engine id and version and the preprocessing version (one reading per key, however
  many products use the image). Its regions are a JSON array: per region an id, the box
  (`[left, top, right, bottom]`) and an optional polygon, the source text, the script (`zh`, `ja`),
  the confidence and the background-uniformity score (both 0..1). It carries its fingerprint.
- `image_translation_candidates`: the AI's Korean suggestion per sent region of one observation,
  with the source language, the ordered ids of the regions sent, the digest of the relevant input,
  the composed `prompt_version` and `policy_version`, the requested identity, the output schema
  version, the translation fingerprint of ADR-0012 §6 / ADR-0026 (one candidate per fingerprint)
  and the provenance copied from the outcome. Only a successful translation is recorded.
- `image_edit_revisions`: the operator's saved edit of one image of one product, numbered from one
  by exactly one per lineage (product group, source SHA-256). Per region of its observation, exactly
  once: the final Korean text, its origin (`AI_SUGGESTION` or `OPERATOR_CONFIRMED`), include or
  keep-original, the erase decision (`ERASE`, `OVERLAY`; `NONE` for a kept region) and the style. It
  also names the renderer version, the font hashes and the content fingerprint.
- `image_edit_current`: the current revision of each lineage. It is the only updatable table, and
  only forward: it names its own lineage's newest revision, never changes lineage and is never
  deleted.

The database refuses an update or a delete of the first three, a malformed region or item, a
candidate that sends a region its observation does not hold or answers out of order, a revision
whose observation is another image's, a gap in a lineage's numbering, an `AI_SUGGESTION` item whose
text is not its candidate's, and a pointer that moves backwards or to another lineage.

**Every canonical table starts empty** (M0 acceptance): nothing is seeded.

**Downgrade fails closed** while any row exists in any of the four tables: an observation, a paid
suggestion and an operator's edit history are never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_image_studio_translation"
down_revision: str | None = "0064_fx_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVATIONS = "image_ocr_observations"
CANDIDATES = "image_translation_candidates"
REVISIONS = "image_edit_revisions"
CURRENT = "image_edit_current"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (OBSERVATIONS, CANDIDATES, REVISIONS, CURRENT)

# Frozen with this revision (``app.capabilities.image_studio.models``).
LANGUAGES = "source_language IN ('zh', 'ja')"
_UNIT = "IN ('integer', 'real')"
REGION_SHAPE = (
    "json_type(r.value) = 'object'"
    " AND json_type(r.value, '$.region_id') = 'text'"
    " AND length(json_extract(r.value, '$.region_id')) > 0"
    " AND json_type(r.value, '$.text') = 'text'"
    " AND json_extract(r.value, '$.script') IN ('zh', 'ja')"
    f" AND json_type(r.value, '$.confidence') {_UNIT}"
    " AND json_extract(r.value, '$.confidence') BETWEEN 0 AND 1"
    f" AND json_type(r.value, '$.background_uniformity') {_UNIT}"
    " AND json_extract(r.value, '$.background_uniformity') BETWEEN 0 AND 1"
    " AND json_type(r.value, '$.box') = 'array'"
    " AND json_array_length(r.value, '$.box') = 4"
    " AND json_type(r.value, '$.polygon') IN ('null', 'array')"
)
ITEM_SHAPE = (
    "json_type(i.value) = 'object'"
    " AND json_type(i.value, '$.region_id') = 'text'"
    " AND ((json_type(i.value, '$.include') = 'true'"
    " AND json_type(i.value, '$.text') = 'text' AND length(json_extract(i.value, '$.text')) > 0"
    " AND json_extract(i.value, '$.origin') IN ('AI_SUGGESTION', 'OPERATOR_CONFIRMED')"
    " AND json_extract(i.value, '$.erase') IN ('ERASE', 'OVERLAY')"
    " AND json_type(i.value, '$.style') = 'object')"
    " OR (json_type(i.value, '$.include') = 'false'"
    " AND json_type(i.value, '$.text') = 'null' AND json_type(i.value, '$.origin') = 'null'"
    " AND json_extract(i.value, '$.erase') = 'NONE' AND json_type(i.value, '$.style') = 'null'))"
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


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))


def upgrade() -> None:
    op.create_table(
        OBSERVATIONS,
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("ocr_engine_id", sa.String(length=64), nullable=False),
        sa.Column("ocr_engine_version", sa.String(length=64), nullable=False),
        sa.Column("preprocessing_version", sa.String(length=64), nullable=False),
        sa.Column("regions_json", sa.Text(), nullable=False),
        sa.Column("observation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        _check(OBSERVATIONS, _hex64("source_sha256"), "source_sha256_hex"),
        _check(OBSERVATIONS, "ocr_engine_id <> ''", "engine_id_present"),
        _check(OBSERVATIONS, "ocr_engine_version <> ''", "engine_version_present"),
        _check(OBSERVATIONS, "preprocessing_version <> ''", "preprocessing_version_present"),
        _check(OBSERVATIONS, _json("regions_json", "array"), "regions_is_array"),
        _check(OBSERVATIONS, _hex64("observation_fingerprint"), "fingerprint_hex"),
        _check(OBSERVATIONS, "correlation_id <> ''", "correlation_present"),
        _fk(OBSERVATIONS, "source_sha256", "source_assets", "sha256"),
        sa.PrimaryKeyConstraint("observation_id", name=op.f(f"pk_{OBSERVATIONS}")),
        sa.UniqueConstraint(
            "source_sha256",
            "ocr_engine_id",
            "ocr_engine_version",
            "preprocessing_version",
            name=op.f(
                f"uq_{OBSERVATIONS}_source_sha256_ocr_engine_id_ocr_engine_version"
                "_preprocessing_version"
            ),
        ),
    )
    op.create_table(
        CANDIDATES,
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("source_language", sa.String(length=2), nullable=False),
        sa.Column("sent_region_ids_json", sa.Text(), nullable=False),
        sa.Column("relevant_input", sa.String(length=64), nullable=False),
        sa.Column("prompt_version_json", sa.Text(), nullable=False),
        sa.Column("policy_version_json", sa.Text(), nullable=False),
        sa.Column("requested_identity_json", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.String(length=64), nullable=False),
        sa.Column("translation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("translations_json", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(CANDIDATES, LANGUAGES, "source_language_known"),
        _check(CANDIDATES, _json("sent_region_ids_json", "array"), "sent_is_array"),
        _check(CANDIDATES, "json_array_length(sent_region_ids_json) > 0", "sent_not_empty"),
        _check(CANDIDATES, _hex64("relevant_input"), "relevant_input_hex"),
        _check(CANDIDATES, _json("prompt_version_json", "object"), "prompt_version_is_object"),
        _check(CANDIDATES, _json("policy_version_json", "object"), "policy_version_is_object"),
        _check(
            CANDIDATES, _json("requested_identity_json", "object"), "requested_identity_is_object"
        ),
        _check(CANDIDATES, "output_schema_version <> ''", "schema_version_present"),
        _check(CANDIDATES, _hex64("translation_fingerprint"), "fingerprint_hex"),
        _check(CANDIDATES, _json("translations_json", "array"), "translations_is_array"),
        _check(
            CANDIDATES,
            "json_array_length(translations_json) = json_array_length(sent_region_ids_json)",
            "one_translation_per_sent_region",
        ),
        _check(CANDIDATES, _json("provenance_json", "object"), "provenance_is_object"),
        _check(CANDIDATES, "correlation_id <> ''", "correlation_present"),
        _fk(CANDIDATES, "observation_id", OBSERVATIONS, "observation_id"),
        sa.PrimaryKeyConstraint("candidate_id", name=op.f(f"pk_{CANDIDATES}")),
        sa.UniqueConstraint(
            "translation_fingerprint", name=op.f(f"uq_{CANDIDATES}_translation_fingerprint")
        ),
    )
    op.create_index(
        op.f(f"ix_{CANDIDATES}_observation_id"), CANDIDATES, ["observation_id"], unique=False
    )
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("items_json", sa.Text(), nullable=False),
        sa.Column("renderer_version", sa.String(length=64), nullable=False),
        sa.Column("font_hashes_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "seq >= 1", "seq_positive"),
        _check(REVISIONS, _hex64("source_sha256"), "source_sha256_hex"),
        _check(REVISIONS, _json("items_json", "array"), "items_is_array"),
        _check(REVISIONS, "renderer_version <> ''", "renderer_version_present"),
        _check(REVISIONS, _json("font_hashes_json", "object"), "font_hashes_is_object"),
        _check(REVISIONS, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(REVISIONS, "authored_by <> ''", "authored_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        _fk(REVISIONS, "product_group_id", "product_groups", "product_group_id"),
        _fk(REVISIONS, "source_sha256", "source_assets", "sha256"),
        _fk(REVISIONS, "observation_id", OBSERVATIONS, "observation_id"),
        _fk(REVISIONS, "candidate_id", CANDIDATES, "candidate_id"),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint(
            "product_group_id",
            "source_sha256",
            "seq",
            name=op.f(f"uq_{REVISIONS}_product_group_id_source_sha256_seq"),
        ),
    )
    op.create_table(
        CURRENT,
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(CURRENT, "moved_by <> ''", "moved_by_present"),
        _check(CURRENT, "correlation_id <> ''", "correlation_present"),
        _fk(CURRENT, "revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("product_group_id", "source_sha256", name=op.f(f"pk_{CURRENT}")),
    )
    _install_triggers()


def _install_triggers() -> None:
    _trigger(
        OBSERVATIONS,
        "regions_shape",
        "INSERT",
        _raise(
            f"{OBSERVATIONS}: every region has its id, box, text, script and scores",
            f"EXISTS (SELECT 1 FROM json_each(NEW.regions_json) r WHERE NOT ({REGION_SHAPE}))"
            " OR (SELECT COUNT(DISTINCT json_extract(r.value, '$.region_id'))"
            " FROM json_each(NEW.regions_json) r) <> json_array_length(NEW.regions_json)",
        ),
    )
    _immutable(OBSERVATIONS)
    observed = (
        f"SELECT json_extract(r.value, '$.region_id') FROM {OBSERVATIONS} o,"
        " json_each(o.regions_json) r WHERE o.observation_id = NEW.observation_id"
    )
    _trigger(
        CANDIDATES,
        "sends_observed_regions",
        "INSERT",
        _raise(
            f"{CANDIDATES}: a candidate sends distinct regions of its own observation",
            "EXISTS (SELECT 1 FROM json_each(NEW.sent_region_ids_json) s"
            f" WHERE s.type <> 'text' OR s.value NOT IN ({observed}))"
            " OR (SELECT COUNT(DISTINCT s.value) FROM json_each(NEW.sent_region_ids_json) s)"
            " <> json_array_length(NEW.sent_region_ids_json)",
        ),
    )
    _trigger(
        CANDIDATES,
        "answers_in_order",
        "INSERT",
        _raise(
            f"{CANDIDATES}: one Korean text per sent region, in the order sent",
            "EXISTS (SELECT 1 FROM json_each(NEW.translations_json) t"
            " WHERE json_type(t.value) <> 'object' OR json_type(t.value, '$.text') <> 'text'"
            " OR json_extract(t.value, '$.region_id')"
            " IS NOT json_extract(NEW.sent_region_ids_json, '$[' || t.key || ']'))",
        ),
    )
    _immutable(CANDIDATES)
    _trigger(
        REVISIONS,
        "seq_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(r.seq), 0) FROM {REVISIONS} r"
            " WHERE r.product_group_id = NEW.product_group_id"
            " AND r.source_sha256 = NEW.source_sha256)",
        ),
    )
    _trigger(
        REVISIONS,
        "observation_is_own_image",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision edits an observation of its own image",
            f"NEW.source_sha256 IS NOT (SELECT o.source_sha256 FROM {OBSERVATIONS} o"
            " WHERE o.observation_id = NEW.observation_id)",
        ),
    )
    _trigger(
        REVISIONS,
        "candidate_is_own_observation",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision starts from a candidate of its own observation",
            "NEW.candidate_id IS NOT NULL AND NEW.observation_id IS NOT"
            f" (SELECT c.observation_id FROM {CANDIDATES} c"
            " WHERE c.candidate_id = NEW.candidate_id)",
        ),
    )
    items = "SELECT 1 FROM json_each(NEW.items_json) i"
    _trigger(
        REVISIONS,
        "items_cover_the_observation",
        "INSERT",
        _raise(
            f"{REVISIONS}: one well-formed item per region of its observation",
            f"EXISTS ({items} WHERE NOT ({ITEM_SHAPE}))"
            f" OR EXISTS ({items} WHERE json_extract(i.value, '$.region_id') NOT IN ({observed}))"
            " OR (SELECT COUNT(DISTINCT json_extract(i.value, '$.region_id'))"
            " FROM json_each(NEW.items_json) i) <> json_array_length(NEW.items_json)"
            " OR json_array_length(NEW.items_json) IS NOT"
            f" (SELECT json_array_length(o.regions_json) FROM {OBSERVATIONS} o"
            " WHERE o.observation_id = NEW.observation_id)",
        ),
    )
    # An AI_SUGGESTION item is exactly its candidate's text for that region (IT-06): any other
    # text is the operator's, and says so.
    _trigger(
        REVISIONS,
        "suggestion_is_the_candidates",
        "INSERT",
        _raise(
            f"{REVISIONS}: an AI_SUGGESTION item carries its candidate text unchanged",
            f"EXISTS ({items} WHERE json_extract(i.value, '$.origin') = 'AI_SUGGESTION'"
            f" AND NOT EXISTS (SELECT 1 FROM {CANDIDATES} c, json_each(c.translations_json) t"
            " WHERE c.candidate_id = NEW.candidate_id"
            " AND json_extract(t.value, '$.region_id') = json_extract(i.value, '$.region_id')"
            " AND json_extract(t.value, '$.text') = json_extract(i.value, '$.text')))",
        ),
    )
    _immutable(REVISIONS)
    newest = (
        f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
        " WHERE r.revision_id = NEW.revision_id"
        " AND r.product_group_id = NEW.product_group_id AND r.source_sha256 = NEW.source_sha256"
        f" AND r.seq = (SELECT MAX(m.seq) FROM {REVISIONS} m"
        " WHERE m.product_group_id = NEW.product_group_id"
        " AND m.source_sha256 = NEW.source_sha256))"
    )
    _trigger(
        CURRENT,
        "names_newest_on_insert",
        "INSERT",
        _raise(f"{CURRENT}: the current revision is the newest of its own lineage", newest),
    )
    _trigger(
        CURRENT,
        "same_lineage",
        "UPDATE",
        _raise(
            f"{CURRENT}: a pointer never changes lineage",
            "NEW.product_group_id <> OLD.product_group_id OR NEW.source_sha256 <> OLD.source_sha256",
        ),
    )
    _trigger(
        CURRENT,
        "moves_forward",
        "UPDATE",
        _raise(
            f"{CURRENT}: a pointer only moves forward",
            f"(SELECT r.seq FROM {REVISIONS} r WHERE r.revision_id = NEW.revision_id)"
            f" <= (SELECT r.seq FROM {REVISIONS} r WHERE r.revision_id = OLD.revision_id)",
        ),
    )
    _trigger(
        CURRENT,
        "names_newest_on_update",
        "UPDATE",
        _raise(f"{CURRENT}: the current revision is the newest of its own lineage", newest),
    )
    _trigger(CURRENT, "no_delete", "DELETE", _raise(f"a {CURRENT} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        counts = ", ".join(f"{table}={count}" for table, count in held.items() if count)
        raise RuntimeError(
            f"cannot drop the Image Studio translation owner: rows are held ({counts});"
            " an observation, a paid suggestion and an operator's edit history are never"
            " silently destroyed"
        )
    op.drop_index(op.f(f"ix_{CANDIDATES}_observation_id"), table_name=CANDIDATES)
    for table in reversed(CREATED):
        op.drop_table(table)
