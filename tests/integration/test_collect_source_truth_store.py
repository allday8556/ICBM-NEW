"""COLLECT source-truth persistence (M3 PR-B, ADR-0010 §6–§9, Issue #52 §13), on a migrated
database with fake image decoding. No supplier is contacted."""

import contextlib
import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import CheckConstraint

from app.collect.assets import (
    DecodedImage,
    SourceAssetIntegrityError,
    SourceAssetStore,
    StoredAsset,
    UnsupportedSourceImageError,
)
from app.collect.facts import FIELD_REGISTRY, FactsStatus, TextValue
from app.collect.revisions import ProductFactsRevisionStore
from app.config import AppConfig
from app.core.errors import InputValidationError
from app.db.database import Database, create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from tests.collect_support import CAPTURED, PNG, REPRESENTATIVE, base_fields, collected, confirmed
from tests.support import FakeClock

pytestmark = pytest.mark.integration

SOURCE_TRUTH_TABLES = (
    "product_facts_revisions",
    "product_facts_fields",
    "product_facts_evidence",
    "source_assets",
    "product_facts_image_refs",
)


class FakeDecoder:
    """Recognises only the synthetic PNG signature; everything else is unsupported."""

    def decode(self, data: bytes) -> DecodedImage | None:
        return DecodedImage("image/png", 64, 64) if data.startswith(b"\x89PNG") else None


@pytest.fixture
def database(config: AppConfig) -> Iterator[Database]:
    db = Database(config.database_url)
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def revisions(database: Database, clock: FakeClock) -> ProductFactsRevisionStore:
    return ProductFactsRevisionStore(database, clock)


@pytest.fixture
def assets(config: AppConfig, database: Database, clock: FakeClock) -> SourceAssetStore:
    return SourceAssetStore(config.source_assets_dir, database, FakeDecoder(), clock)


@pytest.fixture
def images(assets: SourceAssetStore) -> tuple:
    return (replace(REPRESENTATIVE, sha256=assets.put(PNG).sha256),)


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(config.database_path)
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


# ---------------------------------------------------------------- revisions


def test_every_collection_appends_a_new_immutable_revision(
    revisions: ProductFactsRevisionStore, images: tuple
) -> None:
    stored = [
        revisions.append(
            collected(
                images=images,
                collection_run_id=f"run-{n}",
                captured_at=CAPTURED + timedelta(minutes=n),
            )
        )
        for n in (1, 2, 3)
    ]
    assert [revision.sequence for revision in stored] == [1, 2, 3]
    assert len({revision.revision_id for revision in stored}) == 3
    assert len({revision.source_fingerprint for revision in stored}) == 1  # unchanged source
    assert all(revision.fingerprints_intact() for revision in stored)
    history = revisions.history("kmretail", "1234")
    assert [revision.revision_id for revision in history] == [r.revision_id for r in stored]


def test_revision_history_survives_a_restart(
    config: AppConfig, database: Database, clock: FakeClock, images: tuple
) -> None:
    first = ProductFactsRevisionStore(database, clock).append(collected(images=images))
    database.dispose()
    reopened = Database(config.database_url)
    try:
        store = ProductFactsRevisionStore(reopened, clock)
        assert store.get(first.revision_id) == first
        again = store.append(collected(images=images, collection_run_id="run-after-restart"))
        assert (again.sequence, again.source_fingerprint) == (2, first.source_fingerprint)
        asset_store = SourceAssetStore(config.source_assets_dir, reopened, FakeDecoder(), clock)
        assert asset_store.read(images[0].sha256) == PNG
    finally:
        reopened.dispose()


def test_a_changed_name_stays_the_same_source_identity(
    revisions: ProductFactsRevisionStore, images: tuple
) -> None:
    first = revisions.append(collected(images=images))
    fields = base_fields()
    fields["original_name"] = confirmed(TextValue(text="바뀐 상품명"), ".name")
    second = revisions.append(collected(fields=fields, images=images))
    assert (second.supplier_key, second.source_product_id, second.sequence) == (
        "kmretail",
        "1234",
        2,
    )
    assert second.source_fingerprint != first.source_fingerprint
    assert second.fields["original_name"].fingerprint != first.fields["original_name"].fingerprint
    assert second.fields["prices"].fingerprint == first.fields["prices"].fingerprint
    assert revisions.get(first.revision_id) == first  # history is not rewritten


def test_read_back_is_exactly_what_was_collected(
    revisions: ProductFactsRevisionStore, images: tuple
) -> None:
    source = collected(images=images)
    stored = revisions.append(source)
    assert list(stored.fields) == list(FIELD_REGISTRY)
    for key, fact in source.fields.items():
        field = stored.fields[key]
        assert (field.status, field.value) == (fact.status, fact.value), key
        assert tuple(entry.evidence for entry in field.evidence) == fact.evidence, key
    assert stored.images == images
    assert (stored.facts_status, stored.currency) == (FactsStatus.CONFIRMED, "KRW")
    assert (stored.captured_at, stored.source_url) == (CAPTURED, source.source_url)


def test_an_image_reference_needs_stored_bytes(revisions: ProductFactsRevisionStore) -> None:
    with pytest.raises(InputValidationError) as caught:
        revisions.append(collected())  # its sha256 was never stored as a source asset
    assert caught.value.code == "COLLECT_IMAGE_ASSET_MISSING"
    assert revisions.history("kmretail", "1234") == ()


# ---------------------------------------------------------------- database guarantees


def test_every_source_truth_table_is_append_only(
    config: AppConfig, revisions: ProductFactsRevisionStore, images: tuple
) -> None:
    revisions.append(collected(images=images))
    with contextlib.closing(_raw(config)) as raw:
        for table in SOURCE_TRUTH_TABLES:
            assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"UPDATE {table} SET rowid = rowid")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"DELETE FROM {table}")


def test_the_database_refuses_forbidden_rows_even_without_the_domain(
    config: AppConfig, revisions: ProductFactsRevisionStore, images: tuple
) -> None:
    revision = revisions.append(collected(images=images)).revision_id
    at = "'2026-09-16 00:00:00'"
    forbidden = {
        "ck_product_facts_revisions_currency_krw": (
            "INSERT INTO product_facts_revisions VALUES ('r2', 'kmretail', '1234', 9, "
            f"'https://shop.example/p', {at}, {at}, 'USD', 'r1', '{'b' * 64}', '{'c' * 64}', "
            "'run', 'cid', 'CONFIRMED')"
        ),
        "ck_product_facts_fields_value_is_json": (
            f"INSERT INTO product_facts_fields VALUES ('{revision}', 'extra', 'COVERAGE', "
            f"'CONFIRMED', 'not json', '{'d' * 64}')"
        ),
        "ck_product_facts_fields_absent_has_no_value": (
            f"INSERT INTO product_facts_fields VALUES ('{revision}', 'extra', 'COVERAGE', "
            f"'ABSENT', '{{}}', '{'d' * 64}')"
        ),
        "ck_product_facts_evidence_observed_bounded": (
            f"INSERT INTO product_facts_evidence VALUES ('{revision}', 'prices', 9, 'DOM_TEXT', "
            f"'.price', '{'x' * 4097}', NULL, 'CONFIRMED', '{'e' * 64}')"
        ),
        "ck_product_facts_image_refs_status_matches_issue": (
            f"INSERT INTO product_facts_image_refs VALUES ('{revision}', 'DETAIL', 9, "
            "'img.shop.example', '.detail img', NULL, NULL, 'CONFIRMED', NULL, NULL, NULL)"
        ),
        "ck_source_assets_sha256_hex": (
            f"INSERT INTO source_assets VALUES ('{'F' * 64}', 'image/png', 1, 1, 1, {at})"
        ),
    }
    with contextlib.closing(_raw(config)) as raw:
        for constraint, statement in forbidden.items():
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                raw.execute(statement)


def test_source_truth_checks_match_the_orm(config: AppConfig) -> None:
    # The CHECK expressions of 0007 are frozen literals; they must equal the models' expressions.
    with contextlib.closing(_raw(config)) as raw:
        for table in SOURCE_TRUTH_TABLES:
            ddl = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            checks = [
                c for c in metadata.tables[table].constraints if isinstance(c, CheckConstraint)
            ]
            assert checks, table
            for check in checks:
                assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"


def test_0007_downgrade_never_drops_source_truth(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)
    db = Database(url)
    try:
        SourceAssetStore(tmp_path / "source-assets", db, FakeDecoder(), FakeClock()).put(PNG)
    finally:
        db.dispose()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0006_m2_marketplace_connections")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == head_revision() == "0007_m3_product_facts_revisions"
    finally:
        engine.dispose()


# ---------------------------------------------------------------- source assets


def test_source_assets_keep_the_original_bytes_once(
    config: AppConfig, assets: SourceAssetStore
) -> None:
    first = assets.put(PNG)
    second = assets.put(PNG)
    expected = StoredAsset(hashlib.sha256(PNG).hexdigest(), "image/png", len(PNG), 64, 64)
    assert first == second == expected
    path = config.source_assets_dir / "sha256" / first.sha256[:2] / first.sha256
    assert path.read_bytes() == PNG
    assert list(path.parent.iterdir()) == [path]  # no staging file left behind
    assert assets.read(first.sha256) == PNG
    with contextlib.closing(_raw(config)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0] == 1


def test_undecodable_bytes_are_refused_and_nothing_is_written(
    config: AppConfig, assets: SourceAssetStore
) -> None:
    html = b"<html>not an image</html>"
    with pytest.raises(UnsupportedSourceImageError):
        assets.put(html)
    with pytest.raises(UnsupportedSourceImageError):
        assets.put(b"")
    assert assets.get(hashlib.sha256(html).hexdigest()) is None
    written = list(config.source_assets_dir.rglob("*")) if config.source_assets_dir.exists() else []
    assert [path for path in written if path.is_file()] == []


def test_a_corrupted_asset_is_detected_and_repaired_only_by_matching_bytes(
    assets: SourceAssetStore,
) -> None:
    stored = assets.put(PNG)
    assets.path(stored.sha256).write_bytes(b"tampered")
    with pytest.raises(SourceAssetIntegrityError):
        assets.read(stored.sha256)
    assets.put(PNG)
    assert assets.read(stored.sha256) == PNG
