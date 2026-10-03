"""The image auto-selection over the durable owners (Issue #219, owner decision 2026-10-03).

The rule records its selection as ``DecisionOrigin.RULE`` with the automatic QA of what it chose,
so a collected Item is no longer stuck on IMAGE_SELECTION_MISSING; an operator's selection always
supersedes it; a file that later becomes a blocked common image makes the rule's selection
``IMAGE_SELECTION_RECHECK_REQUIRED`` instead of changing silently; and a rule name the supplier's
table does not hold leaves the Item unselected with the reason shown.
"""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.products.auto_images import (
    AUTO_SELECTION_RULE_VERSION,
    AutoSelectionBlocked,
    AutoSelectionStatus,
    ImageAutoSelector,
    ImageSlot,
    StaticSupplierImageRoles,
)
from app.stages.products.common_images import CommonImageVerdict
from app.stages.products.image_model import (
    AUTO_QA_RULE_VERSION,
    DecisionOrigin,
    OutputRole,
    QaVerdict,
)
from tests.support.collect_support import PNG, REPRESENTATIVE
from tests.support.jobs_support import FakeClock
from tests.support.product_support import SUPPLIER, Collections, FakeDecoder, product

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
DETAIL_RULE = "test.detail"


def _services(client: TestClient) -> Container:
    return client.app.state.container  # type: ignore[attr-defined,no-any-return]


def _selector(client: TestClient, **extra: ImageSlot) -> ImageAutoSelector:
    services = _services(client)
    table = {REPRESENTATIVE.provenance: ImageSlot.REPRESENTATIVE, DETAIL_RULE: ImageSlot.DETAIL}
    return ImageAutoSelector(
        store=services.product_store,
        images=services.images,
        roles=StaticSupplierImageRoles({SUPPLIER: {**table, **extra}}),
        audit=services.audit,
        clock=services.clock,
    )


def _stored(client: TestClient, config: AppConfig, marker: str) -> str:
    services = _services(client)
    assets = SourceAssetStore(config.source_assets_dir, services.db, FakeDecoder(), FakeClock())
    return assets.put(PNG + marker.encode()).sha256


def _detail(ordinal: int, sha256: str, provenance: str = DETAIL_RULE) -> ImageReference:
    return ImageReference(
        role=ImageRole.DETAIL,
        ordinal=ordinal,
        host="img.shop.example",
        provenance=provenance,
        status=FieldStatus.CONFIRMED,
        sha256=sha256,
    )


def _item(client: TestClient, config: AppConfig, *images: ImageReference) -> str:
    services = _services(client)
    run_id, _revision = Collections.of(services, config).collect(product(), extra_images=images)
    result = services.materializer.materialize_run(run_id)
    assert result.item_id is not None
    return result.item_id


def _codes(client: TestClient, item_id: str) -> set[str]:
    return {r.code for r in _services(client).product_readiness.base_readiness(item_id).reasons}


def test_the_rule_selects_with_automatic_qa_and_the_item_is_no_longer_missing_images(
    client: TestClient, config: AppConfig
) -> None:
    first, second = _stored(client, config, "d1"), _stored(client, config, "d2")
    item_id = _item(client, config, _detail(1, first), _detail(2, second))
    assert "IMAGE_SELECTION_MISSING" in _codes(client, item_id)
    result = _selector(client).auto_select(item_id)
    assert result.status is AutoSelectionStatus.SELECTED
    selection = result.selection
    assert selection is not None
    assert selection.decision_origin is DecisionOrigin.RULE
    assert selection.decided_by == AUTO_SELECTION_RULE_VERSION
    assert [o.role for o in selection.outputs] == [
        OutputRole.REPRESENTATIVE,
        OutputRole.DETAIL,
        OutputRole.DETAIL,
    ]
    assert [o.sha256 for o in selection.outputs[1:]] == [first, second]
    codes = _codes(client, item_id)
    assert not {c for c in codes if c.startswith("IMAGE_")}, codes
    qa = _services(client).images.qa_for_selected_asset(
        asset_kind=selection.outputs[0].asset_kind,
        sha256=selection.outputs[0].sha256,
        derivation_id=None,
        validated_source_revision_id=selection.source_revision_id,
    )
    assert qa is not None and qa.qa_rule_version == AUTO_QA_RULE_VERSION
    assert qa.verdict is QaVerdict.PASS
    # Running the rule again over the same revision changes nothing.
    assert _selector(client).auto_select(item_id).status is AutoSelectionStatus.UNCHANGED


def test_an_operator_selection_supersedes_the_rule_and_is_never_moved(
    client: TestClient, config: AppConfig
) -> None:
    item_id = _item(client, config, _detail(1, _stored(client, config, "d1")))
    assert _selector(client).auto_select(item_id).status is AutoSelectionStatus.SELECTED
    found = client.get(f"/api/v1/products/items/{item_id}/image-candidates").json()
    representative = next(i for i in found["images"] if i["role"] == "REPRESENTATIVE")
    chosen = client.post(
        f"/api/v1/products/items/{item_id}/image-selection",
        json={
            "source_revision_id": found["source_revision_id"],
            "decisions": [
                {
                    "role": image["role"],
                    "ordinal": image["ordinal"],
                    "sha256": image["sha256"],
                    "decision": "USE_SOURCE" if image is representative else "EXCLUDE",
                }
                for image in found["images"]
            ],
            "outputs": [
                {
                    "role": "REPRESENTATIVE",
                    "source_role": "REPRESENTATIVE",
                    "source_ordinal": representative["ordinal"],
                }
            ],
            "actor": "operator",
        },
        headers=CLIENT,
    )
    assert chosen.status_code == 200, chosen.text
    held = _selector(client).auto_select(item_id)
    assert held.status is AutoSelectionStatus.OPERATOR_HELD
    assert held.selection is not None
    assert held.selection.decision_origin is DecisionOrigin.OPERATOR


def test_a_file_blocked_after_the_rule_selected_it_asks_for_a_recheck(
    client: TestClient, config: AppConfig
) -> None:
    shared = _stored(client, config, "shared")
    item_id = _item(client, config, _detail(1, shared))
    assert _selector(client).auto_select(item_id).status is AutoSelectionStatus.SELECTED
    decided = _services(client).common_images.decide(
        SUPPLIER, shared, CommonImageVerdict.BLOCK, decided_by="operator"
    )
    assert decided.verdict is CommonImageVerdict.BLOCK
    assert "IMAGE_SELECTION_RECHECK_REQUIRED" in _codes(client, item_id)
    # The selection itself is unchanged: nothing is replaced silently.
    current = _services(client).images.current_selection(item_id)
    assert current is not None and any(o.sha256 == shared for o in current.outputs)


def test_an_unknown_rule_name_leaves_the_item_unselected_and_says_why(
    client: TestClient, config: AppConfig
) -> None:
    item_id = _item(client, config, _detail(1, _stored(client, config, "d1"), "test.unknown"))
    result = _selector(client).auto_select(item_id)
    assert result.status is AutoSelectionStatus.BLOCKED
    assert result.blocked is AutoSelectionBlocked.ROLE_RULE_UNKNOWN
    assert result.detail == "test.unknown"
    assert "IMAGE_SELECTION_MISSING" in _codes(client, item_id)
    # The operator sees why beside the candidates: the production table does not hold the test
    # helper's rule names either.
    found = client.get(f"/api/v1/products/items/{item_id}/image-candidates").json()
    assert found["auto_selection"]["blocked"] == "ROLE_RULE_UNKNOWN"


def test_the_operator_runs_the_rule_over_every_bound_item(
    client: TestClient, config: AppConfig
) -> None:
    item_id = _item(client, config, _detail(1, _stored(client, config, "d1")))
    response = client.post("/api/v1/products/image-auto-selection", json={}, headers=CLIENT)
    assert response.status_code == 200, response.text
    results: list[dict[str, Any]] = response.json()["results"]
    mine = next(r for r in results if r["item_id"] == item_id)
    # Production reads the supplier's published table, which the helper's names are not in.
    assert mine["status"] == "BLOCKED" and mine["blocked"] == "ROLE_RULE_UNKNOWN"
    single = client.post(f"/api/v1/products/items/{item_id}/image-auto-selection", headers=CLIENT)
    assert single.json()["status"] == "BLOCKED"


def test_0040_widens_only_its_checks_and_its_downgrade_keeps_a_rule_selection(
    config: AppConfig,
) -> None:
    from alembic import command

    from app.main import create_app
    from app.platform.db.migrate import alembic_config

    url = f"sqlite:///{(config.data_dir / 'runtime' / 'icbm.db').as_posix()}"
    with TestClient(create_app(config)) as client:
        item_id = _item(client, config, _detail(1, _stored(client, config, "d1")))
        assert _selector(client).auto_select(item_id).status is AutoSelectionStatus.SELECTED
    # A rule selection is never silently destroyed.
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0039_supplier_common_images")
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        stored = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name IN"
            " ('image_selection_revisions', 'image_selection_outputs',"
            " 'trg_current_image_selection_moves_chain') ORDER BY name"
        ).fetchall()
        assert raw.execute("SELECT decision_origin FROM image_selection_revisions").fetchall() == [
            ("RULE",)
        ]
    text = " ".join(row[0] for row in stored)
    assert "decision_origin IN ('OPERATOR', 'RULE')" in text
    assert "role IN ('REPRESENTATIVE', 'ADDITIONAL', 'DETAIL')" in text
    assert "s.decision_origin IN ('OPERATOR', 'RULE')" in text


def test_0040_round_trips_on_an_empty_selection_history(tmp_path: Any) -> None:
    from alembic import command

    from app.platform.db.migrate import alembic_config, upgrade_to_head

    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def definitions() -> list[str]:
        with sqlite3.connect(database) as raw:
            return [
                row[0]
                for row in raw.execute(
                    "SELECT sql FROM sqlite_master"
                    " WHERE sql IS NOT NULL AND name LIKE '%selection%'"
                    " ORDER BY name"
                )
            ]

    at_head = definitions()
    command.downgrade(alembic_config(url), "0039_supplier_common_images")
    below = definitions()
    assert any("decision_origin IN ('OPERATOR'))" in sql for sql in below)
    assert any("role IN ('REPRESENTATIVE', 'DETAIL'))" in sql for sql in below)
    upgrade_to_head(url)
    # The rebuilt tables, indexes and triggers are exactly what a fresh head creates.
    assert definitions() == at_head
