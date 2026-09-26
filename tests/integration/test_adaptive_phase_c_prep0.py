"""Phase C C1 PREP-0: every actual send of a Phase-C-accounted collection, accounted durably first
(Issue #110 `5841947773`).

Everything here runs against synthetic data roots, the fake shop and the fake CONNECT transport:
the socket layer is refused and no supplier is read. What this proves:

- an ordinary collection becomes Phase-C-accounted only when its frozen capture request binds it
  to a campaign that registered its read ceilings; every other run is unaccounted and unchanged;
- each actual send — ``PRODUCT_READ`` and ``IMAGE_REQUEST`` at the collection transport's budget,
  ``CONNECT_CONTROL_READ`` and ``CONNECT_PROTECTED_READ`` at the CONNECT fetch — consumes exactly
  one reservation of its own class, committed before the send;
- ``CONNECT_AUTHENTICATE`` is a hard zero: a login is refused before it is attempted or counted;
- the ceilings are enforced before a send, across retries, restarts and runs of the campaign;
  ``IMAGE_REQUEST`` per attempt;
- a missing binding, a target mismatch and an unreadable owner refuse before any send;
- the database refuses an unbound or over-ceiling reservation, and every row is append-only;
- migration 0028 is additive and its downgrade never destroys accounting.
"""

import contextlib
import socket
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text

from app.collect.adaptive_capture.accounting import PhaseCSendRefused
from app.collect.adaptive_store.gate import build_supplier_gate
from app.collect.collection import pacing_key
from app.collect.models import CollectionOutcome
from app.collect.shadow import FrozenCapture
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import TransientError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.migrate import alembic_config, upgrade_to_head
from integrations.suppliers.base import ProbeResponse, RequestKind
from integrations.suppliers.transport.collection import CollectionBudgetRefused
from scripts.m3collect import fake_shop
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    PRIMARY_BYTES,
    PRIMARY_URL,
    PRODUCT_URL,
    SUPPLIER_KEY,
    FakeGateway,
    page,
)
from scripts.phasec.ceilings import read_budget
from tests.shadow_support import (
    INTERVAL,
    OPERATOR,
    collect_once,
    raw,
    registered,
    rows,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

CAMPAIGN = "phase-c-synthetic-prep0"
ACCOUNTING = ("adaptive_phase_c_read_budgets", "adaptive_phase_c_reads")


class NetworkRefused(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("PREP-0 makes no network call")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


Witness = Callable[[str], None]


class _Witnessed:
    """The transport's budget, observed the instant after it reserved and before the send."""

    def __init__(self, inner: Any, witness: Witness | None) -> None:
        self._inner = inner
        self._witness = witness

    def reserve(self, kind: Any, subject: str) -> None:
        self._inner.reserve(kind, subject)
        if self._witness is not None:
            self._witness(kind.value)


@dataclass
class Shop(FakeGateway):
    witness: Witness | None = None
    fail_first_document: bool = False

    def read_document(
        self, profile: Any, url: str, *, kind: Any, budget: Any, session: Any = None
    ) -> Any:
        watched = _Witnessed(budget, self.witness)
        if self.fail_first_document:
            self.fail_first_document = False
            watched.reserve(kind, url)
            raise TransientError("SUPPLIER_TRANSIENT", "the shop dropped the connection")
        return super().read_document(profile, url, kind=kind, budget=watched, session=session)

    def read_image(self, profile: Any, url: str, *, budget: Any, **kwargs: Any) -> Any:
        return super().read_image(profile, url, budget=_Witnessed(budget, self.witness), **kwargs)


class Connect(fake_shop.FakeConnect):
    """The shop's CONNECT transport; ``expired`` makes a stored session read as signed out."""

    def __init__(self) -> None:
        super().__init__()
        self.expired = False
        self.witness: Witness | None = None

    def fetch(self, definition: Any, *, kind: RequestKind, session: bytes | None) -> ProbeResponse:
        if self.witness is not None:
            self.witness(f"CONNECT_{kind.value}")
        if self.expired and kind is RequestKind.PROTECTED_READ:
            self.fetches.append(kind.value)
            return ProbeResponse(200, definition.probe.target, None, '<div class="state-logoff">')
        return super().fetch(definition, kind=kind, session=session)

    def login(self, definition: Any, credentials: Any) -> bytes:
        if self.witness is not None:
            self.witness("CONNECT_AUTHENTICATE")
        return super().login(definition, credentials)


def shop() -> Shop:
    return Shop(documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES})


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@contextlib.contextmanager
def application(
    config: AppConfig, clock: FakeClock, gateway: Shop, connect: Connect, secrets: MemorySecretStore
) -> Iterator[Container]:
    """One application process with the real CONNECT owner over the fake CONNECT transport."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=secrets,
            supplier_gateway=connect,
            suppliers=(fake_shop.connect_definition(),),
            collection_gateway=gateway,
            collections=(registered(),),
            adaptive_supplier_gate=build_supplier_gate({SUPPLIER_KEY}),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def connected(app: Container) -> None:
    """The ordinary CONNECT an operator runs before any campaign: never accounted."""
    app.connect.save_credentials(
        SUPPLIER_KEY,
        username=fake_shop.MEMBER_ID,
        password=fake_shop.MEMBER_PASSWORD,
        actor="operator",
    )
    app.connect.verify(SUPPLIER_KEY, trigger="operator_test", allow_login=True)
    app.connect.set_auto_connect(SUPPLIER_KEY, enabled=True, actor="operator")


def request(app: Container, budget: dict[str, tuple[str, int]] | None = None, **kw: Any) -> str:
    return app.capture_store.request(
        campaign_id=CAMPAIGN,
        supplier_key=SUPPLIER_KEY,
        target=pacing_key(registered().collection, PRODUCT_URL).url,
        lifetime=timedelta(hours=1),
        requested_by=OPERATOR,
        correlation_id=f"corr-{uuid.uuid4()}",
        read_budget=read_budget("C1") if budget is None else budget,
        **kw,
    )


def budget_with(**changes: tuple[str, int]) -> dict[str, tuple[str, int]]:
    return {**read_budget("C1"), **changes}


def durable_witness(config: AppConfig) -> tuple[list[tuple[str, int]], Witness]:
    """At each send, how many reservations of its class are already committed in the data root."""
    seen: list[tuple[str, int]] = []

    def witness(request_class: str) -> None:
        (found,) = rows(
            config,
            "SELECT COUNT(*) FROM adaptive_phase_c_reads WHERE request_class = ?",
            request_class,
        )[0]
        seen.append((request_class, found))

    return seen, witness


def outcome(app: Container, run_id: str) -> tuple[CollectionOutcome, str | None]:
    run = app.collection.run(run_id)
    return run.outcome, run.detail


def refusals(app: Container) -> list[tuple[str, str]]:
    return [
        (str(r["request_class"]), str(r["reason"])) for r in app.phase_c_reads.refusals(CAMPAIGN)
    ]


def logins_counted(config: AppConfig) -> int:
    ((attempts,),) = rows(config, "SELECT real_login_attempts FROM supplier_connections")
    return int(attempts)


# ================================================================ accounting


def test_each_actual_send_of_an_accounted_run_is_reserved_durably_before_it_is_sent(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        assert all(rows(config, f"SELECT COUNT(*) FROM {t}") == [(0,)] for t in ACCOUNTING)
        seen, witness = durable_witness(config)
        gateway.witness = connect.witness = witness
        fetched = len(connect.fetches)
        request(app)
        run_id = collect_once(app, clock, PRODUCT_URL)
        counts = app.phase_c_reads.counts(CAMPAIGN, "C1")
        assert outcome(app, run_id)[0] is CollectionOutcome.RECORDED
    assert connect.fetches[fetched:] == ["CONTROL_READ", "PROTECTED_READ"]
    assert gateway.document_reads == 1 and len(gateway.image_reads) == 2
    assert counts == {
        "PRODUCT_READ": 1,
        "IMAGE_REQUEST": 2,
        "POLICY_READ": 0,
        "CONNECT_CONTROL_READ": 1,
        "CONNECT_PROTECTED_READ": 1,
        "CONNECT_AUTHENTICATE": 0,
    }
    # One reservation per actual send, each already committed when its send happened.
    assert sorted(cls for cls, _ in seen) == sorted(Counter(counts).elements())
    ordinal: Counter[str] = Counter()
    for request_class, committed in seen:
        ordinal[request_class] += 1
        assert committed == ordinal[request_class], (request_class, committed)
    (runs,) = rows(config, "SELECT COUNT(DISTINCT collection_run_id) FROM adaptive_phase_c_reads")
    assert runs == (1,)


def test_an_ordinary_collection_is_unaccounted_and_unchanged(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        fetched = len(connect.fetches)
        ordinary = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, ordinary)[0] is CollectionOutcome.RECORDED
        assert all(rows(config, f"SELECT COUNT(*) FROM {t}") == [(0,)] for t in ACCOUNTING)
        # An accounted run between ordinary ones is the only one counted.
        request(app)
        collect_once(app, clock, PRODUCT_URL)
        before = app.phase_c_reads.counts(CAMPAIGN, "C1")
        after_ordinary = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, after_ordinary)[0] is CollectionOutcome.RECORDED
        assert app.phase_c_reads.counts(CAMPAIGN, "C1") == before
    assert connect.fetches[fetched:] == ["CONTROL_READ", "PROTECTED_READ"] * 3
    assert gateway.document_reads == 3


def test_a_login_is_a_hard_zero_refused_before_it_is_attempted(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        logins, attempts = connect.logins, logins_counted(config)
        request(app)
        connect.expired = True  # the stored session now reads as signed out: CONNECT would log in
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, run_id) == (CollectionOutcome.FAILED, "PHASE_C_SEND_REFUSED")
        assert ("CONNECT_AUTHENTICATE", "CEILING") in refusals(app)
        assert app.phase_c_reads.counts(CAMPAIGN, "C1")["CONNECT_AUTHENTICATE"] == 0
    assert connect.logins == logins, "no login was transmitted"
    assert logins_counted(config) == attempts, "no login attempt was even counted"
    assert gateway.document_reads == 0


def test_a_ceiling_is_enforced_before_the_send_across_runs_of_the_campaign(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    budget = budget_with(PRODUCT_READ=("CAMPAIGN", 1))
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        request(app, budget)
        first = collect_once(app, clock, PRODUCT_URL)
        request(app, budget)
        second = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, first)[0] is CollectionOutcome.RECORDED
        assert outcome(app, second) == (CollectionOutcome.FAILED, "PHASE_C_SEND_REFUSED")
        assert refusals(app) == [("PRODUCT_READ", "CEILING")]
        assert app.phase_c_reads.counts(CAMPAIGN, "C1")["PRODUCT_READ"] == 1
        with pytest.raises(Exception, match="frozen once"):
            request(app, budget_with(PRODUCT_READ=("CAMPAIGN", 9)))  # never widened
    assert gateway.document_reads == 1, "the refused read was never sent"


def test_image_requests_are_bounded_per_attempt(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        request(app, budget_with(IMAGE_REQUEST=("ATTEMPT", 1)))
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, run_id)[0] is CollectionOutcome.RECORDED
        assert ("IMAGE_REQUEST", "CEILING") in refusals(app)
        assert app.phase_c_reads.counts(CAMPAIGN, "C1")["IMAGE_REQUEST"] == 1
    assert len(gateway.image_reads) == 1, "the refused image was never requested"


def test_a_retry_consumes_the_same_frozen_ceilings(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        request(app, budget_with(PRODUCT_READ=("CAMPAIGN", 1)))
        run_id = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL).collection_run_id
        gateway.fail_first_document = True
        with pytest.raises(TransientError):
            app.collection.collect(SUPPLIER_KEY, PRODUCT_URL, run_id=run_id, attempt_no=1)
        clock.advance(INTERVAL + 1)
        with pytest.raises(CollectionBudgetRefused):
            app.collection.collect(SUPPLIER_KEY, PRODUCT_URL, run_id=run_id, attempt_no=2)
        assert app.phase_c_reads.counts(CAMPAIGN, "C1")["PRODUCT_READ"] == 1
        assert [r["attempt_no"] for r in app.phase_c_reads.refusals(CAMPAIGN)] == [2]
    assert gateway.document_reads == 0, "the retry was refused before it was sent"


def test_accounting_reads_back_after_a_restart_and_keeps_counting(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    budget = budget_with(PRODUCT_READ=("CAMPAIGN", 2))
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        request(app, budget)
        collect_once(app, clock, PRODUCT_URL)
        before = app.phase_c_reads.counts(CAMPAIGN, "C1")
    with application(config, clock, gateway, connect, secrets) as app:  # a fresh process
        assert app.phase_c_reads.counts(CAMPAIGN, "C1") == before
        request(app, budget)
        assert outcome(app, collect_once(app, clock, PRODUCT_URL))[0] is CollectionOutcome.RECORDED
        request(app, budget)
        refused = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, refused) == (CollectionOutcome.FAILED, "PHASE_C_SEND_REFUSED")
        assert app.phase_c_reads.counts(CAMPAIGN, "C1")["PRODUCT_READ"] == 2
    assert gateway.document_reads == 2


# ================================================================ fail-closed binding


def test_a_run_without_registered_ceilings_is_refused_before_any_send(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        fetched = len(connect.fetches)
        app.capture_store.request(
            campaign_id=CAMPAIGN,
            supplier_key=SUPPLIER_KEY,
            target=pacing_key(registered().collection, PRODUCT_URL).url,
            lifetime=timedelta(hours=1),
            requested_by=OPERATOR,
            correlation_id="corr-unbudgeted",
        )
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, run_id) == (CollectionOutcome.FAILED, "PHASE_C_SEND_REFUSED")
    assert connect.fetches[fetched:] == [] and gateway.document_reads == 0


def test_a_target_mismatch_is_refused(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        request(app)
        run_id = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL).collection_run_id
        capture = app.capture_store.requests(CAMPAIGN)[0]
        with pytest.raises(PhaseCSendRefused) as refused:
            app.phase_c_reads.bind(
                collection_run_id=run_id,
                supplier_key=SUPPLIER_KEY,
                target="https://shop.example/other",
                capture=FrozenCapture("REQUESTED", capture.request_id),
                attempt_no=1,
            )
    assert refused.value.details["reason"] == "TARGET_MISMATCH"


class _BrokenDb:
    """The accounting owner's database: reads work or fail, writes always fail."""

    def __init__(self, real: Any, *, reads: bool) -> None:
        self._real, self._reads = real, reads

    def read(self) -> Any:
        if not self._reads:
            raise RuntimeError("the data root cannot be read")
        return self._real.read()

    def write(self, **_: Any) -> Any:
        raise RuntimeError("the data root cannot be written")


@pytest.mark.parametrize("reads", [False, True], ids=["unreadable", "unwritable"])
def test_an_unusable_owner_refuses_before_any_send(
    config: AppConfig,
    clock: FakeClock,
    secrets: MemorySecretStore,
    monkeypatch: pytest.MonkeyPatch,
    reads: bool,
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        fetched = len(connect.fetches)
        request(app)
        monkeypatch.setattr(app.phase_c_reads, "_db", _BrokenDb(app.db, reads=reads))
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert outcome(app, run_id) == (CollectionOutcome.FAILED, "PHASE_C_SEND_REFUSED")
    assert connect.fetches[fetched:] == [] and gateway.document_reads == 0


# ================================================================ the database


def test_the_database_admits_only_bound_reservations_under_the_ceiling(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    gateway, connect = shop(), Connect()
    with application(config, clock, gateway, connect, secrets) as app:
        connected(app)
        ordinary = collect_once(app, clock, PRODUCT_URL)
        request(app, budget_with(PRODUCT_READ=("CAMPAIGN", 1)))
        accounted = collect_once(app, clock, PRODUCT_URL)
    insert = (
        "INSERT INTO adaptive_phase_c_reads (read_id, campaign_id, stage, request_class,"
        " collection_run_id, attempt_no, subject_digest, reserved_at)"
        " VALUES (?, ?, 'C1', 'PRODUCT_READ', ?, 1, ?, '2026-09-26 00:00:00')"
    )
    with raw(config) as db:
        with pytest.raises(sqlite3.IntegrityError, match="belongs to a run frozen REQUESTED"):
            db.execute(insert, (str(uuid.uuid4()), CAMPAIGN, ordinary, "a" * 64))
        with pytest.raises(sqlite3.IntegrityError, match="PHASE_C_CEILING"):
            db.execute(insert, (str(uuid.uuid4()), CAMPAIGN, accounted, "a" * 64))
        with pytest.raises(sqlite3.IntegrityError, match="frozen ceiling"):
            db.execute(insert, (str(uuid.uuid4()), "phase-c-other", accounted, "a" * 64))
        for table in ACCOUNTING:
            with pytest.raises(sqlite3.IntegrityError, match="never"):
                db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="never"):
            db.execute("UPDATE adaptive_phase_c_read_budgets SET ceiling = 99")


def test_0028_is_additive_and_its_downgrade_never_destroys_accounting(
    tmp_path: Path, config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    command.upgrade(alembic_config(url), "0027_adaptive_capture_seam")

    def schema() -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            found = connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
            return dict(found.fetchall())

    before = schema()
    upgrade_to_head(url)
    after = schema()
    assert {name for name in before if before[name] != after[name]} == set()
    assert set(ACCOUNTING) | {"adaptive_phase_c_read_refusals"} <= set(after) - set(before)
    command.downgrade(alembic_config(url), "0027_adaptive_capture_seam")
    assert schema() == before
    with application(config, clock, shop(), Connect(), secrets) as app:
        request(app)
        live = app.db.url
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(live), "0027_adaptive_capture_seam")
    assert rows(config, "SELECT COUNT(*) FROM adaptive_phase_c_read_budgets") == [(6,)]


def test_a_reservation_commits_with_full_synchronous_and_restores_normal(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore
) -> None:
    with application(config, clock, shop(), Connect(), secrets) as app:
        with app.db.write(durable=True) as session:
            assert session.execute(text("PRAGMA synchronous")).scalar() == 2  # FULL
        with app.db.write() as session:
            assert session.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL
        with app.db.read() as session:
            assert session.execute(text("PRAGMA synchronous")).scalar() == 1
