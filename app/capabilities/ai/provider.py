"""The AI provider port (ADR-0012 §1, ADR-0026 §4; AIF-2), with no provider.

ICBM runs with no AI provider at all (ADR-0012 §1, ADR-0014 §18). The port is one task request in,
one typed outcome and its execution provenance out. The production container binds
``NoAIProvider``: it names no requested identity, so the ``ai`` capability reads NOT_CONFIGURED,
which never fails core readiness (ADR-0012 §9), and every execution is refused before anything is
sent. A vendor adapter, its credential and its egress grant arrive only with the owner's decision
on the provider, model, key, cost cap and data transfer (ADR-0026 AIF-11).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from app.platform.core.errors import AUTO_RETRYABLE, ErrorClass, PolicyBlockedError
from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus

AI_PROVIDER_NOT_CONFIGURED: Final = "AI_PROVIDER_NOT_CONFIGURED"
AI_CAPABILITY_KEY: Final = "ai"


class BillingMode(StrEnum):
    """ADR-0012 §7: unknown cost is not zero cost."""

    METERED_API = "METERED_API"
    SUBSCRIPTION = "SUBSCRIPTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RequestedIdentity:
    """What a request asks for, before the call (ADR-0012 §6). It is part of the fingerprint; the
    model that actually answered never is."""

    requested_provider: str
    requested_model: str
    routing_config_version: str
    proxy_version: str


@dataclass(frozen=True)
class AIExecutionProvenance:
    """What answered, as the provider reported it: exactly ADR-0012 §6's record. It is copied from
    the outcome, never inferred or fabricated; an unknown value stays ``None`` (an unmeasured cost
    is never 0, §7). ``allocated_cost`` is an internal allocation, never vendor-billed cost. No
    secret is ever here."""

    requested_provider: str
    requested_model: str

    actual_provider: str | None
    actual_model: str | None

    proxy_name: str | None
    proxy_version: str | None
    proxy_binary_sha256: str | None
    routing_config_version: str | None

    alias_applied: bool | None
    fallback_applied: bool | None

    billing_mode: BillingMode
    tokens_in: int | None
    tokens_out: int | None
    vendor_cost: str | None
    estimated_cost: str | None
    allocated_cost: str | None

    latency_ms: int | None


@dataclass(frozen=True)
class TaskRequest:
    """One composed task request (``ComposedRequest.text``) for one task."""

    task_key: str
    text: str
    correlation_id: str


@dataclass(frozen=True)
class ProviderOutcome:
    """The provider's answer: the parsed JSON object of an ``OK`` call, or the classified error of
    a failed one, always with its provenance."""

    ok: bool
    provenance: AIExecutionProvenance
    value: dict[str, Any] | None = None
    error_class: ErrorClass | None = None
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class AIProvider(Protocol):
    def requested_identity(self) -> RequestedIdentity | None:
        """The identity a request would ask for, or ``None`` when no provider is configured."""
        ...

    def execute(self, request: TaskRequest) -> ProviderOutcome: ...


class NoAIProvider:
    """The production binding until the owner adopts a provider: nothing is configured, nothing is
    ever sent."""

    def requested_identity(self) -> RequestedIdentity | None:
        return None

    def execute(self, request: TaskRequest) -> ProviderOutcome:
        raise not_configured()


def not_configured() -> PolicyBlockedError:
    return PolicyBlockedError(
        AI_PROVIDER_NOT_CONFIGURED,
        "no AI provider is configured; ICBM runs without one (ADR-0012 §1)",
    )


def capability(provider: AIProvider) -> CapabilityReport:
    """The ``ai`` capability for readiness (ADR-0012 §9): never a core failure. A provider that
    reports its own state (the profiled sidecar, ADR-0027 §8) answers itself."""
    own = getattr(provider, "capability_report", None)
    if callable(own):
        report: CapabilityReport = own()
        return report
    identity = provider.requested_identity()
    if identity is None:
        return CapabilityReport(
            key=AI_CAPABILITY_KEY,
            status=CapabilityStatus.NOT_CONFIGURED,
            detail="no AI provider is configured",
        )
    return CapabilityReport(
        key=AI_CAPABILITY_KEY,
        status=CapabilityStatus.READY,
        detail=f"provider={identity.requested_provider} model={identity.requested_model}",
    )


def retryable(error_class: ErrorClass | None) -> bool:
    """ADR-0012 §8: only a transient or rate-limited failure is retried automatically."""
    return error_class in AUTO_RETRYABLE
