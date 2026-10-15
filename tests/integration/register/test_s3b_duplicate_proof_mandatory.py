"""Owner ruling (Issue #219 ``6190407624``): SmartStore duplicate checking stays on.

"이미 등록되어있는 상품을 중복등록하면 의미가 없잖아" — so for SmartStore:

- a new target-policy revision that turns duplicate proof off is refused, in Korean;
- a stored revision that holds ``false`` is never rewritten, and is read as ``true``;
- the CREATE send path revalidates duplicates whatever a flag or a frozen request says: a request
  frozen without evidence is still checked, and with no answer nothing is sent.

Synthetic values only; no provider is reached.
"""

import contextlib
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.stages.register.execution import (
    DUPLICATE_CHECK_REQUIRED,
    ExecutionRefused,
    RegistrationExecutionService,
)
from app.stages.register.policy import AssetPolicy, TargetPolicy
from app.stages.register.target_policy import (
    TargetPolicyInputsView,
    TargetPolicyStore,
    encode_content,
)
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    api,
    container,
)
from tests.support.gate1_support import CLIENT, MARKET, OPERATOR, policy_inputs
from tests.support.product_support import context
from tests.support.register_support import establish

pytestmark = pytest.mark.integration

BASE = "/api/v1/settings/target-policies"


def test_a_smartstore_revision_cannot_turn_duplicate_checking_off(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    assert MARKET == "smartstore"
    account = establish(container, config, MARKET, "uid-proof-1")
    inputs = {**policy_inputs(account), "duplicate_proof_required": False}
    refused = api.post(
        f"{BASE}/{MARKET}/{account}/revisions",
        json={"actor": OPERATOR, "expected_current_revision": None, "inputs": inputs},
        headers=CLIENT,
    )
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "TARGET_POLICY_DUPLICATE_PROOF_REQUIRED"
    assert "스마트스토어는 중복 확인을 끌 수 없습니다" in error["message"]
    with contextlib.closing(sqlite3.connect(config.database_path)) as raw:
        assert (
            raw.execute("SELECT COUNT(*) FROM registration_target_policy_revisions").fetchone()[0]
            == 0
        )


def test_a_stored_false_is_read_as_true_and_never_rewritten(
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    account = establish(container, config, MARKET, "uid-proof-2")
    content = encode_content(
        MARKET, account, TargetPolicyInputsView.model_validate(policy_inputs(account))
    )
    # A revision written before the ruling held false: written through the store as it was.
    content = {**content, "duplicate_proof_required": False}
    TargetPolicyStore(
        container.db, container.clock, container.audit, container.authoring_revisions
    ).append(
        MARKET,
        account,
        content,
        expected_current_revision=None,
        authored_by=OPERATOR,
        correlation_id="cid-proof",
    )
    target = container.registration_preflight.target_policy(MARKET, account)
    assert target is not None and target.duplicate_proof_required is True
    with contextlib.closing(sqlite3.connect(config.database_path)) as raw:
        stored = raw.execute("SELECT content_json FROM registration_target_policy_revisions")
        assert all('"duplicate_proof_required":false' in row[0] for row in stored)


def test_the_policy_model_never_holds_false_for_smartstore() -> None:
    def target(key: str) -> TargetPolicy:
        return TargetPolicy(
            marketplace_key=key,
            marketplace_account_id="mpa-1",
            policy_revision="p-1",
            taxonomy_revision="t-1",
            pricing_context=context(),
            sanitizer_profile_version="s-1",
            asset_policy=AssetPolicy(profile="a-1"),
            duplicate_proof_required=False,
        )

    assert target("smartstore").duplicate_proof_required is True
    # Another marketplace keeps what its policy says.
    assert target("market_a").duplicate_proof_required is False


class _Preflight:
    def candidate(self, request: Any, *, identity_generation: int) -> Any:
        return SimpleNamespace(request=request)


def _service(check: Any) -> RegistrationExecutionService:
    return RegistrationExecutionService(
        registrations=None,  # type: ignore[arg-type]
        preflight=_Preflight(),  # type: ignore[arg-type]
        sender=None,  # type: ignore[arg-type]
        readback=None,  # type: ignore[arg-type]
        lookup=None,  # type: ignore[arg-type]
        capability=None,  # type: ignore[arg-type]
        compare=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        authority=None,  # type: ignore[arg-type]
        duplicate_check=check,
    )


def test_a_smartstore_send_is_revalidated_whatever_the_flag_says() -> None:
    frozen_without_evidence = SimpleNamespace(duplicate_evidence=None)
    asked: list[Any] = []

    def no_answer(candidate: Any) -> None:
        asked.append(candidate)
        return None

    # SmartStore: even a request frozen with no evidence is checked, and no answer sends nothing.
    with pytest.raises(ExecutionRefused) as refused:
        _service(no_answer)._send_time_duplicates(
            frozen_without_evidence,  # type: ignore[arg-type]
            1,
            "smartstore",
        )
    assert refused.value.code == DUPLICATE_CHECK_REQUIRED
    assert len(asked) == 1
    # Another marketplace whose policy needs no proof keeps its frozen request.
    asked.clear()
    kept = _service(no_answer)._send_time_duplicates(
        frozen_without_evidence,  # type: ignore[arg-type]
        1,
        "market_a",
    )
    assert kept is frozen_without_evidence and asked == []
