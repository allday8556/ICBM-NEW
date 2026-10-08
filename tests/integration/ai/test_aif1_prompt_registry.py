"""ADR-0026 AIF-1: the PromptTemplate and PlatformPolicy stores behind Settings' AI Prompt Registry.

The registry is the v29 prototype's, as the owner planned it (Issue #219 `6057252039`): its entries
are the store's keys and revision 1 of each is the prototype's text, verbatim. An operator saves one
field of one entry against the revision it read, or resets one field to the seed; every such
revision is audited without its text, and nothing is ever overwritten or deleted. No route here
reaches an AI provider.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.ai.prompts import RUNTIME_PLACEHOLDER
from app.capabilities.ai.registry import CATALOG, Layer
from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig, database_path

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
PROTOTYPE = (
    Path(__file__).resolve().parents[3] / "design/prototypes/icbm_redesign_test_v29_final.html"
)
BUNDLE = "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"


def _prototype_literal(name: str, key: str | None = None, field: str = "prompt") -> str:
    """One template literal of the prototype's registry, verbatim."""
    source = PROTOTYPE.read_text(encoding="utf-8")
    if key is None:
        match = re.search(rf"const {name} = `(.*?)`;", source, re.S)
    else:
        start = source.index(f"  {key}: {{", source.index(f"const {name} = {{"))
        match = re.compile(rf"\b{field}:\s*`(.*?)`", re.S).search(source, start)
    assert match is not None
    return match.group(1)


def _registry(client: TestClient) -> dict[str, dict[str, Any]]:
    body = client.get("/api/v1/ai/registry", headers=CLIENT).json()
    return {entry["key"]: entry for entry in body["entries"]}


def _save(client: TestClient, family: str, key: str, field: str, text: str, revision: str) -> Any:
    return client.post(
        f"/api/v1/ai/{family}/{key}/revisions",
        headers=CLIENT,
        json={
            "actor": "operator",
            "expected_current_revision": revision,
            "field": field,
            "text": text,
        },
    )


def _reset(client: TestClient, family: str, key: str, field: str, revision: str) -> Any:
    return client.post(
        f"/api/v1/ai/{family}/{key}/reset",
        headers=CLIENT,
        json={"actor": "operator", "expected_current_revision": revision, "field": field},
    )


def _registry_events(config: AppConfig) -> list[dict[str, Any]]:
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        rows = raw.execute(
            "SELECT event_type, action, target_ref, before_json, after_json, details_json"
            " FROM audit_events WHERE event_type IN (?, ?) ORDER BY seq",
            (AuditEventType.AI_PROMPT_TEMPLATE_REVISED, AuditEventType.AI_PLATFORM_POLICY_REVISED),
        ).fetchall()
    return [
        dict(
            zip(
                ("event_type", "action", "target_ref", "before", "after", "details"),
                row,
                strict=True,
            )
        )
        for row in rows
    ]


def test_the_registry_is_the_v29_prototypes_and_its_seed_is_the_prototypes_text(
    client: TestClient,
) -> None:
    entries = _registry(client)
    # Every entry of the prototype's registry, and nothing else, in the catalog's order.
    assert list(entries) == [entry.key for entry in CATALOG]
    assert [entries[k]["layer"] for k in entries].count("ROLE") == 6
    assert [entries[k]["layer"] for k in entries].count("POLICY") == 4
    assert [entries[k]["layer"] for k in entries].count("TASK") == 15
    # Revision 1 of each is the prototype's text, byte for byte, and nothing is modified yet.
    assert entries["ICBM_GLOBAL_RULES_V2"]["content"]["prompt"] == _prototype_literal(
        "ICBM_GLOBAL_RULES_V2"
    )
    assert entries["ROLE_PRODUCT_MD_V1"]["content"]["prompt"] == _prototype_literal(
        "ROLE_REGISTRY", "ROLE_PRODUCT_MD_V1"
    )
    assert entries["POLICY_NAVER_V1"]["content"]["prompt"] == _prototype_literal(
        "PLATFORM_POLICY_REGISTRY", "POLICY_NAVER_V1"
    )
    for field in ("prompt", "variables", "output"):
        assert entries[BUNDLE]["content"][field] == _prototype_literal(
            "TASK_REGISTRY", BUNDLE, field
        )
    assert entries[BUNDLE]["content"]["mode"] == "PRODUCT_RECOMMEND_BUNDLE"
    assert entries[BUNDLE]["role_key"] == "ROLE_PRODUCT_MD_V1"
    assert entries[BUNDLE]["screen"] == "통합DB"
    assert entries[BUNDLE]["editable_fields"] == ["prompt", "variables", "output"]
    for entry in entries.values():
        assert entry["modified_fields"] == []
        assert entry["current"]["revision_no"] == 1
        assert entry["current"]["origin"] == "SEED"
        assert len(entry["history"]) == 1


def test_a_save_changes_one_field_as_a_new_audited_revision_and_a_reset_restores_the_seed(
    client: TestClient, config: AppConfig
) -> None:
    seed = _registry(client)[BUNDLE]
    new_output = '{"product_name":{"recommended":""}}'
    saved = _save(client, "prompts", BUNDLE, "output", new_output, seed["current"]["revision_id"])
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["current"]["revision_no"] == 2
    assert body["current"]["origin"] == "OPERATOR"
    assert body["content"]["output"] == new_output
    # Only that field changed: the others are still the seed's.
    assert {k: v for k, v in body["content"].items() if k != "output"} == {
        k: v for k, v in seed["content"].items() if k != "output"
    }
    assert body["modified_fields"] == ["output"]
    assert [h["revision_no"] for h in body["history"]] == [2, 1]

    reset = _reset(client, "prompts", BUNDLE, "output", body["current"]["revision_id"])
    assert reset.status_code == 200, reset.text
    restored = reset.json()
    assert restored["current"]["revision_no"] == 3
    assert restored["current"]["origin"] == "RESET"
    assert restored["content"] == seed["content"]
    assert restored["modified_fields"] == []
    # History is kept: nothing was overwritten or deleted.
    assert [h["origin"] for h in restored["history"]] == ["RESET", "OPERATOR", "SEED"]

    events = _registry_events(config)
    assert [(e["event_type"], e["action"], e["target_ref"]) for e in events] == [
        ("AI_PROMPT_TEMPLATE_REVISED", "AI_TEMPLATE_OPERATOR", BUNDLE),
        ("AI_PROMPT_TEMPLATE_REVISED", "AI_TEMPLATE_RESET", BUNDLE),
    ]
    assert json.loads(events[0]["details"]) == {
        "layer": "TASK",
        "field": "output",
        "origin": "OPERATOR",
    }
    # An audit event carries identities and fingerprints, never the prompt text.
    for event in events:
        for part in (event["before"], event["after"], event["details"]):
            assert "product_name" not in part and "정확성" not in part


def test_a_platform_policy_is_its_own_store(client: TestClient, config: AppConfig) -> None:
    policy = _registry(client)["POLICY_COUPANG_V1"]
    revision = policy["current"]["revision_id"]
    # The template routes never reach a policy, and the policy routes never reach a template.
    assert _save(client, "prompts", "POLICY_COUPANG_V1", "prompt", "x", revision).status_code == 404
    role = _registry(client)["ROLE_CS_V1"]
    assert (
        _save(
            client, "policies", "ROLE_CS_V1", "prompt", "x", role["current"]["revision_id"]
        ).status_code
        == 404
    )
    saved = _save(client, "policies", "POLICY_COUPANG_V1", "prompt", "쿠팡 정책 수정본", revision)
    assert saved.status_code == 200, saved.text
    assert saved.json()["modified_fields"] == ["prompt"]
    assert [e["event_type"] for e in _registry_events(config)] == ["AI_PLATFORM_POLICY_REVISED"]


def test_a_stale_identical_blank_or_non_editable_save_changes_nothing(
    client: TestClient, config: AppConfig
) -> None:
    role = _registry(client)["ROLE_CS_V1"]
    current = role["current"]["revision_id"]
    first = _save(client, "prompts", "ROLE_CS_V1", "prompt", "CS 역할 수정본", current)
    assert first.status_code == 200
    # Saved against the revision it read, which has since moved: refused, with the current one.
    stale = _save(client, "prompts", "ROLE_CS_V1", "prompt", "다른 수정본", current)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "AI_PROMPT_CURRENT_MOVED"
    moved = first.json()["current"]["revision_id"]
    assert stale.json()["error"]["details"]["current_revision"] == moved
    # Identical to the current text: nothing to save.
    same = _save(client, "prompts", "ROLE_CS_V1", "prompt", "CS 역할 수정본", moved)
    assert (same.status_code, same.json()["error"]["code"]) == (409, "AI_PROMPT_UNCHANGED")
    # A blank text, the task's mode and a field a role does not have are refused.
    blank = _save(client, "prompts", "ROLE_CS_V1", "prompt", "   ", moved)
    assert (blank.status_code, blank.json()["error"]["code"]) == (422, "AI_PROMPT_TEXT_INVALID")
    bundle = _registry(client)[BUNDLE]["current"]["revision_id"]
    mode = _save(client, "prompts", BUNDLE, "mode", "OTHER", bundle)
    assert (mode.status_code, mode.json()["error"]["code"]) == (422, "AI_PROMPT_FIELD_NOT_EDITABLE")
    output = _save(client, "prompts", "ROLE_CS_V1", "output", "{}", moved)
    assert output.json()["error"]["code"] == "AI_PROMPT_FIELD_NOT_EDITABLE"
    unknown = _save(client, "prompts", "ROLE_UNKNOWN_V1", "prompt", "x", moved)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "AI_PROMPT_UNKNOWN")
    # Only the one accepted save was written.
    entry = _registry(client)["ROLE_CS_V1"]
    assert [h["revision_no"] for h in entry["history"]] == [2, 1]
    assert len(_registry_events(config)) == 1


def test_the_preview_composes_the_current_layers_in_the_prototypes_order(
    client: TestClient,
) -> None:
    entries = _registry(client)
    role, policy = "ROLE_PRODUCT_MD_V1", "POLICY_NAVER_V1"
    response = client.get(
        "/api/v1/ai/preview",
        params={"role": role, "policy": policy, "task": BUNDLE},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    text = body["text"]
    task = entries[BUNDLE]["content"]
    order = [
        entries["ICBM_GLOBAL_RULES_V2"]["content"]["prompt"],
        "ROLE_PROFILE",
        entries[role]["content"]["prompt"],
        "PLATFORM_POLICY",
        entries[policy]["content"]["prompt"],
        "TASK_MODE",
        task["mode"],
        task["prompt"],
        "INPUT_VARIABLES",
        task["variables"],
        "OUTPUT_SCHEMA",
        task["output"],
        "RUNTIME_DATA",
        RUNTIME_PLACEHOLDER,
    ]
    # Each part follows the one before it (a later part may also occur earlier, e.g. the global
    # rules name OUTPUT_SCHEMA, so each is searched from where the previous one was found).
    position = 0
    for part in order:
        position = text.index(part, position) + len(part)
    assert text.endswith(RUNTIME_PLACEHOLDER)
    # The composition names the revision each layer was read at.
    assert body["revisions"] == {
        key: entries[key]["current"]["revision_id"]
        for key in ("ICBM_GLOBAL_RULES_V2", role, policy, BUNDLE)
    }
    # A key of the wrong layer is refused.
    wrong = client.get(
        "/api/v1/ai/preview",
        params={"role": BUNDLE, "policy": policy, "task": BUNDLE},
        headers=CLIENT,
    )
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (422, "AI_PROMPT_PREVIEW_INVALID")


def test_the_database_refuses_to_rewrite_or_drop_a_revision(
    client: TestClient, config: AppConfig
) -> None:
    key = "ROLE_DATA_VALIDATOR_V1"
    seed = _registry(client)[key]["current"]["revision_id"]
    assert _save(client, "prompts", key, "prompt", "검증 역할 수정본", seed).status_code == 200
    statements = (
        'UPDATE ai_prompt_template_revisions SET content_json = \'{"prompt":"x"}\'',
        "DELETE FROM ai_prompt_template_revisions",
        "DELETE FROM ai_prompt_template_current",
        "UPDATE ai_prompt_templates SET layer = 'TASK'",
        # A revision of a task must hold all four fields, and one of a role only its prompt.
        f"INSERT INTO ai_prompt_template_revisions VALUES ('r-1', '{key}', 3,"
        ' \'{"prompt":"x","output":"y"}\', \'' + "0" * 64 + "', 'OPERATOR', NULL, 'a', 'c',"
        " '2026-10-08 00:00:00')",
        # The pointer only names its own newest revision.
        f"UPDATE ai_prompt_template_current SET revision_id = '{seed}'"
        f" WHERE template_key = '{key}'",
    )
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    assert [h["revision_no"] for h in _registry(client)[key]["history"]] == [2, 1]


def test_every_layer_of_the_catalog_is_seeded_with_its_own_fields(client: TestClient) -> None:
    for entry in _registry(client).values():
        fields = sorted(entry["content"])
        if entry["layer"] == Layer.TASK:
            assert fields == ["mode", "output", "prompt", "variables"], entry["key"]
        else:
            assert fields == ["prompt"], entry["key"]
        assert all(value.strip() for value in entry["content"].values()), entry["key"]
