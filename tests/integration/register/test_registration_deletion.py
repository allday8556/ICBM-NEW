"""ADR-0018 §3.5: the deletion of one ICBM-confirmed registration.

A deletion is a marketplace mutation: it runs only through the send-time safety stack, under its
own exact DELETE grant, inside a bounded LIVE window and with the protected-write brake released.
Its attempt is opened, with the grant spent, before any byte is sent; an unknown deletion is never
resent; only an origin-product read-back confirms or resolves one. No provider is reached here:
the sender and the read-back are fakes.
"""

import contextlib
import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest

from app.capabilities.live_safety import model as live_model
from app.capabilities.live_safety.model import GrantState, MutationRefused, MutationStage
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, InputValidationError
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.deletion import DeleteHandoff, RegistrationDeletionService
from app.stages.register.model import (
    DeletionState,
    DeletionVerification,
    RegistrationConflictError,
)
from app.stages.register.store import RegistrationStore
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore.deletion import SmartStoreDeleteSender
from tests.integration.register.test_m5_registration_foundation import (  # noqa: F401 - fixtures
    _confirm,
    _finish,
    _freeze,
    _intent,
    _priced,
    account,
    sources,
    store,
)
from tests.support.live_safety_support import PermittedMode, ProvenProofs
from tests.support.product_support import Collections, raw

pytestmark = pytest.mark.integration

APPROVAL = "5826077470"
CID = "cid-deletion"
PRODUCT = "mp-delete-1"


class FakeSender:
    def __init__(self, *handoffs: DeleteHandoff) -> None:
        self.handoffs = list(handoffs)
        self.sent: list[str] = []

    def endpoint_adopted(self) -> bool:
        return True

    def send(self, *, marketplace_product_id: str) -> DeleteHandoff:
        self.sent.append(marketplace_product_id)
        return self.handoffs.pop(0)


class FakeReadback:
    def __init__(
        self, status: str | None = None, *, available: bool = True, not_found: bool = False
    ) -> None:
        self.status = status
        self.is_available = available
        # The provider's HTTP 404 for the product, as the adopted caller reports it.
        self.not_found = not_found
        self.reads: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        self.reads.append(marketplace_product_id)
        if self.not_found:
            raise AppError("SMARTSTORE_HTTP_404", "gone", details={"http_status": 404})
        if self.status is None:
            raise AppError("SMARTSTORE_HTTP_404", "not readable")
        return {"originProduct": {"statusType": self.status}}


APPLIED = DeleteHandoff(RemoteOutcome.APPLIED_PROVEN, response_status=200)
UNKNOWN = DeleteHandoff(RemoteOutcome.UNKNOWN, response_status=None, error_code="READ_TIMEOUT")
PRECLUDED = DeleteHandoff(RemoteOutcome.NOT_APPLIED_PROVEN, error_code="TCP_CONNECT_FAILURE")


def _service(
    container: Container,
    sender: FakeSender,
    readback: FakeReadback,
    *,
    mode: Any = None,
    proofs: Any = None,
) -> RegistrationDeletionService:
    stack = SafetyStack(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        mode=mode or PermittedMode(),
        proofs=proofs or ProvenProofs(),
        clock=container.clock,
    )
    return RegistrationDeletionService(
        registrations=container.registrations,
        sender=sender,
        readback=readback,
        sale_status=lambda retained: smartstore_readback.normalize(retained).sale_status,
        authority=stack,
    )


@pytest.fixture
def registration(
    container: Container,
    config: AppConfig,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
) -> str:
    """One ACTIVE registration ICBM confirmed, with provider product ``PRODUCT``."""
    item = _priced(container, config, sources)
    intent = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent, RemoteOutcome.APPLIED_PROVEN, PRODUCT)
    return _confirm(store, intent)


def _grant(container: Container, registration_id: str) -> str:
    now = container.clock.now()
    return container.live_authority.issue_delete_grant(
        registration_id=registration_id,
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by="operator",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    ).grant_id


def _release(container: Container) -> None:
    container.live_authority.release_brake(
        actor="operator",
        reason_code="CANARY_WINDOW",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )


def _delete(service: RegistrationDeletionService, registration_id: str) -> Any:
    return service.delete(registration_id, actor="operator", correlation_id=CID)


def test_a_delete_grant_binds_the_exact_confirmed_registration(
    container: Container, registration: str
) -> None:
    grant_id = _grant(container, registration)
    grant = container.live_authority.grant_record(grant_id)
    stored = container.registrations.registration(registration)
    assert grant is not None and stored is not None
    assert grant.stage is MutationStage.DELETE and grant.budget_max == 1
    assert (grant.intent_id, grant.registration_snapshot_id) == (
        stored.intent_id,
        stored.registration_snapshot_id,
    )
    assert grant.idempotency_key is None and grant.create_attempt_no is None


def test_without_every_layer_nothing_is_sent_and_nothing_is_spent(
    container: Container, registration: str
) -> None:
    grant_id = _grant(container, registration)
    sender = FakeSender(APPLIED)
    # The production execution mode: no LIVE window is open, so the mode layer refuses.
    service = _service(container, sender, FakeReadback("DELETE"), mode=container.execution_mode)
    with pytest.raises(MutationRefused) as caught:
        _delete(service, registration)
    reasons = {layer["reason"] for layer in caught.value.details["layers"]}
    assert {live_model.MODE_NOT_LIVE, live_model.BRAKE_ENGAGED} <= reasons
    assert sender.sent == []
    assert container.registrations.deletions(registration) == ()
    stored = container.live_authority.grant_record(grant_id)
    assert stored is not None and stored.budget_used == 0 and stored.state is GrantState.ACTIVE
    events = container.audit.list_events(limit=50)
    assert any(e.action == "refuse_delete" for e in events)


def test_without_a_grant_or_retention_the_stack_refuses(
    container: Container, registration: str
) -> None:
    _release(container)
    sender = FakeSender(APPLIED)
    with pytest.raises(MutationRefused) as caught:
        _delete(_service(container, sender, FakeReadback("DELETE")), registration)
    assert caught.value.code == live_model.GRANT_MISSING
    _grant(container, registration)
    service = _service(
        container,
        sender,
        FakeReadback("DELETE"),
        proofs=ProvenProofs(missing=frozenset({"retention"})),
    )
    with pytest.raises(MutationRefused) as caught:
        _delete(service, registration)
    assert caught.value.code == live_model.RETENTION_UNPROVEN
    assert sender.sent == []


def test_the_canary_only_rows_are_not_layers_of_a_deletion(
    container: Container, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    proofs = ProvenProofs(missing=frozenset({"eligibility", "restore", "visual", "residual_risk"}))
    sender = FakeSender(APPLIED)
    record = _delete(
        _service(container, sender, FakeReadback("DELETE"), proofs=proofs), registration
    )
    assert record.state is DeletionState.APPLIED_PROVEN


def test_an_applied_deletion_is_confirmed_by_read_back_and_never_repeated(
    container: Container, registration: str
) -> None:
    grant_id = _grant(container, registration)
    _release(container)
    sender, readback = FakeSender(APPLIED), FakeReadback("DELETE")
    service = _service(container, sender, readback)
    record = _delete(service, registration)
    assert sender.sent == [PRODUCT] and readback.reads == [PRODUCT]
    assert record.state is DeletionState.APPLIED_PROVEN
    assert record.verification is DeletionVerification.DELETE_CONFIRMED
    assert record.deleted and record.open
    grant = container.live_authority.grant_record(grant_id)
    assert grant is not None and grant.state is GrantState.EXHAUSTED
    # Never twice: neither a new attempt nor a new grant.
    with pytest.raises(RegistrationConflictError, match="never resent"):
        _delete(service, registration)
    with pytest.raises(InputValidationError) as refused:
        _grant(container, registration)
    assert refused.value.code == "LIVE_GRANT_DELETION_OPEN"
    assert sender.sent == [PRODUCT]


def test_an_unknown_deletion_is_never_resent_until_a_read_back_shows_the_listing(
    container: Container, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    sender = FakeSender(UNKNOWN, APPLIED)
    # No session to read with: the unknown attempt stays unresolved.
    unavailable = FakeReadback("SALE", available=False)
    record = _delete(_service(container, sender, unavailable), registration)
    assert record.state is DeletionState.UNKNOWN and record.verification is None
    assert record.open and not record.deleted
    with pytest.raises(InputValidationError):
        _grant(container, registration)
    with pytest.raises(RegistrationConflictError):
        _delete(_service(container, sender, unavailable), registration)
    # A read-back that shows the listing still on sale resolves it: the way to a new grant opens.
    still = _service(container, sender, FakeReadback("SALE"))
    resolved = still.verify(registration, correlation_id=CID)
    assert resolved.verification is DeletionVerification.STILL_PRESENT and not resolved.open
    _grant(container, registration)
    again = _delete(_service(container, sender, FakeReadback("DELETE")), registration)
    assert again.attempt_no == 2 and again.deleted
    assert sender.sent == [PRODUCT, PRODUCT]


def test_an_unknown_deletion_read_back_as_deleted_is_deleted(
    container: Container, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    record = _delete(_service(container, FakeSender(UNKNOWN), FakeReadback("DELETE")), registration)
    assert record.state is DeletionState.UNKNOWN
    assert record.verification is DeletionVerification.DELETE_CONFIRMED
    assert record.deleted and record.open


def test_a_failed_read_back_records_nothing(container: Container, registration: str) -> None:
    _grant(container, registration)
    _release(container)
    record = _delete(_service(container, FakeSender(APPLIED), FakeReadback(None)), registration)
    assert record.state is DeletionState.APPLIED_PROVEN and record.verification is None


def test_a_404_after_a_proven_deletion_confirms_it(container: Container, registration: str) -> None:
    # Owner decision 6031580064 (ADR-0018 §3.5 amendment): once removed, the product reads 404.
    _grant(container, registration)
    _release(container)
    gone = FakeReadback(not_found=True)
    record = _delete(_service(container, FakeSender(APPLIED), gone), registration)
    assert record.state is DeletionState.APPLIED_PROVEN
    assert record.verification is DeletionVerification.DELETE_CONFIRMED and record.deleted


def test_a_404_after_an_unknown_deletion_still_proves_nothing(
    container: Container, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    gone = FakeReadback(not_found=True)
    record = _delete(_service(container, FakeSender(UNKNOWN), gone), registration)
    assert record.state is DeletionState.UNKNOWN and record.verification is None
    assert record.open and not record.deleted


def test_a_precluded_deletion_is_not_open_and_may_be_granted_again(
    container: Container, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    readback = FakeReadback("DELETE")
    record = _delete(_service(container, FakeSender(PRECLUDED), readback), registration)
    assert record.state is DeletionState.NOT_APPLIED_PROVEN and not record.open
    assert readback.reads == []
    _grant(container, registration)


def test_the_database_keeps_one_open_deletion_and_never_forgets_one(
    container: Container, config: AppConfig, registration: str
) -> None:
    _grant(container, registration)
    _release(container)
    record = _delete(
        _service(container, FakeSender(UNKNOWN), FakeReadback(available=False)), registration
    )
    with contextlib.closing(raw(config)) as connection:
        for sql, match in (
            ("DELETE FROM registration_deletions", "append-only"),
            (
                "UPDATE registration_deletions SET marketplace_product_id = 'other'",
                "identity",
            ),
            ("UPDATE registration_deletions SET state = 'NOT_APPLIED_PROVEN'", "exactly once"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=match):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO registration_deletions (deletion_id, registration_id, intent_id,"
                " marketplace_key, marketplace_account_id, marketplace_product_id, grant_id,"
                " attempt_no, state, actor, correlation_id, started_at)"
                " SELECT 'second', registration_id, intent_id, marketplace_key,"
                " marketplace_account_id, marketplace_product_id, grant_id, 2, 'STARTED', actor,"
                " correlation_id, started_at FROM registration_deletions"
            )
    assert container.registrations.deletions(registration) == (record,)


def test_a_delete_grant_carries_no_create_binding(
    container: Container, config: AppConfig, registration: str
) -> None:
    grant_id = _grant(container, registration)
    with contextlib.closing(raw(config)) as connection:
        row = connection.execute(
            "SELECT * FROM live_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        columns = [d[0] for d in connection.execute("SELECT * FROM live_grants").description]
        values = dict(zip(columns, row, strict=True))
        for change in ({"idempotency_key": "'key'"}, {"budget_max": "2"}):
            ((column, value),) = change.items()
            values_sql = ", ".join(
                value if c == column else ("'new'" if c == "grant_id" else "?") for c in columns
            )
            params = [values[c] for c in columns if c not in ("grant_id", column)]
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(f"INSERT INTO live_grants VALUES ({values_sql})", params)


def test_production_wires_the_adopted_delete_sender_with_the_committed_bearer(
    container: Container,
) -> None:
    service = container.registration_deletions
    assert isinstance(service._sender, SmartStoreDeleteSender)
    assert service._sender._bearer == container.smartstore.committed_bearer
    assert service._readback._bearer == container.smartstore.committed_bearer
    # Without a session the sender refuses locally, before any transport: precluded.
    handoff = service._sender.send(marketplace_product_id="123")
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "SMARTSTORE_SESSION_UNAVAILABLE"
