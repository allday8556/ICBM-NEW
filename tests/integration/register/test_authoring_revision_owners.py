"""The durable authoring-revision owners (ADR-0014 §27.1; Issue #89 resolution 5907626428).

Through the real application on a migrated database, with no provider contacted:
- a saved target policy is stamped by the server with the owner's current revisions — the category
  mapping of exactly its marketplace and taxonomy, the detail composition of exactly its
  marketplace — and each is a real row with strictly typed v1 content and its fingerprint;
- identities are stable across saves and restarts, a scope never gets a duplicate, and a refused
  save creates nothing;
- a profile changes only by appending the next revision of its scope, a rollback included; the
  table refuses an update, a delete, an out-of-order sequence and content of another scope;
- nothing is backfilled: a policy revision and a preparation from before the owners keep ``null``
  and stay unowned until a new policy revision is appended and the unit is authored against it;
- with owner-held revisions an otherwise valid unit is READY and freezes offline, and its Snapshot
  names exactly the owner's revisions.
"""

import contextlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring_revisions import (
    CATEGORY_MAPPING_CONTENT_VERSION,
    DETAIL_COMPOSITION_CONTENT_VERSION,
    SERVER_ACTOR,
    AuthoringRevisionError,
    AuthoringRevisionKind,
    canonical_content,
    category_mapping_content,
    detail_composition_content,
)
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.model import sanitized_digest
from app.stages.register.preparation import (
    AUTHORING_REVISIONS_UNOWNED,
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
)
from tests.conftest import LOCAL
from tests.integration.register.test_authoring_unowned_revisions import (  # noqa: F401 - fixtures
    PREPARATIONS,
    api,
    author,
    container,
    draft,
    evaluate,
    inputs,
)
from tests.integration.register.test_g1a_target_policy import (
    _served_preflight,
    save,
)
from tests.integration.register.test_g1a_target_policy import (
    inputs as policy_inputs,
)
from tests.support import gate1_support
from tests.support.gate1_support import save_policy
from tests.support.product_support import Collections, raw
from tests.support.register_support import (
    CATEGORY,
    CID,
    MARKET,
    OPERATOR,
    TAXONOMY,
    establish,
    no_match,
    prepared,
    ready_item,
    request,
)
from tests.support.register_support import (
    draft as make_draft,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
TABLE = "registration_authoring_revisions"
MAPPING = AuthoringRevisionKind.CATEGORY_MAPPING
COMPOSITION = AuthoringRevisionKind.DETAIL_COMPOSITION
AT = "2026-09-30 00:00:00"


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:  # noqa: F811 - the imported fixture
    return establish(container, config, MARKET, "uid-authoring-1")


def rows(config: AppConfig) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            f"SELECT kind, marketplace_key, taxonomy_revision, seq, revision_id FROM {TABLE}"
            " ORDER BY kind, taxonomy_revision, seq"
        ).fetchall()


def owner_events(container: Container) -> list[Any]:  # noqa: F811
    return [
        event
        for event in container.audit.list_events(limit=500)
        if event.event_type == AuditEventType.REGISTRATION_AUTHORING_REVISION_APPENDED
    ]


# ------------------------------------------------------------------ stamping


def test_a_saved_policy_is_stamped_with_real_owner_revisions(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,
) -> None:
    assert rows(config) == []
    saved = save(api, account, policy_inputs())
    assert saved.status_code == 200, saved.text
    stamped = saved.json()["inputs"]

    mapping = container.authoring_revisions.current(MAPPING, MARKET, TAXONOMY)
    composition = container.authoring_revisions.current(COMPOSITION, MARKET)
    assert mapping is not None and composition is not None
    assert stamped["category_mapping_revision"] == mapping.revision_id
    assert stamped["detail_composition_revision"] == composition.revision_id
    assert rows(config) == [
        ("CATEGORY_MAPPING", MARKET, TAXONOMY, 1, mapping.revision_id),
        ("DETAIL_COMPOSITION", MARKET, None, 1, composition.revision_id),
    ]
    # Real rows with the strictly typed content — operator-confirmed selection only, and the
    # B-DETAIL v2 composition profile (sections, body format and renderer; no product content) —
    # and the fingerprint of exactly that content. Never a label or a sentinel.
    assert mapping.content == {
        "content_version": CATEGORY_MAPPING_CONTENT_VERSION,
        "kind": "CATEGORY_MAPPING",
        "marketplace_key": MARKET,
        "taxonomy_revision": TAXONOMY,
        "selection": "OPERATOR_CONFIRMED",
        "automatic_mapping": False,
    }
    assert composition.content == {
        "content_version": DETAIL_COMPOSITION_CONTENT_VERSION,
        "kind": "DETAIL_COMPOSITION",
        "marketplace_key": MARKET,
        "sections": ["DETAIL_IMAGES", "BODY"],
        "body_format": "PLAIN_TEXT",
        "renderer": "detail-renderer/v1",
        "guidance": False,
    }
    for record in (mapping, composition):
        assert record.content_fingerprint == sanitized_digest(record.content)
        assert record.created_by == SERVER_ACTOR and record.seq == 1
    assert (mapping.taxonomy_revision, composition.taxonomy_revision) == (TAXONOMY, None)
    # The production policy source hands on exactly these two.
    target = container.registration_preflight.target_policy(MARKET, account)
    assert target is not None
    assert (target.category_mapping_revision, target.detail_composition_revision) == (
        mapping.revision_id,
        composition.revision_id,
    )
    events = owner_events(container)
    assert sorted(event.details["kind"] for event in events) == [
        "CATEGORY_MAPPING",
        "DETAIL_COMPOSITION",
    ]
    assert {event.actor for event in events} == {SERVER_ACTOR}


def test_identities_are_stable_and_a_scope_never_gets_a_duplicate(config: AppConfig) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        served: Container = client.app.state.container
        first_account = establish(served, config, MARKET, "uid-authoring-1")
        first = save(client, first_account, policy_inputs()).json()
        before = rows(config)
        # Another save of the same scope, and another account of the same marketplace and
        # taxonomy, read the same two revisions: nothing is appended.
        changed = policy_inputs(templates={"shipping": "shipping-2", "returns": "returns-2"})
        second = save(
            client, first_account, changed, expected=first["current"]["policy_revision"]
        ).json()
        other_account = establish(served, config, MARKET, "uid-authoring-2")
        other = save(client, other_account, policy_inputs()).json()
        assert rows(config) == before
    # A restart: the same durable identities, and a further save still appends nothing.
    with TestClient(create_app(config), base_url=LOCAL) as client:
        assert rows(config) == before
        third = save(
            client, first_account, policy_inputs(), expected=second["current"]["policy_revision"]
        ).json()
        assert rows(config) == before
    for name in ("category_mapping_revision", "detail_composition_revision"):
        assert (
            first["inputs"][name]
            == second["inputs"][name]
            == third["inputs"][name]
            == other["inputs"][name]
        )


def test_a_category_mapping_never_floats_across_taxonomy_revisions(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,
) -> None:
    first = save(api, account, policy_inputs()).json()
    moved = save(
        api,
        account,
        policy_inputs(taxonomy_revision="taxonomy-test-2"),
        expected=first["current"]["policy_revision"],
    ).json()
    # Another taxonomy is another category-mapping scope with its own first revision; the detail
    # composition is scoped to the marketplace alone and is the same one.
    assert (
        moved["inputs"]["category_mapping_revision"] != first["inputs"]["category_mapping_revision"]
    )
    assert (
        moved["inputs"]["detail_composition_revision"]
        == first["inputs"]["detail_composition_revision"]
    )
    assert [row[:4] for row in rows(config)] == [
        ("CATEGORY_MAPPING", MARKET, TAXONOMY, 1),
        ("CATEGORY_MAPPING", MARKET, "taxonomy-test-2", 1),
        ("DETAIL_COMPOSITION", MARKET, None, 1),
    ]
    other = container.authoring_revisions.current(MAPPING, MARKET, "taxonomy-test-2")
    assert other is not None and other.content["taxonomy_revision"] == "taxonomy-test-2"
    assert container.authoring_revisions.current(MAPPING, MARKET, "taxonomy-unknown") is None
    assert container.authoring_revisions.current(MAPPING, "another-market", TAXONOMY) is None


def test_a_refused_save_creates_no_owner_revision(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,
) -> None:
    for reference in ("category_mapping_revision", "detail_composition_revision"):
        named = save(api, account, policy_inputs(**{reference: "body-only-v1"}))
        assert named.status_code == 422
        assert named.json()["error"]["code"] == "TARGET_POLICY_AUTHORING_REVISION_UNOWNED"
    invalid = save(api, account, policy_inputs(templates={"shipping": "https://x.invalid"}))
    assert invalid.status_code == 422
    # A save against a policy that does not exist yet as the client believes it: refused inside
    # the unit of work that would have created the first revisions, so they do not exist either.
    moved = save(
        api, account, policy_inputs(taxonomy_revision="taxonomy-test-2"), expected="not-current"
    )
    assert moved.status_code == 409
    assert rows(config) == [] and owner_events(container) == []

    first = save(api, account, policy_inputs()).json()
    before = rows(config)
    same = save(api, account, policy_inputs(), expected=first["current"]["policy_revision"])
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "TARGET_POLICY_UNCHANGED"
    stale = save(api, account, policy_inputs(taxonomy_revision="taxonomy-test-2"), expected=None)
    assert stale.status_code == 409
    assert rows(config) == before and len(before) == 2


# ------------------------------------------------------------------ the owner


def test_a_profile_changes_only_by_appending_and_a_rollback_is_a_new_revision(
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    owner = container.authoring_revisions
    v1 = detail_composition_content(MARKET)

    def ensure(content: dict[str, Any]) -> str:
        with container.db.write() as session:
            return owner.ensure(session, COMPOSITION, MARKET, None, content, correlation_id=CID)

    first = ensure(v1)
    assert ensure(v1) == first  # the current revision already holds this content
    # No other content is a v1 profile, so the only way to reach a second revision here is the
    # table itself: an appended row is the current one, and the earlier one is never moved back to.
    with contextlib.closing(raw(config)) as connection:
        other = json.dumps({**v1, "content_version": "registration-detail-composition/v2"})
        connection.execute(
            f"INSERT INTO {TABLE} VALUES ('rev-2', 'DETAIL_COMPOSITION', ?, NULL, 2, ?, ?, ?, 's')",
            (MARKET, other, "b" * 64, AT),
        )
        connection.commit()
    current = owner.current(COMPOSITION, MARKET)
    assert current is not None and (current.revision_id, current.seq) == ("rev-2", 2)
    # Asking for the v1 content again appends revision 3 with it: a rollback is a new revision.
    third = ensure(v1)
    assert third not in (first, "rev-2")
    history = owner.history(COMPOSITION, MARKET)
    assert [record.seq for record in history] == [1, 2, 3]
    assert [record.revision_id for record in history] == [first, "rev-2", third]
    assert history[0].content_fingerprint == history[2].content_fingerprint
    current = owner.current(COMPOSITION, MARKET)
    assert current is not None and current.revision_id == third


@pytest.mark.parametrize(
    ("kind", "taxonomy", "content"),
    [
        (COMPOSITION, None, {**detail_composition_content(MARKET), "sections": ["TOP", "BODY"]}),
        (COMPOSITION, None, {**detail_composition_content(MARKET), "guidance": True}),
        (COMPOSITION, None, {**detail_composition_content(MARKET), "extra": 1}),
        (COMPOSITION, None, {**detail_composition_content(MARKET), "content_version": "x/v2"}),
        (COMPOSITION, None, detail_composition_content("another-market")),
        (COMPOSITION, TAXONOMY, detail_composition_content(MARKET)),
        (MAPPING, TAXONOMY, {**category_mapping_content(MARKET, TAXONOMY), "selection": "AI"}),
        (
            MAPPING,
            TAXONOMY,
            {**category_mapping_content(MARKET, TAXONOMY), "automatic_mapping": True},
        ),
        (MAPPING, TAXONOMY, {**category_mapping_content(MARKET, TAXONOMY), "table": {"a": "b"}}),
        (MAPPING, TAXONOMY, category_mapping_content(MARKET, "taxonomy-test-2")),
        (MAPPING, None, category_mapping_content(MARKET, TAXONOMY)),
        (MAPPING, TAXONOMY, detail_composition_content(MARKET)),
        (MAPPING, "https://x.invalid/t", category_mapping_content(MARKET, "https://x.invalid/t")),
    ],
    ids=[
        "guidance-section",
        "guidance-flag",
        "extra-member",
        "other-version",
        "other-marketplace",
        "composition-with-taxonomy",
        "ai-selection",
        "automatic-mapping",
        "mapping-table",
        "other-taxonomy",
        "mapping-without-taxonomy",
        "other-kind",
        "unsafe-scope",
    ],
)
def test_v1_content_is_strict_and_rejects_every_wider_semantic(
    container: Container,  # noqa: F811
    config: AppConfig,
    kind: AuthoringRevisionKind,
    taxonomy: str | None,
    content: dict[str, Any],
) -> None:
    with pytest.raises(AuthoringRevisionError):
        canonical_content(kind, MARKET, taxonomy, content)
    with pytest.raises(AuthoringRevisionError), container.db.write() as session:
        container.authoring_revisions.ensure(
            session, kind, MARKET, taxonomy, content, correlation_id=CID
        )
    assert rows(config) == []


def _insert(connection: sqlite3.Connection, **overrides: Any) -> None:
    content = overrides.pop("content", None)
    values: dict[str, Any] = {
        "revision_id": "rev-x",
        "kind": "DETAIL_COMPOSITION",
        "marketplace_key": MARKET,
        "taxonomy_revision": None,
        "seq": 1,
        "content_fingerprint": "a" * 64,
        "created_at": AT,
        "created_by": "s",
    }
    values.update(overrides)
    if content is None:
        content = (
            detail_composition_content(values["marketplace_key"])
            if values["kind"] == "DETAIL_COMPOSITION"
            else category_mapping_content(values["marketplace_key"], values["taxonomy_revision"])
        )
    connection.execute(
        f"INSERT INTO {TABLE} (revision_id, kind, marketplace_key, taxonomy_revision, seq,"
        " content_json, content_fingerprint, created_at, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            values["revision_id"],
            values["kind"],
            values["marketplace_key"],
            values["taxonomy_revision"],
            values["seq"],
            content if isinstance(content, str) else json.dumps(content),
            values["content_fingerprint"],
            values["created_at"],
            values["created_by"],
        ),
    )


@pytest.mark.parametrize(
    "refused",
    [
        {"kind": "OTHER"},
        {"taxonomy_revision": TAXONOMY},
        {"kind": "CATEGORY_MAPPING"},
        {"kind": "CATEGORY_MAPPING", "taxonomy_revision": ""},
        {"seq": 0},
        {"seq": 2},
        {"content_fingerprint": "A" * 64},
        {"content_fingerprint": "a" * 63},
        {"created_by": ""},
        {"marketplace_key": ""},
        {"content": "[]"},
        {"content": "not json"},
        {"content": detail_composition_content("another-market")},
        {
            "kind": "CATEGORY_MAPPING",
            "taxonomy_revision": TAXONOMY,
            "content": category_mapping_content(MARKET, "taxonomy-test-2"),
        },
        {
            "kind": "CATEGORY_MAPPING",
            "taxonomy_revision": TAXONOMY,
            "content": detail_composition_content(MARKET),
        },
    ],
    ids=[
        "unknown-kind",
        "composition-with-taxonomy",
        "mapping-without-taxonomy",
        "mapping-empty-taxonomy",
        "seq-zero",
        "seq-skipped",
        "fingerprint-uppercase",
        "fingerprint-short",
        "no-author",
        "no-marketplace",
        "content-array",
        "content-not-json",
        "content-other-marketplace",
        "content-other-taxonomy",
        "content-other-kind",
    ],
)
def test_the_table_refuses_a_malformed_row(
    container: Container,  # noqa: F811
    config: AppConfig,
    refused: dict[str, Any],
) -> None:
    with contextlib.closing(raw(config)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, **refused)
        connection.rollback()
    assert rows(config) == []


def test_the_table_is_append_only_in_order_and_never_deleted(
    api: TestClient,  # noqa: F811
    config: AppConfig,
    account: str,
) -> None:
    save(api, account, policy_inputs())
    before = rows(config)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            f"UPDATE {TABLE} SET content_fingerprint = '{'c' * 64}'",
            f"UPDATE {TABLE} SET seq = seq + 1",
            f"UPDATE {TABLE} SET created_by = 'someone'",
            f"DELETE FROM {TABLE}",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()
        # A second row of an existing scope must be the next in order: not the same sequence
        # again and not one that skips ahead.
        for seq in (1, 3):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(connection, revision_id=f"rev-{seq}", seq=seq)
            connection.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                _insert(
                    connection,
                    revision_id=f"map-{seq}",
                    kind="CATEGORY_MAPPING",
                    taxonomy_revision=TAXONOMY,
                    seq=seq,
                )
            connection.rollback()
    assert rows(config) == before


def test_0032_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)

    def tables() -> set[str]:
        with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
            return {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = tables()
    command.downgrade(alembic_config(url), "0031_m5_registration_reconcile")
    # 0032 owns exactly this table; the one a later revision adds steps down with it.
    assert before - tables() == {
        TABLE,
        "canary_eligibility_records",
        "extension_queues",
        "extension_queue_items",
        "residual_risk_acceptances",
        "registration_deletions",
        "synthetic_test_products",
        "supplier_common_image_decisions",
        "marketplace_category_catalog_snapshots",
        "marketplace_category_catalog_entries",
        "common_sales_option_revisions",
        "common_sales_option_axes",
        "common_sales_option_values",
        "current_common_sales_option_revision_moves",
        "common_option_fact_mapping_revisions",
        "common_option_fact_axis_mappings",
        "common_option_fact_value_mappings",
        "current_common_option_fact_mapping_moves",
        "atomic_sku_set_revisions",
        "atomic_skus",
        "atomic_sku_selections",
        "atomic_sku_revision_members",
        "atomic_sku_revision_selection_evidence",
        "current_atomic_sku_set_moves",
        "atomic_sku_product_items",
        "atomic_sku_source_bindings",
        "atomic_sku_pricing_snapshots",
        "current_atomic_sku_pricing_snapshot_moves",
        "registration_bulk_runs",
        "registration_bulk_items",
        "operate_listing_sync_runs",
        "operate_listing_observations",
        "operate_stock_rechecks",
        "operate_order_sync_runs",
        "operate_orders",
        "operate_order_status_history",
        "operate_adopted_listings",
        "operate_adopted_observations",
        "operate_order_adoption_links",
        "operate_supplier_orders",
        "operate_supplier_order_history",
        "ai_prompt_templates",
        "ai_prompt_template_revisions",
        "ai_prompt_template_current",
        "ai_platform_policies",
        "ai_platform_policy_revisions",
        "ai_platform_policy_current",
        "product_enrichment_results",
        "ai_provider_profiles",
        "ai_provider_profile_revisions",
        "ai_provider_profile_current",
        "ai_provider_calls",
    }
    command.upgrade(alembic_config(url), "head")
    assert tables() == before
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        _insert(connection)
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0031_m5_registration_reconcile")
    assert TABLE in tables()


# ------------------------------------------------------------------ no backfill


def test_nothing_is_backfilled_and_a_unit_becomes_owner_held_only_by_new_revisions(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    draft: tuple[str, str],  # noqa: F811 - a policy appended before the owners existed
) -> None:
    draft_id, item_id = draft
    account = container.accounts.accounts(gate1_support.MARKET)[0].marketplace_account_id
    saved = author(api, draft_id, item_id)
    preparation_id = saved["preparation_id"]
    assert AUTHORING_REVISIONS_UNOWNED in evaluate(api, preparation_id)["reason_codes"]
    assert rows(config) == []

    def stored() -> tuple[list[Any], list[Any]]:
        with contextlib.closing(raw(config)) as connection:
            return (
                connection.execute(
                    "SELECT policy_revision_id, content_json, content_fingerprint"
                    " FROM registration_target_policy_revisions ORDER BY revision_no"
                ).fetchall(),
                connection.execute(
                    "SELECT preparation_revision_id, category_json, detail_json"
                    " FROM registration_preparation_revisions ORDER BY revision_no"
                ).fetchall(),
            )

    def unchanged() -> bool:
        policies, preparations = stored()
        return policies[:1] == history[0] and preparations[:1] == history[1]

    history = stored()
    assert [len(part) for part in history] == [1, 1]
    # The operator saves the policy again: a new revision, stamped. The earlier policy revision
    # and the earlier preparation revision are byte-identical, nulls included.
    save_policy(api, account, account_scoped=True)
    mapping, composition = gate1_support.owned_revisions(container, account)
    assert mapping and composition
    assert unchanged()
    assert json.loads(history[0][0][1])["category_mapping_revision"] is None
    assert json.loads(history[0][0][1])["detail_composition_revision"] is None
    assert json.loads(history[1][0][1])["mapping_revision"] is None
    assert json.loads(history[1][0][2])["composition_revision"] is None
    # The preparation still holds its nulls, which are not the policy's revisions: still unowned.
    assert AUTHORING_REVISIONS_UNOWNED in evaluate(api, preparation_id)["reason_codes"]
    # The authoring form now receives the server's revisions and must send back exactly those.
    metadata = api.get(
        f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{gate1_support.CATEGORY}",
        headers=CLIENT,
    ).json()
    assert (metadata["mapping_revision"], metadata["detail_composition_revision"]) == (
        mapping,
        composition,
    )
    stale = api.post(
        f"{PREPARATIONS}/{preparation_id}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": inputs()},
        headers=CLIENT,
    )
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "REGISTER_AUTHORING_REVISION_NOT_OWNED"
    owned = inputs()
    owned["category"]["mapping_revision"] = mapping
    owned["detail_composition_revision"] = composition
    revised = api.post(
        f"{PREPARATIONS}/{preparation_id}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": owned},
        headers=CLIENT,
    )
    assert revised.status_code == 200, revised.text
    codes = evaluate(api, preparation_id)["reason_codes"]
    assert AUTHORING_REVISIONS_UNOWNED not in codes
    # Only the new preparation revision is owner-held; the earlier one is still stored as it was.
    assert unchanged()
    assert [len(part) for part in stored()] == [2, 2]


# ------------------------------------------------------------------ READY and the freeze


def test_an_otherwise_valid_unit_is_ready_and_freezes_with_the_owners_revisions(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,
) -> None:
    preflight = _served_preflight(container)
    current = save(api, account, policy_inputs()).json()
    mapping = current["inputs"]["category_mapping_revision"]
    composition = current["inputs"]["detail_composition_revision"]
    item = ready_item(container, Collections.of(container, config))
    draft_id = make_draft(container.registrations, account, [item])
    req = request(
        container.registrations,
        draft_id,
        account,
        [item],
        category=CategorySelection(
            CATEGORY, mapping, TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        # Exactly the owned profile's sections (B-DETAIL content v2).
        detail=DetailComposition(composition, "invented body text", ("DETAIL_IMAGES", "BODY")),
    )
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    candidate = preflight.candidate(req)
    assert candidate.status is ReadinessStatus.READY, candidate.reasons
    final = preflight.final(req, prepared(candidate))
    assert final.status is ReadinessStatus.READY, final.reasons

    # Any other revision — the other kind's, an earlier label, none — is still never READY.
    for category_revision, detail_revision in (
        (composition, composition),
        (mapping, mapping),
        ("mapping-test-1", composition),
        (mapping, None),
    ):
        other = replace(
            req,
            category=CategorySelection(
                CATEGORY, category_revision, TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
            ),
            detail=DetailComposition(detail_revision, "invented body text"),
        )
        assert AUTHORING_REVISIONS_UNOWNED in preflight.candidate(other).codes
    # Sections other than the owned profile's are never owner-held either (B-DETAIL).
    body_only = replace(req, detail=DetailComposition(composition, "invented body text"))
    assert AUTHORING_REVISIONS_UNOWNED in preflight.candidate(body_only).codes

    builder = RegistrationSnapshotBuilder(
        preflight=preflight, registrations=container.registrations
    )
    snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    with contextlib.closing(raw(config)) as connection:
        frozen = connection.execute(
            "SELECT category_mapping_revision, detail_composition_revision,"
            " policy_revisions_json FROM registration_snapshots"
            " WHERE registration_snapshot_id = ?",
            (snapshot.registration_snapshot_id,),
        ).fetchone()
    assert frozen[:2] == (mapping, composition)
    assert current["current"]["policy_revision"] in json.loads(frozen[2]).values()
    # Freezing is local: nothing was sent, and no Intent was opened by it.
    assert container.jobs.count(job_type_prefix="register.") == 0
