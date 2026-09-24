"""The durable operator-reviewed category metadata over the real application (Gate 1 G1-B,
ADR-0015 §3, authorization 5788082735).

What this proves, from the outside in:
- a save appends one server-created revision, with server-owned review provenance, and moves the
  key's one explicit current pointer; nothing is rewritten or deleted, not even by SQL;
- the server creates the revision identity, the fingerprint and the review time, and refuses a
  client that tries; an AI suggestion is never recorded as reviewed;
- an invalid, unsafe or unproven structure is refused whole: no key, revision, pointer or audit row;
- marketplace × taxonomy revision × category are strictly isolated;
- the history survives a restart exactly, review provenance included;
- the production preflight reads this owner: no current revision → `CATEGORY_METADATA_MISSING`,
  an unreviewed current revision → `CATEGORY_METADATA_UNREVIEWED` with no older-reviewed fallback,
  a reviewed current revision materializes the exact `CategoryMetadata`, a change of the current
  revision stales an earlier candidate, and a frozen Snapshot keeps the revision it froze.

No provider is reached: category metadata is operator-recorded local truth.
"""

import contextlib
import importlib.util
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.audit.models import AuditEventType
from app.config import AppConfig
from app.container import Container
from app.db.database import create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.main import create_app
from app.products.model import ReadinessStatus
from app.register import category_metadata_models as models
from app.register.builder import RegistrationSnapshotBuilder
from app.register.category_metadata import (
    CategoryMetadataService,
    DurableRegistrationMetadata,
    MetadataRevisionRecord,
    RecordRevisionRequest,
    category_metadata_of,
    effectively_reviewed,
)
from app.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
from app.register.preflight import RegistrationPreflightService
from tests.conftest import LOCAL
from tests.product_support import Collections, raw
from tests.register_support import (
    CATEGORY,
    CID,
    MARKET,
    OPERATOR,
    TAXONOMY,
    FakeCapability,
    draft,
    establish,
    metadata,
    no_match,
    prepared,
    ready_item,
    request,
    target,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/settings/category-metadata"
KNOWN = "smartstore"
KEYS = "registration_category_metadata"
REVISIONS = "registration_category_metadata_revisions"
CURRENT = "registration_category_metadata_current"
TABLES = (KEYS, REVISIONS, CURRENT)


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


def content(**overrides: Any) -> dict[str, Any]:
    """Exactly `register_support.metadata()`, as reviewed content."""
    values: dict[str, Any] = {
        "leaf": True,
        "registrable": True,
        "name_max_length": 100,
        "attributes": [
            _rule("brand", required=True),
            _rule("color", required=False),
        ],
        "notice": {
            "notice_type": "notice-test-1",
            "fields": [
                _rule("manufacturer", required=True),
                _rule("origin", required=True, detail_page_reference_allowed=True),
            ],
        },
        "options": {"options_supported": True, "max_options": 5, "max_dimensions": 1},
        "required_templates": ["returns", "shipping"],
    }
    values.update(overrides)
    return values


def _rule(key: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": key,
        "required": False,
        "detail_page_reference_allowed": False,
        "missing_status": "REVIEW_REQUIRED",
        "max_length": None,
    }
    values.update(overrides)
    return values


def body(
    *,
    reviewed: bool = True,
    provenance: str = "OPERATOR_CONFIRMED",
    evidence: str = "seller-center/category-50000803/2026-09-23",
    expected: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "actor": OPERATOR,
        "expected_current_revision": expected,
        "content_provenance": provenance,
        "evidence_reference": evidence,
        "reviewed": reviewed,
        "content": content(**overrides),
    }


def save(
    api: TestClient,
    payload: dict[str, Any],
    *,
    marketplace: str = KNOWN,
    taxonomy: str = TAXONOMY,
    category: str = CATEGORY,
) -> Any:
    return api.post(
        f"{BASE}/{marketplace}/{taxonomy}/{category}/revisions", json=payload, headers=CLIENT
    )


def counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def events(container: Container) -> list[Any]:
    return [
        event
        for event in container.audit.list_events(limit=500)
        if event.event_type == AuditEventType.REGISTRATION_CATEGORY_METADATA_RECORDED
    ]


def _none() -> dict[str, int]:
    return dict.fromkeys(TABLES, 0)


# ------------------------------------------------------------------ the owner and its history


def test_an_unreviewed_then_a_reviewed_revision_persist_with_their_review_provenance(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    empty = api.get(f"{BASE}/{KNOWN}/{TAXONOMY}/{CATEGORY}", headers=CLIENT).json()
    assert empty["editable"] is True and empty["current"] is None and empty["history"] == []

    first = save(api, body(reviewed=False, provenance="AI_SUGGESTION"))
    assert first.status_code == 200, first.text
    suggested = first.json()["current"]
    assert suggested["revision_no"] == 1
    assert suggested["reviewed"] is False
    assert suggested["reviewed_by"] is None and suggested["reviewed_at"] is None
    assert suggested["content_provenance"] == "AI_SUGGESTION"
    assert len(suggested["content_fingerprint"]) == 64

    second = save(api, body(expected=suggested["metadata_revision"])).json()
    reviewed = second["current"]
    assert reviewed["revision_no"] == 2 and reviewed["reviewed"] is True
    assert reviewed["reviewed_by"] == OPERATOR and reviewed["reviewed_at"] is not None
    assert reviewed["evidence_reference"] == "seller-center/category-50000803/2026-09-23"
    assert second["content"] == content()
    history = second["history"]
    assert [(h["revision_no"], h["reviewed"], h["current"]) for h in history] == [
        (2, True, True),
        (1, False, False),
    ]
    # The earlier revision is exactly as it was recorded.
    assert history[1]["content_fingerprint"] == suggested["content_fingerprint"]
    assert history[1]["reviewed_by"] is None
    assert counts(config) == {KEYS: 1, REVISIONS: 2, CURRENT: 1}
    recorded = events(container)
    assert [e.after["revision_no"] for e in reversed(recorded)] == [1, 2]
    assert "brand" not in str([e.model_dump() for e in recorded])


def test_an_ai_suggestion_is_never_recorded_as_reviewed(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    refused = save(api, body(reviewed=True, provenance="AI_SUGGESTION"))
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "CATEGORY_METADATA_AI_NOT_REVIEWABLE"
    assert counts(config) == _none() and events(container) == []


def test_marketplace_taxonomy_and_category_are_isolated(
    api: TestClient, container: Container
) -> None:
    saved = save(api, body()).json()["current"]["metadata_revision"]
    source = container.registration_preflight
    assert source.category_metadata(KNOWN, TAXONOMY, CATEGORY) is not None
    for marketplace, taxonomy, category in (
        ("coupang", TAXONOMY, CATEGORY),
        (KNOWN, "taxonomy-test-2", CATEGORY),
        (KNOWN, TAXONOMY, "category-test-2"),
    ):
        view = api.get(f"{BASE}/{marketplace}/{taxonomy}/{category}", headers=CLIENT).json()
        assert view["current"] is None and view["history"] == []
        assert source.category_metadata(marketplace, taxonomy, category) is None
    listed = api.get(f"{BASE}/{KNOWN}", headers=CLIENT).json()["entries"]
    assert [(e["taxonomy_revision"], e["category_id"]) for e in listed] == [(TAXONOMY, CATEGORY)]
    assert listed[0]["current"]["metadata_revision"] == saved
    assert api.get(f"{BASE}/coupang", headers=CLIENT).json()["entries"] == []
    unknown = api.get(f"{BASE}/nowhere/{TAXONOMY}/{CATEGORY}", headers=CLIENT)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "CATEGORY_METADATA_MARKETPLACE_UNKNOWN"


def test_the_history_and_review_provenance_survive_a_restart_exactly(config: AppConfig) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        first = save(client, body(reviewed=False)).json()["current"]["metadata_revision"]
        saved = save(client, body(expected=first)).json()
    with TestClient(create_app(config), base_url=LOCAL) as client:
        again = client.get(f"{BASE}/{KNOWN}/{TAXONOMY}/{CATEGORY}", headers=CLIENT).json()
        restarted: Container = client.app.state.container
        materialized = restarted.registration_preflight.category_metadata(KNOWN, TAXONOMY, CATEGORY)
    assert again == saved
    assert materialized is not None and materialized.reviewed is True
    assert materialized.metadata_revision == saved["current"]["metadata_revision"]


# ------------------------------------------------------------------ the server owns the identity


@pytest.mark.parametrize(
    "extra",
    [
        {"metadata_revision": "client-made"},
        {"content_fingerprint": "f" * 64},
        {"reviewed_by": "someone"},
        {"reviewed_at": "2026-09-23T00:00:00Z"},
    ],
)
def test_a_client_supplied_identity_or_review_provenance_is_refused(
    api: TestClient, config: AppConfig, extra: dict[str, str]
) -> None:
    at_request = save(api, {**body(), **extra})
    in_content = save(api, {**body(), "content": {**content(), **extra}})
    assert at_request.status_code == 422 and in_content.status_code == 422
    assert counts(config) == _none()


def _without(key: str) -> dict[str, Any]:
    payload = body()
    del payload["content"][key]
    return payload


INVALID = {
    "a missing field is never defaulted": (_without("options"), None),
    "a string where an integer is required": (body(name_max_length="100"), None),
    "an unknown missing status": (body(attributes=[_rule("brand", missing_status="READY")]), None),
    "an evidence reference that is a URL": (
        body(evidence="https://example.invalid/category"),
        "CATEGORY_METADATA_INVALID",
    ),
    "a rule key listed twice": (
        body(attributes=[_rule("brand"), _rule("brand")]),
        "CATEGORY_METADATA_INVALID",
    ),
    "a length limit of zero": (
        body(attributes=[_rule("brand", max_length=0)]),
        "CATEGORY_METADATA_INVALID",
    ),
    "options that contradict their own support flag": (
        body(options={"options_supported": False, "max_options": 3, "max_dimensions": 1}),
        "CATEGORY_METADATA_INVALID",
    ),
    "a name length limit of zero": (body(name_max_length=0), "CATEGORY_METADATA_INVALID"),
    "a template listed twice": (
        body(required_templates=["shipping", "shipping"]),
        "CATEGORY_METADATA_INVALID",
    ),
    "a URL as an attribute key": (
        body(attributes=[_rule("https://example.invalid/brand")]),
        "CATEGORY_METADATA_INVALID",
    ),
    "a bearer credential as a notice type": (
        body(notice={"notice_type": "Bearer abcdefgh12345678", "fields": []}),
        "CATEGORY_METADATA_INVALID",
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_save_is_refused_whole_and_writes_nothing(
    api: TestClient, container: Container, config: AppConfig, case: str
) -> None:
    payload, code = INVALID[case]
    refused = save(api, payload)
    assert refused.status_code == 422, refused.text
    if code is not None:
        assert refused.json()["error"]["code"] == code
    assert counts(config) == _none() and events(container) == []
    current = save(api, body()).json()["current"]["metadata_revision"]
    again = save(api, {**payload, "expected_current_revision": current})
    assert again.status_code == 422
    assert counts(config) == {KEYS: 1, REVISIONS: 1, CURRENT: 1}
    assert len(events(container)) == 1


def test_an_invalid_identifier_is_refused(api: TestClient, config: AppConfig) -> None:
    for taxonomy, category in (("-taxonomy", CATEGORY), (TAXONOMY, "category:1")):
        refused = save(api, body(), taxonomy=taxonomy, category=category)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "CATEGORY_METADATA_INVALID"
    assert counts(config) == _none()


def test_a_save_against_a_moved_or_identical_revision_is_refused(
    api: TestClient, config: AppConfig
) -> None:
    first = save(api, body()).json()["current"]["metadata_revision"]
    moved = save(api, body(name_max_length=90))
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == "CATEGORY_METADATA_CURRENT_MOVED"
    same = save(api, body(expected=first))
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "CATEGORY_METADATA_UNCHANGED"
    assert counts(config) == {KEYS: 1, REVISIONS: 1, CURRENT: 1}


# ------------------------------------------------------------------ the database keeps the rules


def test_the_database_refuses_to_rewrite_history_or_move_the_pointer_back(
    api: TestClient, config: AppConfig
) -> None:
    first = save(api, body(reviewed=False)).json()["current"]["metadata_revision"]
    second = save(api, body(expected=first)).json()["current"]["metadata_revision"]
    other = save(api, body(), category="category-test-2").json()["current"]["metadata_revision"]
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            f"UPDATE {CURRENT} SET metadata_revision_id = '{first}'"
            f" WHERE metadata_revision_id = '{second}'",
            f"UPDATE {CURRENT} SET metadata_revision_id = '{other}'"
            f" WHERE metadata_revision_id = '{second}'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="newest of its own key"):
                connection.execute(statement)
        statements = {
            "never updated": f"UPDATE {REVISIONS} SET recorded_by = 'x'",
            "never deleted": f"DELETE FROM {REVISIONS}",
            f"{CURRENT} row is never deleted": f"DELETE FROM {CURRENT}",
            f"{KEYS} is never updated": f"UPDATE {KEYS} SET created_by = 'x'",
            f"{KEYS} is never deleted": f"DELETE FROM {KEYS}",
            "a revision follows the one before it": (
                f"INSERT INTO {REVISIONS} SELECT 'r-gap', metadata_id, revision_no + 5,"
                " content_json, content_fingerprint, reviewed, reviewed_by, reviewed_at,"
                f" recorded_by, correlation_id, recorded_at FROM {REVISIONS}"
                f" WHERE metadata_revision_id = '{second}'"
            ),
            "names another marketplace, taxonomy or category": (
                f"INSERT INTO {REVISIONS} SELECT 'r-cross', r.metadata_id, 3,"
                f" (SELECT content_json FROM {REVISIONS} WHERE metadata_revision_id = '{other}'),"
                " r.content_fingerprint, r.reviewed, r.reviewed_by, r.reviewed_at,"
                f" r.recorded_by, r.correlation_id, r.recorded_at FROM {REVISIONS} r"
                f" WHERE r.metadata_revision_id = '{second}'"
            ),
            "review_provenance": (
                f"INSERT INTO {REVISIONS} SELECT 'r-anon', metadata_id, 3, content_json,"
                " content_fingerprint, 1, NULL, NULL, recorded_by, correlation_id, recorded_at"
                f" FROM {REVISIONS} WHERE metadata_revision_id = '{second}'"
            ),
        }
        for message, statement in statements.items():
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement)
    assert counts(config) == {KEYS: 2, REVISIONS: 3, CURRENT: 2}


def test_0020_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)
    before = _tables(tmp_path / "icbm.db")
    command.downgrade(alembic_config(url), "0019_g1_registration_target_policy")
    # 0020 owns exactly these three; the ReviewItem tables 0021 adds step down with it.
    assert before - _tables(tmp_path / "icbm.db") == set(TABLES) | {
        "review_items",
        "review_item_events",
        "review_coverage",
    }
    command.upgrade(alembic_config(url), "head")
    assert _tables(tmp_path / "icbm.db") == before
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            f"INSERT INTO {KEYS} VALUES ('meta-1', ?, ?, ?, 'o', 'c', '2026-09-23 00:00:00')",
            (KNOWN, TAXONOMY, CATEGORY),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0019_g1_registration_target_policy")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0020_g1_registration_category_metadata"
    finally:
        engine.dispose()


def _tables(database: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


# ------------------------------------------------------------------ the preflight reads this owner


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def owner(container: Container) -> CategoryMetadataService:
    """The served store, with the synthetic test marketplace known to it (the application knows
    only the real marketplace identities)."""
    return CategoryMetadataService(container.category_metadata._store, frozenset({MARKET}))


def _record(owner: CategoryMetadataService, expected: str | None, **overrides: Any) -> str:
    view = owner.record(
        MARKET,
        TAXONOMY,
        CATEGORY,
        RecordRevisionRequest.model_validate(body(expected=expected, **overrides)),
        correlation_id=CID,
    )
    assert view.current is not None
    return view.current.metadata_revision


def _served_preflight(container: Container, account: str) -> RegistrationPreflightService:
    """The application's own preflight with the durable metadata source it was built with. Only
    the target policy and the capability are test sources here; the metadata is never replaced."""
    served = container.registration_preflight
    assert isinstance(served._metadata, DurableRegistrationMetadata)
    served._policies = StaticRegistrationPolicy((target(account),))
    served._capability = FakeCapability()
    return served


def _codes(result: Any) -> set[str]:
    return {reason.code for reason in result.reasons}


def test_a_reviewed_current_revision_materializes_the_exact_category_metadata(
    container: Container, owner: CategoryMetadataService
) -> None:
    revision = _record(owner, None)
    materialized = DurableRegistrationMetadata(owner._store).category(MARKET, TAXONOMY, CATEGORY)
    assert materialized == metadata(metadata_revision=revision)
    assert container.registration_preflight.category_metadata(MARKET, TAXONOMY, CATEGORY) == (
        materialized
    )


def test_the_preflight_fails_closed_on_missing_and_unreviewed_current_metadata(
    container: Container, config: AppConfig, account: str, owner: CategoryMetadataService
) -> None:
    preflight = _served_preflight(container, account)
    item = ready_item(container, Collections.of(container, config))
    store = container.registrations
    req = request(store, draft(store, account, [item]), account, [item])

    assert "CATEGORY_METADATA_MISSING" in _codes(preflight.candidate(req))

    reviewed = _record(owner, None)
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    ready = preflight.candidate(req)
    assert ready.status is ReadinessStatus.READY, ready.reasons
    assert ready.resolved.metadata is not None
    assert ready.resolved.metadata.metadata_revision == reviewed

    # The current revision becomes unreviewed: no fallback to the older reviewed one.
    unreviewed = _record(owner, reviewed, reviewed=False)
    after = preflight.candidate(req)
    assert "CATEGORY_METADATA_UNREVIEWED" in _codes(after)
    assert after.status is not ReadinessStatus.READY
    assert after.resolved.metadata is not None
    assert after.resolved.metadata.metadata_revision == unreviewed


def test_a_new_current_revision_stales_an_earlier_candidate(
    container: Container, config: AppConfig, account: str, owner: CategoryMetadataService
) -> None:
    preflight = _served_preflight(container, account)
    first = _record(owner, None)
    item = ready_item(container, Collections.of(container, config))
    store = container.registrations
    req = request(store, draft(store, account, [item]), account, [item])
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    candidate = preflight.candidate(req)
    assets = prepared(candidate)
    assert preflight.final(req, assets).status is ReadinessStatus.READY

    _record(owner, first, name_max_length=90)
    assert preflight.candidate(req).candidate_fingerprint != candidate.candidate_fingerprint
    stale = preflight.final(req, assets)
    assert stale.status is ReadinessStatus.STALE
    assert "PREPARED_ASSET_CANDIDATE_MISMATCH" in _codes(stale)


def test_a_frozen_snapshot_keeps_the_metadata_revision_it_froze(
    container: Container, config: AppConfig, account: str, owner: CategoryMetadataService
) -> None:
    preflight = _served_preflight(container, account)
    first = _record(owner, None)
    item = ready_item(container, Collections.of(container, config))
    store = container.registrations
    req = request(store, draft(store, account, [item]), account, [item])
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    final = preflight.final(req, prepared(preflight.candidate(req)))
    assert final.status is ReadinessStatus.READY, final.reasons
    builder = RegistrationSnapshotBuilder(preflight=preflight, registrations=store)
    snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)

    _record(owner, first, name_max_length=90)
    with contextlib.closing(raw(config)) as connection:
        frozen = connection.execute(
            "SELECT json_extract(policy_revisions_json, '$.metadata_revision'),"
            " json_extract(payload_json, '$.category.metadata_revision'), payload_hash"
            " FROM registration_snapshots WHERE registration_snapshot_id = ?",
            (snapshot.registration_snapshot_id,),
        ).fetchone()
    assert frozen == (first, first, snapshot.payload_hash)


# ------------------------------------------------------------------ a frozen unit, its own revision


def _frozen(
    container: Container, config: AppConfig, account: str, preflight: RegistrationPreflightService
) -> tuple[Any, Any, Any, str]:
    """One unit frozen from a final READY evaluation, with the request and assets it froze from."""
    item = ready_item(container, Collections.of(container, config))
    store = container.registrations
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    assets = prepared(preflight.candidate(req))
    final = preflight.final(req, assets)
    assert final.status is ReadinessStatus.READY, final.reasons
    builder = RegistrationSnapshotBuilder(preflight=preflight, registrations=store)
    snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    return snapshot, req, assets, draft_id


def _frozen_category(container: Container, draft_id: str, snapshot_id: str) -> Any:
    units = [u for u in container.register.units(draft_id) if u.unit_ref == snapshot_id]
    assert len(units) == 1
    return units[0].category


def test_a_frozen_unit_renders_the_exact_revision_it_froze_while_the_preflight_reads_current(
    api: TestClient,
    container: Container,
    config: AppConfig,
    account: str,
    owner: CategoryMetadataService,
) -> None:
    preflight = _served_preflight(container, account)
    first = _record(owner, None)
    snapshot, req, assets, draft_id = _frozen(container, config, account, preflight)

    # A materially different current revision: other notice type and fields, an extra and a newly
    # required attribute, no options.
    second = _record(
        owner,
        first,
        name_max_length=90,
        attributes=[
            _rule("brand", required=True),
            _rule("color", required=True),
            _rule("material", required=True),
        ],
        notice={"notice_type": "notice-test-2", "fields": [_rule("manufacturer", required=True)]},
        options={"options_supported": False, "max_options": 1, "max_dimensions": 1},
    )

    # The frozen unit shows R1 — its identity and its rules, never R2's rules under R1's name.
    for category in (
        _frozen_category(container, draft_id, snapshot.registration_snapshot_id),
        next(
            u["category"]
            for u in api.get(f"/api/v1/register/units/{draft_id}", headers=CLIENT).json()
            if u["unit_ref"] == snapshot.registration_snapshot_id
        ),
    ):
        view = category if isinstance(category, dict) else category.model_dump()
        assert view["metadata_revision"] == first
        assert view["metadata_unavailable_reason"] is None
        assert view["reviewed"] is True
        assert view["notice_type"] == "notice-test-1"
        assert [(f["key"], f["required"]) for f in view["attributes"]] == [
            ("brand", True),
            ("color", False),
        ]
        assert [f["key"] for f in view["notice_fields"]] == ["manufacturer", "origin"]
        assert (view["options_supported"], view["max_options"]) == (True, 5)

    # A fresh evaluation reads the current revision, and the frozen candidate is stale under it.
    fresh = preflight.candidate(req)
    assert fresh.resolved.metadata is not None
    assert fresh.resolved.metadata.metadata_revision == second
    stale = preflight.final(req, assets)
    assert stale.status is not ReadinessStatus.READY
    assert "PREPARED_ASSET_CANDIDATE_MISMATCH" in _codes(stale)


def test_an_unresolvable_frozen_revision_fails_closed_and_never_shows_the_current_one(
    container: Container, config: AppConfig, account: str, owner: CategoryMetadataService
) -> None:
    preflight = _served_preflight(container, account)
    durable = preflight._metadata
    # Freeze under a revision the durable owner never held (the offline fixture's), then put the
    # durable source back and give the key a current revision of its own.
    preflight._metadata = StaticRegistrationMetadata((metadata(),), marketplace_key=MARKET)
    snapshot, _req, _assets, draft_id = _frozen(container, config, account, preflight)
    preflight._metadata = durable
    _record(owner, None)

    category = _frozen_category(container, draft_id, snapshot.registration_snapshot_id)
    assert category.metadata_revision == "metadata-test-1"
    assert category.metadata_unavailable_reason == "REGISTER_FROZEN_METADATA_UNRESOLVED"
    assert category.reviewed is None and category.notice_type is None
    assert category.attributes == () and category.notice_fields == ()
    assert (category.options_supported, category.max_options) == (None, None)


def test_an_exact_revision_resolves_only_within_its_own_key(
    container: Container, owner: CategoryMetadataService
) -> None:
    mine = _record(owner, None)
    other = owner.record(
        MARKET,
        TAXONOMY,
        "category-test-2",
        RecordRevisionRequest.model_validate(body()),
        correlation_id=CID,
    ).current
    assert other is not None
    source = DurableRegistrationMetadata(owner._store)
    found = source.revision(MARKET, TAXONOMY, CATEGORY, mine)
    assert found is not None and found.metadata_revision == mine
    assert source.revision(MARKET, TAXONOMY, CATEGORY, other.metadata_revision) is None
    assert source.revision(MARKET, "taxonomy-test-2", CATEGORY, mine) is None
    assert source.revision("market_b", TAXONOMY, CATEGORY, mine) is None
    assert source.revision(MARKET, TAXONOMY, CATEGORY, "no-such-revision") is None


# ------------------------------------------------------------------ reviewed = operator-confirmed


def test_the_database_refuses_a_reviewed_row_that_is_not_operator_confirmed(
    api: TestClient, config: AppConfig
) -> None:
    reviewed = save(api, body()).json()["current"]["metadata_revision"]

    def copy(row_id: str, content: str, review: str) -> str:
        return (
            f"INSERT INTO {REVISIONS} SELECT '{row_id}', metadata_id, 2, {content},"
            f" content_fingerprint, {review}, recorded_by, correlation_id, recorded_at"
            f" FROM {REVISIONS} WHERE metadata_revision_id = '{reviewed}'"
        )

    with contextlib.closing(raw(config)) as connection:
        for content in (
            "json_set(content_json, '$.content_provenance', 'AI_SUGGESTION')",
            "json_set(content_json, '$.content_provenance', 'SOURCE_FACT')",
            "json_remove(content_json, '$.content_provenance')",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="review_provenance"):
                connection.execute(copy("r-bad", content, "1, reviewed_by, reviewed_at"))
        # The same AI-suggested row recorded unreviewed is accepted: the rule is about review.
        ai = "json_set(content_json, '$.content_provenance', 'AI_SUGGESTION')"
        connection.execute(copy("r-ai", ai, "0, NULL, NULL"))
        connection.commit()
    assert counts(config) == {KEYS: 1, REVISIONS: 2, CURRENT: 1}


@dataclass
class _CorruptStore:
    """A store that hands back a row whose stored reviewed bit disagrees with its provenance, as a
    row written around every guard would."""

    record: MetadataRevisionRecord

    def current(self, *_key: str) -> MetadataRevisionRecord:
        return self.record

    def revision(self, *_key: str) -> MetadataRevisionRecord:
        return self.record


@pytest.mark.parametrize(
    "provenance", ["AI_SUGGESTION", "SOURCE_FACT", "operator_confirmed", "<absent>"]
)
def test_a_reviewed_bit_never_makes_non_operator_content_reviewed(
    owner: CategoryMetadataService, provenance: str
) -> None:
    revision = _record(owner, None)
    stored = owner._store.revision(MARKET, TAXONOMY, CATEGORY, revision)
    assert stored is not None and stored.reviewed is True
    document = {key: value for key, value in stored.content.items() if key != "content_provenance"}
    if provenance != "<absent>":
        document["content_provenance"] = provenance

    assert effectively_reviewed(True, document) is False
    assert category_metadata_of(revision, True, document).reviewed is False
    corrupt = _CorruptStore(replace(stored, content=document))
    source = DurableRegistrationMetadata(corrupt)  # type: ignore[arg-type]
    for materialized in (
        source.category(MARKET, TAXONOMY, CATEGORY),
        source.revision(MARKET, TAXONOMY, CATEGORY, revision),
    ):
        assert materialized is not None and materialized.reviewed is False
    # Operator-confirmed content with the stored bit is reviewed; without the bit it is not.
    assert category_metadata_of(revision, True, stored.content).reviewed is True
    assert category_metadata_of(revision, False, stored.content).reviewed is False


def test_the_model_check_is_the_migration_check() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "app/db/migrations/versions/0020_g1_registration_category_metadata.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0020", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.REVIEW_PROVENANCE == models.REVIEW_PROVENANCE
    # `IS`: a missing provenance must fail the CHECK instead of passing as NULL.
    assert "'$.content_provenance') IS 'OPERATOR_CONFIRMED'" in models.REVIEW_PROVENANCE
