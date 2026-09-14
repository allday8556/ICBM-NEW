"""Sanitized M2 evidence (Issue #46 §2.2; docs/acceptance/M2.md §7): the schema is closed and has
no place for a secret; the writer refuses every encoding of every known secret and any bearer
credential, writes nothing when it refuses, and never echoes the value; fingerprints are keyed."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.connect.marketplace.capability import AuthStatus
from app.system.secret_scan import VARIANTS, variants
from integrations.marketplaces.smartstore.registry import EndpointId
from scripts.m2harness import crash
from scripts.m2harness.evidence import (
    SCHEMA_VERSION,
    EvidenceRejected,
    EvidenceWriter,
    Fingerprinter,
    load_schema,
    validate,
)
from scripts.m2harness.fake_provider import Scenario
from scripts.m2harness.ledger import (
    LABELS,
    SELLER,
    TOKEN,
    UNRECOGNIZED,
    CrashVerdict,
    Phase,
    Refusal,
    State,
)

NOW = "2026-09-15T00:00:00.000+00:00"
SCENARIO = Scenario.fixture()
SECRETS = {
    "client_secret": SCENARIO.client_secret,
    "client_id": SCENARIO.client_id,
    "bearer": "dry-bearer.T1.1726000000000.0123456789abcdef0123456789abcdef",
    "account_uid": SCENARIO.account_uid,
    "account_id": SCENARIO.account_id,
}
# Names that would carry a secret or a raw identity. None may exist anywhere in the schema.
FORBIDDEN_FIELDS = {
    "client_secret",
    "client_id",
    "access_token",
    "token",
    "token_hash",
    "authorization",
    "bearer",
    "account_uid",
    "accountuid",
    "account_id",
    "accountid",
    "observed_account_uid",
    "observed_account_id",
    "provider_account_uid",
}


def _document() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": "m2-unit-evidence",
        "mode": "DRY",
        "generated_at": NOW,
        "git": {"head": "a" * 40, "approved_sha": None, "tree_clean": False},
        "observed_range": {"started_at": NOW, "finished_at": NOW},
        "campaign_state": "COMPLETED",
        "campaign_outcome": "COMPLETED",
        "renewal_margin_s": 600,
        "budget": {
            "caps": {TOKEN: 8, SELLER: 6, "OTHER": 0},
            "used": {TOKEN: 3, SELLER: 4, "OTHER": 0},
            "crash_attempt_cap": 2,
            "crash_attempts_used": 1,
            "blocked_before_send": [],
        },
        "requests": [],
        "calls": [],
        "phases": [],
        "sessions": [],
        "identity": {
            "application_fingerprint": None,
            "credential_generation": None,
            "baseline_account_fingerprint": None,
            "observations": [],
            "a3_comparison": None,
            "a3_reason": None,
        },
        "crash": {"attempts": [], "recovery": None},
        "slots": [],
        "scan": None,
        "reconcile": None,
        "regression": [],
        "steps": [{"name": "baseline.check", "result": "PASS", "at": NOW, "detail": "auth=READY"}],
    }


def _objects(node: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_objects(value))
    return found


def test_a_complete_document_is_accepted() -> None:
    assert validate(_document()) == []
    assert json.loads(EvidenceWriter(SECRETS).render(_document()))["mode"] == "DRY"


def test_every_object_in_the_schema_is_closed() -> None:
    for schema in _objects(load_schema()):
        kinds = schema.get("type")
        if kinds == "object" or (isinstance(kinds, list) and "object" in kinds):
            extra = schema.get("additionalProperties")
            assert extra is False or (isinstance(extra, dict) and "propertyNames" in schema), schema


def test_the_schema_has_no_field_for_a_secret_or_a_raw_identity() -> None:
    names = {
        name.lower()
        for schema in _objects(load_schema())
        for name in schema.get("properties", {})
        if isinstance(schema.get("properties"), dict)
    }
    assert names & FORBIDDEN_FIELDS == set()


def _enum(ref: str) -> set[object]:
    schema = load_schema()
    node: Any = schema
    for part in ref.split("/"):
        node = node[part]
    return {value for value in node["enum"] if value is not None}


def test_the_schema_enums_are_exactly_the_harness_values() -> None:
    assert _enum("$defs/state") == {state.value for state in State}
    assert _enum("$defs/phase") == {phase.value for phase in Phase}
    assert _enum("$defs/label") == set(LABELS)
    assert _enum("$defs/endpoint_any") == {e.value for e in EndpointId} | {UNRECOGNIZED}
    reasons = "properties/budget/properties/blocked_before_send/items/properties/reason"
    assert _enum(reasons) == {refusal.value for refusal in Refusal}
    attempt = "properties/crash/properties/attempts/items/properties"
    assert _enum(f"{attempt}/verdict") == {verdict.value for verdict in CrashVerdict}
    assert _enum(f"{attempt}/reasons/items") == {reason.value for reason in crash.Reason}
    observation = "properties/identity/properties/observations/items/properties/auth"
    assert _enum(observation) == {status.value for status in AuthStatus}


@pytest.mark.parametrize("label", sorted(SECRETS))
@pytest.mark.parametrize("variant", VARIANTS)
def test_the_writer_refuses_every_encoding_of_every_secret(label: str, variant: str) -> None:
    secret = SECRETS[label]
    for form in variants(secret)[variant]:
        data = b'{"steps": [{"detail": "prefix ' + form + b' suffix"}]}'
        with pytest.raises(EvidenceRejected) as caught:
            EvidenceWriter(SECRETS).guard(data)
        assert secret not in str(caught.value)


@pytest.mark.parametrize("label", sorted(SECRETS))
def test_a_document_that_carries_a_secret_is_not_written(tmp_path: Path, label: str) -> None:
    document = _document()
    document["steps"][0]["detail"] = f"auth READY {SECRETS[label]}"
    target = tmp_path / "evidence.json"
    with pytest.raises(EvidenceRejected) as caught:
        EvidenceWriter(SECRETS).write(target, document)
    assert not target.exists()
    assert SECRETS[label] not in str(caught.value)


def test_the_writer_refuses_any_bearer_credential() -> None:
    with pytest.raises(EvidenceRejected):
        EvidenceWriter({}).guard(b'{"detail": "Authorization: Bearer abcdefgh12345678"}')
    EvidenceWriter({}).guard(b'{"token_type": "Bearer"}')  # the type name alone is not a credential


def test_an_unknown_field_is_refused_without_echoing_it() -> None:
    document = _document()
    document["git"]["authorization"] = SECRETS["bearer"]
    errors = validate(document)
    assert errors == ["$.git: an unexpected field"]
    assert all(SECRETS["bearer"] not in error and "authorization" not in error for error in errors)


def test_fingerprints_are_keyed_and_not_the_plain_hash() -> None:
    uid = SCENARIO.account_uid
    one, other = Fingerprinter(b"k" * 32), Fingerprinter(b"j" * 32)
    assert one.account(uid) != hashlib.sha256(uid.encode()).hexdigest()
    assert one.account(uid) != other.account(uid)
    assert one.account(uid) != one.application(uid)  # domain-separated
    assert len(one.account(uid)) == 64 and uid not in one.account(uid)
    with pytest.raises(ValueError):
        Fingerprinter(b"short")
