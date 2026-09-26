"""The schema contract (ADR-0018 §7, §8): one canonical manifest, whatever order SQLite stored."""

from app.db.schema_contract import SchemaManifest, canonical_table_sql, manifest_of

TABLE = (
    'CREATE TABLE "t" ( id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL, note TEXT,'
    " CONSTRAINT pk_t PRIMARY KEY (id),"
    " CONSTRAINT ck_t_state CHECK (state IN ('A', 'B, C', 'D)')),"
    " CONSTRAINT ck_t_note CHECK (note IS NULL OR length(note) > 0) )"
)
REORDERED = (
    'CREATE TABLE "t" ( id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL, note TEXT,'
    " CONSTRAINT ck_t_note CHECK (note IS NULL OR length(note) > 0),"
    " CONSTRAINT pk_t PRIMARY KEY (id),"
    " CONSTRAINT ck_t_state CHECK (state IN ('A', 'B, C', 'D)')) )"
)


def test_the_order_of_table_constraints_is_canonical_and_nothing_else_is() -> None:
    assert canonical_table_sql(TABLE) == canonical_table_sql(REORDERED)
    # A comma or a parenthesis inside a literal never splits a clause.
    assert "CHECK (state IN ('A', 'B, C', 'D)'))" in canonical_table_sql(TABLE)
    # Columns keep their order, and a changed clause is a changed table.
    swapped = TABLE.replace(
        "id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL",
        "state VARCHAR(20) NOT NULL, id VARCHAR(40) NOT NULL",
    )
    assert canonical_table_sql(swapped) != canonical_table_sql(TABLE)
    weakened = TABLE.replace("length(note) > 0", "1")
    assert canonical_table_sql(weakened) != canonical_table_sql(TABLE)


def test_a_manifest_names_every_missing_changed_and_unexpected_object() -> None:
    expected = manifest_of(
        [
            ("table", "t", "t", TABLE),
            ("index", "ix_t_state", "t", "CREATE INDEX ix_t_state ON t (state)"),
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t BEFORE DELETE ON t BEGIN SELECT 1; END",
            ),
        ]
    )
    same = manifest_of(
        [
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t  BEFORE DELETE ON t\n BEGIN SELECT 1; END",
            ),
            ("table", "t", "t", REORDERED),
            ("index", "ix_t_state", "t", "CREATE INDEX ix_t_state ON t (state)"),
        ]
    )
    assert expected.differences(same) == [] and expected.digest == same.digest
    damaged = manifest_of(
        [
            ("table", "t", "t", TABLE),
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t BEFORE DELETE ON t BEGIN SELECT 2; END",
            ),
            (
                "trigger",
                "trg_x",
                "t",
                "CREATE TRIGGER trg_x BEFORE INSERT ON t BEGIN SELECT 1; END",
            ),
        ]
    )
    assert expected.differences(damaged) == [
        "missing:index:ix_t_state",
        "changed:trigger:trg_t",
        "unexpected:trigger:trg_x",
    ]
    assert expected.digest != damaged.digest
    assert SchemaManifest({}).differences(SchemaManifest({})) == []
