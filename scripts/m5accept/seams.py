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
