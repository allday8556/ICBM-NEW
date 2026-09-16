"""The reservation guard's version, its upgrade, and what v2 opens (Issue #52 ruling 5699776908).

The upgrade exists because a campaign that is already mid-flight carries the guard it was created
with. Phase B may not reserve a request the ledger's own guard cannot police, so the approval
refuses an older ledger and the operator upgrades it first.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from scripts.m3harness.ledger import (
    GUARD_VERSION,
    IMAGE_ROBOTS_PREFIX,
    Ledger,
    LedgerError,
    Mode,
    State,
    reservations_guard,
)

HOST = "img.example.invalid"
PRODUCT = "https://shop.invalid/product/x/1/"


def _campaign(path: Path) -> Ledger:
    return Ledger.create(
        path / "ledger.db", campaign_id="m3-recon-01", mode=Mode.REAL, product_url=PRODUCT
    )


def _at_image_approval(path: Path) -> Ledger:
    ledger = _campaign(path)
    ledger.record("PREFLIGHT_PASSED", state=State.AWAITING_APPROVAL)
    ledger.record("RUN_A_STARTED", state=State.RUNNING_A)
    ledger.finish_phase_a([HOST], findings_digest="0" * 64)
    return ledger


def _downgrade(ledger: Ledger) -> None:
    """The guard a ledger created before this version carries: v1, and no robots rule."""
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute("DROP TRIGGER trg_reservations_guard")
        db.execute(
            "CREATE TRIGGER trg_reservations_guard BEFORE INSERT ON reservations BEGIN "
            "SELECT RAISE(ABORT, 'PHASE_CLOSED') WHERE (SELECT state FROM current_state) IS NOT "
            "(CASE NEW.kind WHEN 'IMAGE_REQUEST' THEN 'RUNNING_B' ELSE 'RUNNING_A' END); END;"
        )
        db.execute("PRAGMA user_version = 1")
        db.commit()


def test_a_new_ledger_carries_the_current_guard(tmp_path: Path) -> None:
    ledger = _campaign(tmp_path)
    assert ledger.guard_version() == GUARD_VERSION
    assert ledger.upgrade_guard() is False, "there is nothing to upgrade"


def test_the_upgrade_is_versioned_and_idempotent(tmp_path: Path) -> None:
    ledger = _campaign(tmp_path)
    _downgrade(ledger)
    assert ledger.guard_version() == 1
    assert ledger.upgrade_guard() is True
    assert ledger.guard_version() == GUARD_VERSION
    event = ledger.last_event("LEDGER_GUARD_UPGRADED")
    assert event is not None and event["detail"] == {"from": 1, "to": GUARD_VERSION}
    seq = ledger.latest_seq()
    # Running it again changes nothing at all: no trigger churn and no second event.
    assert ledger.upgrade_guard() is False
    assert ledger.latest_seq() == seq


def test_the_upgrade_does_not_touch_the_campaign_state(tmp_path: Path) -> None:
    ledger = _at_image_approval(tmp_path)
    _downgrade(ledger)
    before = ledger.state()
    ledger.upgrade_guard()
    assert ledger.state() is before
    with closing(sqlite3.connect(ledger.path)) as db:
        (state,) = db.execute(
            "SELECT state FROM events WHERE kind = 'LEDGER_GUARD_UPGRADED'"
        ).fetchone()
    assert state is None, "an upgrade is not a transition"


def test_an_upgraded_ledger_and_a_new_one_run_the_same_guard(tmp_path: Path) -> None:
    # One source, installed by both paths, so the two can never drift apart.
    fresh = _campaign(tmp_path / "fresh")
    older = _campaign(tmp_path / "older")
    _downgrade(older)
    older.upgrade_guard()

    def installed(ledger: Ledger) -> str:
        with closing(sqlite3.connect(ledger.path)) as db:
            (sql,) = db.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'trg_reservations_guard'"
            ).fetchone()
        return str(sql)

    assert installed(fresh) == installed(older) == reservations_guard().rstrip(";")


def test_the_upgrade_adds_no_table_and_keeps_every_recorded_row(tmp_path: Path) -> None:
    ledger = _at_image_approval(tmp_path)
    _downgrade(ledger)

    def shape(path: Path) -> tuple[list[str], list[tuple[int, str]]]:
        with closing(sqlite3.connect(path)) as db:
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            rows = list(db.execute("SELECT seq, kind FROM events ORDER BY seq"))
        return tables, rows

    before = shape(ledger.path)
    ledger.upgrade_guard()
    tables, rows = shape(ledger.path)
    assert tables == before[0], "no table is added, dropped or recreated"
    assert rows[: len(before[1])] == before[1], "every recorded event is still there, unchanged"


def _reserve(ledger: Ledger, kind: str, subject: str) -> None:
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute(
            "INSERT INTO reservations (at, epoch, kind, subject) VALUES ('t', 0.0, ?, ?)",
            (kind, subject),
        )
        db.commit()


def test_phase_b_may_read_an_approved_image_hosts_own_robots_once(tmp_path: Path) -> None:
    ledger = _at_image_approval(tmp_path)
    ledger.approve_hosts([HOST])
    ledger.record("APPROVED_B", sha="a" * 40)
    ledger.record("RUN_B_STARTED", state=State.RUNNING_B)
    _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}{HOST}")
    with pytest.raises(sqlite3.IntegrityError, match="IMAGE_ROBOTS_ONCE"):
        _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}{HOST}")
    with pytest.raises(sqlite3.IntegrityError, match="HOST_NOT_APPROVED"):
        _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}other.invalid")


def test_phase_b_opens_nothing_else(tmp_path: Path) -> None:
    # The subject prefix is what opens the phase-B door, not the kind: a generic policy read and a
    # product read stay phase A's alone.
    ledger = _at_image_approval(tmp_path)
    ledger.approve_hosts([HOST])
    ledger.record("APPROVED_B", sha="a" * 40)
    ledger.record("RUN_B_STARTED", state=State.RUNNING_B)
    for kind, subject in (
        ("POLICY_READ", "/robots.txt"),
        ("POLICY_READ", "discovered:/member/terms.html"),
        ("PRODUCT_READ", PRODUCT),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="PHASE_CLOSED"):
            _reserve(ledger, kind, subject)


def test_an_image_robots_read_is_refused_outside_phase_b(tmp_path: Path) -> None:
    ledger = _at_image_approval(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="PHASE_CLOSED"):
        _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}{HOST}")


def test_a_reservation_that_never_completed_is_still_spent(tmp_path: Path) -> None:
    """The known cost of reserving before sending (ruling 5699776908).

    A crash between the reservation and the response leaves the reservation recorded. The ledger is
    append-only and the guard allows one robots read per host, so that host can never be asked
    again and the campaign cannot finish phase B. That is deliberate: a reservation that cannot be
    re-spent is what stops a crash loop from becoming repeated requests to someone else's host. The
    way forward is a new campaign, not an edited ledger.
    """
    ledger = _at_image_approval(tmp_path)
    ledger.approve_hosts([HOST])
    ledger.record("APPROVED_B", sha="a" * 40)
    ledger.record("RUN_B_STARTED", state=State.RUNNING_B)
    _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}{HOST}")  # the process dies here
    assert ledger.counts()["POLICY_READ"] == 1, "the reservation is spent, answered or not"
    with pytest.raises(sqlite3.IntegrityError, match="IMAGE_ROBOTS_ONCE"):
        _reserve(ledger, "POLICY_READ", f"{IMAGE_ROBOTS_PREFIX}{HOST}")
    with closing(sqlite3.connect(ledger.path)) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM reservations")  # and it cannot be taken back
    with pytest.raises(LedgerError):
        ledger.record("RUN_B_STARTED", state=State.RUNNING_A)  # nor can the phase be replayed
