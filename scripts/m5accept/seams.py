"""The local provider seams one M5 acceptance run drives (Issue #89 PR-F §A).

They exist because the contracts they stand for are **NOT_ADOPTED**: there is no adopted CREATE,
no adopted product search and no committed session for a read-back, so a run that must exercise
applied, unproven and mismatched outcomes has to say what the provider did. Each fake answers
exactly what the scenario states and counts every call, so the report can show that a *real*
marketplace mutation count stayed zero while the state machine was exercised end to end.

Nothing here reaches a network: they hold no transport and no client. The production seams are
built alongside them and asked what they allow — which is nothing (`owners.production_seams`).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import AppError, ErrorClass
from app.register.provider import CreateHandoff


@dataclass
class FakeSender:
    """The CREATE handoff a scenario declares. It counts calls; it never sends anything."""

    outcome: RemoteOutcome = RemoteOutcome.APPLIED_PROVEN
    product_id: str | None = "9900112233"
    error_class: ErrorClass | None = None
    error_code: str | None = None
    is_available: bool = True
    calls: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        self.calls.append(listing_identity)
        applied = self.outcome is RemoteOutcome.APPLIED_PROVEN
        return CreateHandoff(
            remote_outcome=self.outcome,
            sanitized_request={"listing_identity": listing_identity},
            marketplace_product_id=self.product_id if applied else None,
            response_status=200 if applied else 500,
            sanitized_response={"accepted": applied},
            error_class=self.error_class,
            error_code=self.error_code,
        )


@dataclass
class HarnessAuthority:
    """The harness's stand-in for the ADR-0018 send-time safety stack. It never reaches anything.

    The harness drives the REGISTER CREATE state machine with the fake sender above, so the stack
    the production owners wire — whose M0 execution-mode layer refuses every CREATE — would stop
    every scenario before the state machine it exists to exercise. This seam admits each CREATE
    the state machine reaches and counts it; the real stack's refusals are the unit and
    integration suites'. No marketplace mutation can follow an admission here: the only sender
    behind it is :class:`FakeSender`.
    """

    admitted: list[tuple[str, int]] = field(default_factory=list)

    def admit_create(
        self,
        session: Any,
        *,
        intent: Any,
        attempt_no: int,
        endpoint_adopted: bool,
        scope: Any,
        actor: str,
        correlation_id: str,
    ) -> None:
        self.admitted.append((intent.intent_id, attempt_no))

    def record_refusal(self, refusal: Any, **_: Any) -> None:  # pragma: no cover - never refuses
        raise AssertionError("the harness authority never refuses")


@dataclass
class FakeReadback:
    """What the marketplace would return for a read-back, as the scenario declares it."""

    retained: dict[str, Any] = field(default_factory=dict)
    is_available: bool = True
    calls: int = 0

    def available(self) -> bool:
        return self.is_available

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        self.calls += 1
        return dict(self.retained)


@dataclass
class DeclaredProjection:
    sendable: bool = True
    gaps: tuple[str, ...] = ()


@dataclass
class DeclaredProjector:
    """The wire projection a scenario declares sendable.

    PR-D's real projection reports named gaps — the CREATE request media type and the image
    container shape are not proven — so it is **not sendable**, which is what stops a send in
    production. A run that must exercise the outcome state machine therefore declares a sendable
    projection, and the boundary phase asks the real one and records that it still refuses.
    """

    sendable: bool = True
    calls: int = 0

    def __call__(self, payload: Mapping[str, Any]) -> DeclaredProjection:
        self.calls += 1
        return DeclaredProjection(
            sendable=self.sendable, gaps=() if self.sendable else ("declared gap",)
        )


@dataclass
class DeclaredComparison:
    """PR-D's own comparison, with the one field its contract does not prove declared."""

    inner: Any
    published_state: str

    @property
    def verdict(self) -> Any:
        return self.inner.verdict

    def __getattr__(self, name: str) -> Any:
        # Everything else — the contract and normalizer versions, the reasons — is PR-D's own.
        return getattr(self.inner, name)

    def canonical(self) -> dict[str, Any]:
        evidence = dict(self.inner.canonical())
        normalized = dict(evidence.get("normalized") or {})
        normalized.setdefault("published_state", self.published_state)
        evidence["normalized"] = normalized
        return evidence


@dataclass
class DeclaredComparator:
    """The read-back comparison, run for real, with a declared published state.

    The adopted read-back contract proves no published state (PR-D), so the execution owner
    refuses to confirm a registration rather than invent one. A run that must reach CONFIRMED
    therefore declares that one field and lets **PR-D's real normalizer and comparison** decide
    everything else — the verdict, the per-Item correspondence and the subset rule are not faked.
    """

    inner: Any
    published_state: str = "DECLARED_ON_SALE"
    calls: int = 0

    def compare(
        self, snapshot_payload: Mapping[str, Any], retained: Mapping[str, Any]
    ) -> DeclaredComparison:
        self.calls += 1
        return DeclaredComparison(
            self.inner.compare(snapshot_payload, retained), self.published_state
        )


@dataclass
class RecordingLookup:
    """The reconcile lookup. Unavailable by default, exactly as production is (§10)."""

    is_available: bool = False
    found: dict[str, Any] = field(default_factory=dict)
    calls: int = 0

    def available(self) -> bool:
        return self.is_available

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        self.calls += 1
        if not self.is_available:
            raise AppError(
                "M5_ACCEPTANCE_LOOKUP_NOT_ADOPTED",
                "no product-search contract is adopted, so nothing is looked up",
            )
        return dict(self.found)
