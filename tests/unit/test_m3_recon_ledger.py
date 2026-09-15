"""The M3 reconnaissance ledger (ADR-0010 §4, §5; rulings on Q1, Q2): no supplier contact."""

import contextlib
import sqlite3
from pathlib import Path

import pytest

from integrations.suppliers.collection import ReadKind
from integrations.suppliers.transport.collection import CollectionBudgetRefused
from scripts.m3harness.ledger import Ledger, LedgerError, Mode, State, hosts_digest

PRODUCT = "https://supplier.test/products/1234"
HOST = "img.supplier.test"


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger.create(
        tmp_path / "ledger.sqlite3", campaign_id="m3-recon-01", mode=Mode.DRY, product_url=PRODUCT
    )


def _running_a(tmp_path: Path) -> Ledger:
    ledger = _ledger(tmp_path)
    ledger.record("PREFLIGHT_PASSED", state=State.AWAITING_APPROVAL)
    ledger.record("RUN_STARTED", state=State.RUNNING_A)
    return ledger


def _refused(ledger: Ledger, kind: ReadKind, subject: str, **kwargs: float) -> str:
    with pytest.raises(CollectionBudgetRefused) as caught:
        ledger.reserve(kind, subject, **kwargs)
    return caught.value.message


def test_a_campaign_is_initialized_once_with_a_valid_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert (ledger.campaign().campaign_id, ledger.state()) == ("m3-recon-01", State.INITIALIZED)
    with pytest.raises(LedgerError):
        _ledger(tmp_path)
    with pytest.raises(LedgerError):
        Ledger.create(
            tmp_path / "x.sqlite3", campaign_id="m2-x", mode=Mode.DRY, product_url=PRODUCT
        )


def test_nothing_is_reserved_before_the_approved_phase(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert "PHASE_CLOSED" in _refused(ledger, ReadKind.POLICY_READ, "/robots.txt")
    ledger.record("PREFLIGHT_PASSED", state=State.AWAITING_APPROVAL)
    assert "PHASE_CLOSED" in _refused(ledger, ReadKind.PRODUCT_READ, PRODUCT)


def test_phase_a_caps_policy_and_product_reads(tmp_path: Path) -> None:
    ledger = _running_a(tmp_path)
    for n in range(3):
        ledger.reserve(ReadKind.POLICY_READ, f"/policy-{n}")
    assert "CAP_REACHED" in _refused(ledger, ReadKind.POLICY_READ, "/robots.txt")
    for n in range(4):
        ledger.reserve(ReadKind.PRODUCT_READ, PRODUCT, epoch=1000.0 + 60 * n)
    assert "CAP_REACHED" in _refused(ledger, ReadKind.PRODUCT_READ, PRODUCT, epoch=2000.0)
    assert ledger.counts() == {"PRODUCT_READ": 4, "IMAGE_REQUEST": 0, "POLICY_READ": 3}


def test_the_same_product_waits_60_seconds(tmp_path: Path) -> None:
    ledger = _running_a(tmp_path)
    ledger.reserve(ReadKind.PRODUCT_READ, PRODUCT, epoch=1000.0)
    message = _refused(ledger, ReadKind.PRODUCT_READ, PRODUCT, epoch=1059.0)
    assert "SAME_PRODUCT_INTERVAL" in message
    ledger.reserve(ReadKind.PRODUCT_READ, PRODUCT, epoch=1060.0)


def test_a_discovered_policy_read_happens_at_most_once(tmp_path: Path) -> None:
    ledger = _running_a(tmp_path)
    ledger.reserve(ReadKind.POLICY_READ, "/robots.txt")
    ledger.reserve(ReadKind.POLICY_READ, "discovered:/member/agreement.html")
    message = _refused(ledger, ReadKind.POLICY_READ, "discovered:/member/privacy.html")
    assert "DISCOVERED_POLICY_ONCE" in message


def test_images_need_phase_b_and_an_approved_observed_host(tmp_path: Path) -> None:
    ledger = _running_a(tmp_path)
    assert "PHASE_CLOSED" in _refused(ledger, ReadKind.IMAGE_REQUEST, HOST)
    with pytest.raises(LedgerError):
        ledger.approve_hosts({HOST})  # only after phase A
    ledger.finish_phase_a([HOST], findings_digest="0" * 64)
    assert ledger.observed_hosts() == frozenset({HOST})
    assert ledger.observation_problems() == []
    with pytest.raises(LedgerError, match="HOST_NOT_OBSERVED"):
        ledger.approve_hosts({"cdn.other.test"})
    ledger.approve_hosts({HOST})
    ledger.record("IMAGES_APPROVED", state=State.RUNNING_B)
    assert "PHASE_CLOSED" in _refused(ledger, ReadKind.PRODUCT_READ, PRODUCT)
    assert "HOST_NOT_APPROVED" in _refused(ledger, ReadKind.IMAGE_REQUEST, "cdn.other.test")
    for _ in range(30):
        ledger.reserve(ReadKind.IMAGE_REQUEST, HOST)
    assert "CAP_REACHED" in _refused(ledger, ReadKind.IMAGE_REQUEST, HOST)


def test_the_observation_is_bound_with_its_digest_and_closed_after_phase_a(
    tmp_path: Path,
) -> None:
    ledger = _running_a(tmp_path)
    ledger.finish_phase_a([HOST, HOST], findings_digest="f" * 64)
    event = ledger.last_event("PHASE_A_DONE")
    assert event is not None
    assert event["detail"] == {
        "observed_hosts": [HOST],
        "observed_hosts_digest": hosts_digest([HOST]),
        "findings_digest": "f" * 64,
    }
    assert ledger.state() is State.AWAITING_IMAGE_HOST_APPROVAL
    with contextlib.closing(sqlite3.connect(ledger.path)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="OBSERVATION_CLOSED"):
            raw.execute("INSERT INTO observed_hosts VALUES ('cdn.other.test', 't')")
        with pytest.raises(sqlite3.IntegrityError, match="HOST_NOT_OBSERVED"):
            raw.execute("INSERT INTO approved_hosts VALUES ('cdn.other.test', 't')")


def test_illegal_transitions_are_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(LedgerError):
        ledger.record("RUN_STARTED", state=State.RUNNING_A)  # no approval STOP was reached
    with pytest.raises(LedgerError):
        ledger.finish_phase_a([HOST], findings_digest="0" * 64)  # phase A is not running
    ledger.record("STOPPED", state=State.STOPPED)
    with pytest.raises(LedgerError):
        ledger.record("PREFLIGHT_PASSED", state=State.AWAITING_APPROVAL)  # terminal


def test_every_table_is_append_only_and_the_triggers_hold_without_the_code(
    tmp_path: Path,
) -> None:
    ledger = _running_a(tmp_path)
    seq = ledger.reserve(ReadKind.POLICY_READ, "/robots.txt")
    ledger.complete(seq, http_status=200, outcome="OK")
    ledger.finish_phase_a([HOST], findings_digest="0" * 64)
    ledger.approve_hosts({HOST})
    with contextlib.closing(sqlite3.connect(ledger.path)) as raw:
        tables = (
            "campaign",
            "events",
            "observed_hosts",
            "approved_hosts",
            "reservations",
            "completions",
        )
        for table in tables:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"UPDATE {table} SET rowid = rowid")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO reservations (at, epoch, kind, subject) VALUES ('t', 1, 'LOGIN', 'x')"
            )


def test_the_cap_trigger_holds_without_the_code(tmp_path: Path) -> None:
    ledger = _running_a(tmp_path)
    ledger.reserve(ReadKind.POLICY_READ, "/robots.txt")
    with contextlib.closing(sqlite3.connect(ledger.path)) as raw:
        for n in range(2):
            raw.execute(
                "INSERT INTO reservations (at, epoch, kind, subject) VALUES ('t', 1, ?, ?)",
                ("POLICY_READ", f"/p{n}"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CAP_REACHED"):
            raw.execute(
                "INSERT INTO reservations (at, epoch, kind, subject) VALUES ('t', 1, ?, ?)",
                ("POLICY_READ", "/p3"),
            )
