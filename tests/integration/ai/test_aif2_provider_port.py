"""ADR-0026 AIF-2: the provider port, its execution and the request composition, with no provider.

Production binds no AI provider: the ``ai`` capability is NOT_CONFIGURED and an execution is
refused before anything is sent or written. The port's behaviour with a provider is proven only
with deterministic fakes defined here; no vendor SDK, credential or network exists.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.capabilities.ai.composer import ComposedRequest, PlatformLimits
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.prompts import ReviseRequest
from app.capabilities.ai.provider import (
    AIExecutionProvenance,
    BillingMode,
    NoAIProvider,
    ProviderOutcome,
    RequestedIdentity,
    TaskRequest,
)
from app.container import Container
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, RateLimitedError
from app.stages.connect.state import CapabilityStatus

pytestmark = pytest.mark.integration

BUNDLE = "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"
IDENTITY = RequestedIdentity("fake-provider", "fake-model-1", "routing-1", "proxy-none")


def _provenance(identity: RequestedIdentity = IDENTITY, **overrides: Any) -> AIExecutionProvenance:
    values: dict[str, Any] = {
        "requested_provider": identity.requested_provider,
        "requested_model": identity.requested_model,
        "actual_provider": "fake-provider",
        "actual_model": "fake-model-1-0101",
        "proxy": None,
        "alias_or_fallback": None,
        "billing_mode": BillingMode.SUBSCRIPTION,
        "tokens_in": 120,
        "tokens_out": 40,
        "vendor_cost": None,
        "estimated_cost": None,
        "latency_ms": 85,
    }
    values.update(overrides)
    return AIExecutionProvenance(**values)


@dataclass
class FakeProvider:
    """A deterministic provider: it answers what the test scripts and records every request."""

    outcome: ProviderOutcome | None = None
    raises: Exception | None = None
    identity: RequestedIdentity | None = IDENTITY
    requests: list[TaskRequest] = field(default_factory=list)

    def requested_identity(self) -> RequestedIdentity | None:
        return self.identity

    def execute(self, request: TaskRequest) -> ProviderOutcome:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        assert self.outcome is not None
        return self.outcome


@dataclass
class FakeLimits:
    owner: str
    revision: str
    values: Mapping[str, Any]
    contexts: list[Mapping[str, Any]] = field(default_factory=list)

    def limits(self, context: Mapping[str, Any]) -> PlatformLimits | None:
        self.contexts.append(dict(context))
        return PlatformLimits(self.owner, self.revision, self.values)


@pytest.fixture
def seeded(container: Container) -> Container:
    container.prompt_registry.seed_on_startup()
    return container


def _audit_count(container: Container) -> int:
    return len(container.audit.list_events(limit=1000))


def _composed(container: Container) -> ComposedRequest:
    return container.ai_composer.compose(BUNDLE, {"product_id": "p-1", "original_name": "의자"})


def test_with_no_provider_the_capability_is_not_configured_and_nothing_is_sent(
    seeded: Container,
) -> None:
    report = seeded.ai_execution.capability()
    assert (report.key, report.status) == ("ai", CapabilityStatus.NOT_CONFIGURED)
    assert seeded.ai_execution.identity() is None
    # It is a capability, never a core check: it is only listed as degraded (the served
    # application's /api/ready stays READY with it, test_connect_api).
    readiness = seeded.readiness.check()
    assert "ai" in readiness.degraded_capabilities
    assert not [check for check in readiness.checks if "ai" in check.name.lower().split("_")]
    before = _audit_count(seeded)
    with pytest.raises(AppError) as refused:
        seeded.ai_execution.run(_composed(seeded), correlation_id="c-1")
    assert refused.value.code == "AI_PROVIDER_NOT_CONFIGURED"
    assert refused.value.error_class is ErrorClass.POLICY_BLOCKED
    assert _audit_count(seeded) == before
    with pytest.raises(AppError):
        NoAIProvider().execute(TaskRequest(BUNDLE, "x", "c-2"))


def test_the_request_composes_the_current_layers_with_the_runtime_data(seeded: Container) -> None:
    registry = {entry.key: entry for entry in seeded.prompt_registry.registry().entries}
    composed = _composed(seeded)
    assert (composed.role_key, composed.policy_key, composed.mode) == (
        "ROLE_PRODUCT_MD_V1",
        "POLICY_COMMON_MARKET_V1",
        "PRODUCT_RECOMMEND_BUNDLE",
    )
    runtime = composed.text.split("RUNTIME_DATA\n" + "-" * 40 + "\n", 1)[1]
    assert json.loads(runtime) == {"original_name": "의자", "product_id": "p-1"}
    assert composed.prompt_version == {
        key: registry[key].current.revision_id
        for key in ("ICBM_GLOBAL_RULES_V2", "ROLE_PRODUCT_MD_V1", BUNDLE)
    }
    assert composed.policy_version == {
        "POLICY_COMMON_MARKET_V1": registry["POLICY_COMMON_MARKET_V1"].current.revision_id
    }
    # A saved role prompt is what the next composition reads, at its new revision.
    seeded.prompt_registry.revise(
        "ROLE_PRODUCT_MD_V1",
        "template",
        ReviseRequest(
            actor="operator",
            expected_current_revision=registry["ROLE_PRODUCT_MD_V1"].current.revision_id,
            field="prompt",
            text="ROLE\n수정된 MD 역할",
        ),
        cid="c-3",
    )
    again = _composed(seeded)
    assert "수정된 MD 역할" in again.text
    assert (
        again.prompt_version["ROLE_PRODUCT_MD_V1"] != composed.prompt_version["ROLE_PRODUCT_MD_V1"]
    )
    assert again.prompt_version[BUNDLE] == composed.prompt_version[BUNDLE]


def test_a_policy_carries_the_limits_its_owner_holds_at_their_revision(seeded: Container) -> None:
    from app.capabilities.ai.composer import PromptComposer
    from app.capabilities.ai.prompts import PromptRegistryStore

    limits = FakeLimits("register.category_metadata", "meta-rev-7", {"name_max_length": 100})
    composer = PromptComposer(
        PromptRegistryStore(seeded.db, seeded.clock, seeded.audit),
        {"POLICY_NAVER_V1": (limits,)},
    )
    composed = composer.compose(
        "TASK_CATEGORY_REMATCH_V1",
        {"platform": "smartstore"},
        policy_key="POLICY_NAVER_V1",
        context={"category_id": "50000803"},
    )
    assert "PLATFORM_LIMITS" in composed.text and '"name_max_length": 100' in composed.text
    assert composed.policy_version["limits:register.category_metadata"] == "meta-rev-7"
    assert limits.contexts == [{"category_id": "50000803"}]
    # Another marketplace's policy references no owner here.
    other = composer.compose("TASK_CATEGORY_REMATCH_V1", {}, policy_key="POLICY_COUPANG_V1")
    assert "PLATFORM_LIMITS" not in other.text
    assert list(other.policy_version) == ["POLICY_COUPANG_V1"]


def test_only_a_registry_task_with_a_role_and_a_platform_policy_is_composed(
    seeded: Container,
) -> None:
    for task, policy, code in (
        ("ROLE_CS_V1", None, "AI_TASK_UNKNOWN"),
        ("TASK_TAG_RECOMMEND_V1", None, "AI_TASK_HAS_NO_ROLE"),
        (BUNDLE, "ROLE_CS_V1", "AI_POLICY_UNKNOWN"),
    ):
        with pytest.raises(InputValidationError) as refused:
            seeded.ai_composer.compose(task, {}, policy_key=policy)
        assert refused.value.code == code


def test_an_outcome_keeps_the_providers_provenance_and_classifies_every_failure(
    seeded: Container,
) -> None:
    composed = _composed(seeded)
    value = {"product_name": {"recommended": "메쉬 사무용 의자"}}
    fake = FakeProvider(outcome=ProviderOutcome(ok=True, provenance=_provenance(), value=value))
    done = AIExecution(fake).run(composed, correlation_id="c-4")
    assert done.outcome.ok and done.outcome.value == value
    assert done.outcome.provenance == _provenance()
    assert done.identity == IDENTITY
    assert [r.text for r in fake.requests] == [composed.text]
    assert AIExecution(fake).capability().status is CapabilityStatus.READY

    # A provider that answers for another requested model is never trusted.
    mismatched = FakeProvider(
        outcome=ProviderOutcome(
            ok=True, provenance=_provenance(requested_model="other-model"), value=value
        )
    )
    failed = AIExecution(mismatched).run(composed, correlation_id="c-5").outcome
    assert (failed.ok, failed.error_code, failed.value) == (False, "AI_PROVENANCE_MISMATCH", None)

    # A non-object answer is a failed task.
    listed = FakeProvider(outcome=ProviderOutcome(ok=True, provenance=_provenance(), value=None))
    assert AIExecution(listed).run(composed, correlation_id="c-6").outcome.error_code == (
        "AI_OUTPUT_NOT_OBJECT"
    )

    # A rate limit is retryable; its provenance holds only the request, nothing invented.
    limited = AIExecution(FakeProvider(raises=RateLimitedError("AI_RATE_LIMITED", "slow down")))
    execution = limited.run(composed, correlation_id="c-7")
    assert (execution.outcome.error_class, execution.retryable) == (ErrorClass.RATE_LIMITED, True)
    unknown = execution.outcome.provenance
    assert (unknown.actual_model, unknown.tokens_in, unknown.billing_mode) == (
        None,
        None,
        BillingMode.UNKNOWN,
    )

    # An adapter fault is a failed task, never a crash, and never retried.
    crashed = AIExecution(FakeProvider(raises=RuntimeError("boom"))).run(
        composed, correlation_id="c-8"
    )
    assert (crashed.outcome.error_code, crashed.outcome.error_class, crashed.retryable) == (
        "AI_PROVIDER_FAILED",
        ErrorClass.UNKNOWN,
        False,
    )
