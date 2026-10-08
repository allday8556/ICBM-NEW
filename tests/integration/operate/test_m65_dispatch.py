"""M6.5-C the DISPATCH stage (ADR-0025 §5): the shipment of one fulfillable product order.

A dispatch is a marketplace mutation: it runs only through the send-time safety stack, under its
own exact DISPATCH grant, inside a bounded LIVE window with the brake released and the 주문 판매자
group attested. Its attempt is opened, with the grant spent, before any byte is sent. An UNKNOWN
dispatch is never resent and opens no new grant; only a provider's per-order failure that a
read-back shows undispatched reopens the way. No provider is reached: the sender and the order
read are fakes; the orders, supplier orders, grants, brake and audit are the real owners'.
"""

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from app.capabilities.live_safety.model import GrantState, MutationRefused, MutationStage
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.container import Container
from app.stages.connect.accounts import AccountBinding, MarketplaceAccountStore
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.dispatch import (
    APPLIED_PROVEN,
    CONFLICT,
    DISPATCH_CONFIRMED,
    NOT_APPLIED_PROVEN,
    REJECTED,
    UNDISPATCHED,
    UNKNOWN,
    DispatchHandoff,
    DispatchRefused,
    DispatchService,
)
from app.stages.operate.fulfillment import (
    DISPATCH_CONFLICT,
    DISPATCH_REJECTED,
    DISPATCH_SENT,
    DISPATCH_UNKNOWN,
    FulfillmentConflict,
)
from app.stages.operate.order_facts import ProductOrderFacts
from integrations.marketplaces.smartstore.dispatch import SmartStoreDispatchSender
from tests.integration.operate.test_m6_orders import (  # noqa: F401 - fixtures
    CID,
    _facts,
    registration,
)
from tests.integration.operate.test_m65_fulfillment import _fulfillment, _ingest, _record
from tests.integration.register.test_m5_registration_foundation import (  # noqa: F401 - fixtures
    MARKET,
    account,
    sources,
    store,
)
from tests.support.live_safety_support import PermittedMode, ProvenProofs

pytestmark = pytest.mark.integration

APPROVAL = "6053008136"
PO = "po-1"
TRACKING = "6000-1111"


@dataclass
class FakeSender:
    handoffs: list[DispatchHandoff] = field(default_factory=list)
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    def endpoint_adopted(self) -> bool:
        return True

    def send(
        self, *, product_order_id: str, carrier_code: str, tracking_number: str, dispatch_date: Any
    ) -> DispatchHandoff:
        self.sent.append((product_order_id, carrier_code, tracking_number))
        return self.handoffs.pop(0)


@dataclass
class FakeReader:
    answers: list[Any] = field(default_factory=list)
    reads: int = 0

    def details(self, product_order_ids: Any) -> tuple[ProductOrderFacts, ...]:
        self.reads += 1
        answer = self.answers.pop(0) if self.answers else ()
        if isinstance(answer, Exception):
            raise answer
        return answer


SUCCESS = DispatchHandoff(RemoteOutcome.APPLIED_PROVEN, listed="SUCCESS", response_status=200)
FAILED = DispatchHandoff(
    RemoteOutcome.APPLIED_PROVEN, listed="FAIL", fail_code="104122", response_status=200
)
LOST = DispatchHandoff(RemoteOutcome.UNKNOWN, error_code="READ_TIMEOUT")
PRECLUDED = DispatchHandoff(RemoteOutcome.NOT_APPLIED_PROVEN, error_code="TCP_CONNECT_FAILURE")


def _shipped(tracking: str = TRACKING, company: str = "CJGLS") -> tuple[ProductOrderFacts, ...]:
    return (
        replace(_facts(), status="DELIVERING", tracking_number=tracking, delivery_company=company),
    )


def _waiting() -> tuple[ProductOrderFacts, ...]:
    return (_facts(),)


def _bound(container: Container) -> Any:
    accounts = MarketplaceAccountStore(container.db, container.clock, container.audit)

    def account_of(marketplace_key: str) -> str | None:
        for record in accounts.accounts(marketplace_key):
            if (
                accounts.binding(marketplace_key, record.marketplace_account_id)
                is AccountBinding.BOUND
            ):
                return record.marketplace_account_id
        return None

    return account_of


def _service(
    container: Container,
    sender: FakeSender,
    reader: FakeReader,
    *,
    attested: bool = True,
    mode: Any = None,
) -> DispatchService:
    stack = SafetyStack(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        mode=mode or PermittedMode(),
        proofs=ProvenProofs(),
        clock=container.clock,
    )
    return DispatchService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        sender=sender,
        authority=stack,
        reader=reader,
        order_group_attested=lambda: attested,
        account_of=_bound(container),
    )


def _ready(container: Container) -> None:
    """One MATCHED PAYED order with a recorded supplier order and captured tracking."""
    _ingest(container, _facts())
    service = _fulfillment(container)
    _record(service)
    service.capture_tracking(
        PO,
        carrier_code="CJGLS",
        tracking_number=TRACKING,
        expected_revision=1,
        actor="operator",
        correlation_id=CID,
    )


def _grant(container: Container) -> str:
    now = container.clock.now()
    return container.live_authority.issue_dispatch_grant(
        product_order_id=PO,
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by="operator",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    ).grant_id


def _release(container: Container) -> None:
    container.live_authority.release_brake(
        actor="operator",
        reason_code="DISPATCH_WINDOW",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )


def _dispatch(service: DispatchService) -> Any:
    return service.dispatch(PO, actor="operator", correlation_id=CID)


def _grant_state(container: Container, grant_id: str) -> GrantState:
    with LiveAuthorityStore(container.db, container.clock, container.audit).reading() as unit:
        record = unit.grant_record(grant_id)
        assert record is not None
        return record.state


def test_without_every_layer_nothing_is_sent_and_nothing_is_spent(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    grant = _grant(container)
    sender = FakeSender([SUCCESS])
    # The production execution mode is M0_DRY_RUN_ONLY and the brake is engaged.
    stack_mode_default = _service(container, sender, FakeReader(), mode=container.execution_mode)
    with pytest.raises(MutationRefused) as refused:
        _dispatch(stack_mode_default)
    layers = {layer["layer"] for layer in refused.value.details["layers"]}
    assert {"EXECUTION_MODE", "PROTECTED_WRITE_BRAKE"} <= layers
    assert sender.sent == [] and stack_mode_default.attempts(PO) == ()
    assert _grant_state(container, grant) is GrantState.ACTIVE
    with sqlite3.connect(container.config.database_path) as raw:
        (refusals,) = raw.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'LIVE_MUTATION_REFUSED'"
            " AND target_ref = 'product_order:po-1'"
        ).fetchone()
    assert refusals == 1


def test_the_exact_grant_and_the_attested_group_are_layers(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    sender = FakeSender([SUCCESS])
    with pytest.raises(MutationRefused) as missing:
        _dispatch(_service(container, sender, FakeReader()))
    assert missing.value.code == "LIVE_GRANT_NO_MATCHING_ACTIVE_GRANT"
    _grant(container)
    with pytest.raises(MutationRefused) as unattested:
        _dispatch(_service(container, sender, FakeReader(), attested=False))
    assert unattested.value.code == "LIVE_ORDER_GROUP_NOT_ATTESTED"
    # A grant binds the supplier order revision it was issued for: amending the tracking leaves
    # it matching nothing.
    _fulfillment(container).capture_tracking(
        PO,
        carrier_code="HANJIN",
        tracking_number="7000",
        expected_revision=2,
        actor="operator",
        correlation_id=CID,
    )
    with pytest.raises(MutationRefused) as moved:
        _dispatch(_service(container, sender, FakeReader()))
    assert moved.value.code == "LIVE_GRANT_NO_MATCHING_ACTIVE_GRANT"
    assert sender.sent == []


def test_an_applied_dispatch_is_confirmed_and_never_sent_again(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    grant = _grant(container)
    sender = FakeSender([SUCCESS])
    reader = FakeReader([_shipped()])
    attempt = _dispatch(_service(container, sender, reader))
    assert sender.sent == [(PO, "CJGLS", TRACKING)]
    assert (attempt.state, attempt.verification, attempt.attempt_no) == (
        APPLIED_PROVEN,
        DISPATCH_CONFIRMED,
        1,
    )
    assert _grant_state(container, grant) is GrantState.EXHAUSTED
    assert _fulfillment(container).states([PO]) == {PO: DISPATCH_SENT}
    # Nothing re-opens a dispatched order: no new grant, and the tracking is frozen.
    with pytest.raises(DispatchRefused) as blocked:
        _grant(container)
    assert blocked.value.code == "OPERATE_DISPATCH_BLOCKED"
    with pytest.raises(FulfillmentConflict) as frozen:
        _fulfillment(container).capture_tracking(
            PO,
            carrier_code="HANJIN",
            tracking_number="7000",
            expected_revision=2,
            actor="operator",
            correlation_id=CID,
        )
    assert frozen.value.code == "OPERATE_TRACKING_FROZEN"


def test_an_unknown_dispatch_is_never_resent_and_only_its_tracking_resolves_it(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    _grant(container)
    sender = FakeSender([LOST])
    # The read right after shows the order still PAYED without tracking: it proves nothing.
    reader = FakeReader([_waiting()])
    service = _service(container, sender, reader)
    attempt = _dispatch(service)
    assert (attempt.state, attempt.verification) == (UNKNOWN, None)
    assert _fulfillment(container).states([PO]) == {PO: DISPATCH_UNKNOWN}
    # GPT audit (PR #262): no new grant after an UNKNOWN, whatever a read shows.
    with pytest.raises(DispatchRefused):
        _grant(container)
    assert service.verify(PO, actor="operator", correlation_id=CID).verification is None  # type: ignore[union-attr]
    reader.answers.append(_shipped())
    confirmed = service.verify(PO, actor="operator", correlation_id=CID)
    assert confirmed is not None and confirmed.verification == DISPATCH_CONFIRMED
    assert sender.sent == [(PO, "CJGLS", TRACKING)]


def test_a_rejected_dispatch_reopens_only_once_a_read_back_shows_it_undispatched(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    _grant(container)
    sender = FakeSender([FAILED, SUCCESS])
    reader = FakeReader([RuntimeError("read failed")])
    service = _service(container, sender, reader)
    attempt = _dispatch(service)
    # A failed read-back records nothing: the rejection still blocks.
    assert (attempt.state, attempt.fail_code, attempt.verification) == (REJECTED, "104122", None)
    assert _fulfillment(container).states([PO]) == {PO: DISPATCH_REJECTED}
    with pytest.raises(DispatchRefused):
        _grant(container)
    reader.answers.append(_waiting())
    shown = service.verify(PO, actor="operator", correlation_id=CID)
    assert shown is not None and shown.verification == UNDISPATCHED
    # Now the tracking may be corrected and a new grant binds the new revision.
    _fulfillment(container).capture_tracking(
        PO,
        carrier_code="CJGLS",
        tracking_number="6000-2222",
        expected_revision=2,
        actor="operator",
        correlation_id=CID,
    )
    _grant(container)
    reader.answers.append(_shipped("6000-2222"))
    second = _dispatch(service)
    assert (second.attempt_no, second.state, second.verification) == (
        2,
        APPLIED_PROVEN,
        DISPATCH_CONFIRMED,
    )


def test_a_precluded_dispatch_was_not_applied_and_reads_nothing(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    _grant(container)
    reader = FakeReader()
    attempt = _dispatch(_service(container, FakeSender([PRECLUDED]), reader))
    assert (attempt.state, attempt.verification, reader.reads) == (NOT_APPLIED_PROVEN, None, 0)
    # Proven not applied: a new grant is possible.
    assert _grant(container)


def test_a_conflicting_read_back_is_shown_and_never_repaired(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    _grant(container)
    reader = FakeReader([_shipped("9999", "HANJIN")])
    attempt = _dispatch(_service(container, FakeSender([SUCCESS]), reader))
    assert attempt.verification == CONFLICT
    assert _fulfillment(container).states([PO]) == {PO: DISPATCH_CONFLICT}
    with pytest.raises(DispatchRefused):
        _grant(container)


def test_a_dispatch_grant_binds_exactly_one_order_and_no_other_stage(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    # Not dispatchable yet: no supplier order, no tracking.
    _ingest(container, _facts())
    with pytest.raises(DispatchRefused) as early:
        _grant(container)
    assert early.value.code == "OPERATE_DISPATCH_TRACKING_MISSING"
    _ready(container)
    grant = _grant(container)
    with sqlite3.connect(container.config.database_path) as raw:
        row = raw.execute(
            "SELECT stage, budget_max, intent_id, registration_snapshot_id, idempotency_key,"
            " preparation_revision_id FROM live_grants WHERE grant_id = ?",
            (grant,),
        ).fetchone()
        binding = raw.execute(
            "SELECT product_order_id, supplier_order_revision, carrier_code, tracking_number"
            " FROM live_grant_dispatch_bindings WHERE grant_id = ?",
            (grant,),
        ).fetchone()
    assert row == (MutationStage.DISPATCH.value, 1, None, None, None, None)
    assert binding == (PO, 2, "CJGLS", TRACKING)


def test_the_database_keeps_the_attempts_and_bindings_as_evidence(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ready(container)
    _release(container)
    grant = _grant(container)
    _dispatch(_service(container, FakeSender([LOST]), FakeReader([_waiting()])))
    with sqlite3.connect(container.config.database_path) as raw:
        for statement in (
            # A second attempt while an UNKNOWN one blocks the order.
            "INSERT INTO operate_dispatch_attempts (attempt_id, product_order_id, attempt_no,"
            " grant_id, marketplace_key, marketplace_account_id, supplier_order_revision,"
            " carrier_code, tracking_number, dispatch_date, state, actor, correlation_id,"
            " started_at) SELECT 'x', product_order_id, 2, grant_id, marketplace_key,"
            " marketplace_account_id, supplier_order_revision, carrier_code, tracking_number,"
            " dispatch_date, 'STARTED', actor, correlation_id, started_at"
            " FROM operate_dispatch_attempts",
            "UPDATE operate_dispatch_attempts SET tracking_number = 'other'",
            "UPDATE operate_dispatch_attempts SET state = 'NOT_APPLIED_PROVEN'",
            "DELETE FROM operate_dispatch_attempts",
            "UPDATE live_grant_dispatch_bindings SET tracking_number = 'other'",
            "DELETE FROM live_grant_dispatch_bindings",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)
        # A dispatch binding belongs to a DISPATCH grant only.
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO live_grant_dispatch_bindings VALUES ((SELECT grant_id FROM"
                " live_grants WHERE grant_id <> ? LIMIT 1), 'po-x', 1, 'CJGLS', '1')",
                (grant,),
            )


def test_production_wires_the_adopted_dispatch_sender_behind_the_stack(
    container: Container,
) -> None:
    sender = container.dispatches._sender
    assert isinstance(sender, SmartStoreDispatchSender)
    assert sender.endpoint_adopted() is True
    assert container.dispatches._authority is not None
