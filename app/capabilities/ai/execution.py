"""One composed task request through the provider port (ADR-0012 §6–§8, ADR-0026 §4; AIF-2).

With no provider the request is refused before anything is sent or written
(``AI_PROVIDER_NOT_CONFIGURED``, AIF-08). With one, its outcome is checked against the identity the
request asked for, and a provider failure becomes a classified ``FAILED`` outcome whose
provenance holds only what is known: the requested identity, and ``None`` or ``UNKNOWN`` for what
the provider never reported (ADR-0012 §6: never inferred or fabricated; §7: unknown cost is not
zero). Persisting the outcome is the result owner's (AIF-3).
"""

from dataclasses import dataclass, replace
from typing import Final

from app.capabilities.ai.composer import ComposedRequest
from app.capabilities.ai.provider import (
    AIExecutionProvenance,
    AIProvider,
    BillingMode,
    ProviderOutcome,
    RequestedIdentity,
    TaskRequest,
    capability,
    not_configured,
    retryable,
)
from app.platform.core.errors import AppError, ErrorClass
from app.stages.connect.contracts import CapabilityReport

AI_PROVENANCE_MISMATCH: Final = "AI_PROVENANCE_MISMATCH"
AI_OUTPUT_NOT_OBJECT: Final = "AI_OUTPUT_NOT_OBJECT"
AI_PROVIDER_FAILED: Final = "AI_PROVIDER_FAILED"


@dataclass(frozen=True)
class Execution:
    request: ComposedRequest
    identity: RequestedIdentity
    outcome: ProviderOutcome

    @property
    def retryable(self) -> bool:
        return not self.outcome.ok and retryable(self.outcome.error_class)


def unknown_provenance(identity: RequestedIdentity) -> AIExecutionProvenance:
    """The provenance of a task no answer reported on (refused before any call, or with no
    report): the requested identity, and unknown for everything only an answer could tell."""
    return _unknown(identity)


def _unknown(identity: RequestedIdentity) -> AIExecutionProvenance:
    """The provenance of a call whose answer reported nothing: what the request asked for, and
    ``None`` or ``UNKNOWN`` for everything only the answer could have told."""
    return AIExecutionProvenance(
        requested_provider=identity.requested_provider,
        requested_model=identity.requested_model,
        actual_provider=None,
        actual_model=None,
        proxy_name=None,
        proxy_version=identity.proxy_version,
        proxy_binary_sha256=None,
        routing_config_version=identity.routing_config_version,
        alias_applied=None,
        fallback_applied=None,
        billing_mode=BillingMode.UNKNOWN,
        tokens_in=None,
        tokens_out=None,
        vendor_cost=None,
        estimated_cost=None,
        allocated_cost=None,
        latency_ms=None,
    )


def _requested(provenance: AIExecutionProvenance) -> tuple[str, str, str | None, str | None]:
    return (
        provenance.requested_provider,
        provenance.requested_model,
        provenance.routing_config_version,
        provenance.proxy_version,
    )


class AIExecution:
    def __init__(self, provider: AIProvider) -> None:
        self._provider = provider

    def capability(self) -> CapabilityReport:
        return capability(self._provider)

    def identity(self) -> RequestedIdentity | None:
        return self._provider.requested_identity()

    def run(self, request: ComposedRequest, *, correlation_id: str) -> Execution:
        identity = self._provider.requested_identity()
        if identity is None:
            raise not_configured()
        try:
            outcome = self._provider.execute(
                TaskRequest(request.task_key, request.text, correlation_id)
            )
        except AppError as error:
            outcome = ProviderOutcome(
                ok=False,
                provenance=_unknown(identity),
                error_class=error.error_class,
                error_code=error.code,
            )
        except Exception as error:  # an adapter fault is a failed task, never a crash
            outcome = ProviderOutcome(
                ok=False,
                provenance=_unknown(identity),
                error_class=ErrorClass.UNKNOWN,
                error_code=AI_PROVIDER_FAILED,
                details={"exception": type(error).__name__},
            )
        # An answer is trusted only for the exact identity it was asked under: the provider, the
        # model, the routing configuration and the proxy version (ADR-0012 §6).
        asked = (
            identity.requested_provider,
            identity.requested_model,
            identity.routing_config_version,
            identity.proxy_version,
        )
        if _requested(outcome.provenance) != asked:
            outcome = replace(
                outcome,
                ok=False,
                value=None,
                provenance=_unknown(identity),
                error_class=ErrorClass.CONFLICT,
                error_code=AI_PROVENANCE_MISMATCH,
            )
        elif outcome.ok and not isinstance(outcome.value, dict):
            outcome = replace(
                outcome,
                ok=False,
                value=None,
                error_class=ErrorClass.VALIDATION,
                error_code=AI_OUTPUT_NOT_OBJECT,
            )
        return Execution(request=request, identity=identity, outcome=outcome)
