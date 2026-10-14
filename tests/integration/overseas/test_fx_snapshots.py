"""ADR-0038 §3 (FX-02, FX-03): the FX snapshot owner on a migrated database.

The real container and application with an invented publication source: either a scripted fake
or the real AP01 reader over ``httpx.MockTransport``. No request reaches Korea Eximbank or any
other host, and every rate is invented.

Proven here:
- a refresh records one immutable row per currency for the day it read, audited without the key;
  the rows refuse an update, a delete, a second row for a day and a malformed rate;
- 「지금 갱신」 asks for today on a business day, else (or when today is not out yet) the most
  recent business day not yet recorded inside the FX-03 window; a recorded day is never asked
  again; a failure records nothing and stops;
- the newest snapshot as of a day, and the FX-03 reasons through the status read;
- the scheduler reads only on a business day at or after 11:00 KST, with the key, while today is
  unrecorded, once per interval; ``interval_s <= 0`` disables it;
- the key is write-only through the API: never echoed, audited, logged or stored in the database,
  and every write needs the client header;
- the real reader is granted the Eximbank host alone;
- migration 0064 is additive and its downgrade fails closed.
"""

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.capabilities.overseas.fx import (
    FX_API_KEY_MISSING,
    KST,
    PRICING_FX_STALE,
    PRICING_FX_UNAVAILABLE,
    SCHEDULER_ACTOR,
    FxRefreshScheduler,
    RefreshOutcome,
    SetFxKeyRequest,
)
from app.config import AppConfig
from app.container import Container, build_container
from app.main import create_app
from app.platform.core import egress
from app.platform.core.errors import PolicyBlockedError
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.platform.db.migrate import alembic_config, upgrade_to_head
from integrations.fx.koreaexim import (
    FX_SOURCE_AUTHKEY_INVALID,
    Ap01Failure,
    Ap01NotPublished,
    Ap01Publication,
    Ap01Result,
    KoreaEximAp01,
    parse_ap01,
)
from tests.conftest import LOCAL, make_config
from tests.support.jobs_support import TEST_JOBS, FakeClock
from tests.support.product_support import count, raw

pytestmark = pytest.mark.integration

KEY = "TestKeyAbcdefghijklmnop0123456789"
HEADERS = {"X-ICBM-Client": "pytest"}
OPERATOR = "operator:local"
CID = "cid-fx"
EVENT = "FX_RATES_FETCHED"
FRI_BEFORE, MON, TUE, WED, THU, FRI, SAT = (date(2026, 10, d) for d in (9, *range(12, 18)))


def _kst(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST).astimezone(UTC)


def _body(cny: str = "195.12", jpy: str = "938.4", usd: str = "1,399.9") -> bytes:
    rows = [
        {"result": 1, "cur_unit": "CNH", "tts": cny, "deal_bas_r": "193.18", "cur_nm": "위안화"},
        {"result": 1, "cur_unit": "JPY(100)", "tts": jpy, "deal_bas_r": "929.11"},
        {"result": 1, "cur_unit": "USD", "tts": usd, "deal_bas_r": "1,386.2"},
    ]
    return json.dumps(rows, ensure_ascii=False).encode("utf-8")


def publication(day: date, **rates: str) -> Ap01Publication:
    read = parse_ap01(day, _body(**rates))
    assert isinstance(read, Ap01Publication)
    return read


class FakeSource:
    """Answers per day; any other day is not published, or published when ``always`` is set."""

    def __init__(self) -> None:
        self.answers: dict[date, Ap01Result] = {}
        self.always = False
        self.calls: list[tuple[str, date]] = []

    def fetch(self, authkey: str, day: date) -> Ap01Result:
        self.calls.append((authkey, day))
        if day in self.answers:
            return self.answers[day]
        return publication(day) if self.always else Ap01NotPublished(day)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(_kst(WED, 12))


@pytest.fixture
def config(data_dir: Path) -> AppConfig:
    return make_config(data_dir, fx_refresh_interval_s=3600.0)


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@contextlib.contextmanager
def _container(config: AppConfig, clock: FakeClock, fx_source: Any) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            fx_source=fx_source,
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def fx(config: AppConfig, clock: FakeClock, source: FakeSource) -> Iterator[Container]:
    with _container(config, clock, source) as built:
        yield built


def _keyed(container: Container) -> None:
    container.fx.set_key(SetFxKeyRequest(key=KEY), actor=OPERATOR, correlation_id=CID)  # type: ignore[arg-type]


def _refresh(container: Container) -> Any:
    return container.fx.refresh(actor=OPERATOR, correlation_id=CID)


def _audit(config: AppConfig, event: str = EVENT) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT actor, reason_code, details_json, after_json FROM audit_events"
            " WHERE event_type = ? ORDER BY seq",
            (event,),
        ).fetchall()


def _all_audit_text(config: AppConfig) -> str:
    with contextlib.closing(raw(config)) as connection:
        return json.dumps(connection.execute("SELECT * FROM audit_events").fetchall())


# ---------------------------------------------------------------- recording


def test_a_refresh_records_one_immutable_row_per_currency(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = publication(WED)
    result = _refresh(fx)
    assert (result.outcome, result.code, result.publication_date) == (
        RefreshOutcome.RECORDED,
        None,
        WED,
    )
    assert source.calls == [(KEY, WED)]
    assert count(config, "fx_snapshots") == 3
    rates = {rate.currency: rate for rate in result.status.rates}
    assert [(r.source_code, r.unit, r.tts, r.deal_bas_r, r.reason) for r in rates.values()] == [
        ("CNH", 1, "195.12", "193.18", None),
        ("JPY(100)", 100, "938.4", "929.11", None),
        ("USD", 1, "1399.9", "1,386.2", None),
    ]
    assert result.status.key_configured and result.status.today == WED
    jpy = fx.fx.rate("JPY")
    assert (jpy.publication_date, jpy.unit, jpy.tts, jpy.rate_type) == (WED, 100, "938.4", "TTS")
    with contextlib.closing(raw(config)) as connection:
        (row,) = connection.execute(
            "SELECT publication_date, source, published_json FROM fx_snapshots"
            " WHERE currency = 'USD'"
        ).fetchall()
    assert row[:2] == ("2026-10-14", "koreaexim/AP01")
    assert json.loads(row[2])["tts"] == "1,399.9"
    ((actor, reason, details, after),) = _audit(config)
    assert (actor, reason) == (OPERATOR, None)
    assert json.loads(details) == {
        "trigger": "MANUAL",
        "source": "koreaexim/AP01",
        "searchdate": "2026-10-14",
        "result": "RECORDED",
    }
    assert [s["currency"] for s in json.loads(after)["snapshots"]] == ["CNY", "JPY", "USD"]
    assert KEY not in _all_audit_text(config)


def test_the_newest_snapshot_is_read_as_of_a_day(
    fx: Container, source: FakeSource, clock: FakeClock
) -> None:
    _keyed(fx)
    source.answers[MON] = publication(MON, jpy="930.0")
    source.answers[WED] = publication(WED, jpy="938.4")
    clock.advance(-2 * 86400)  # Monday
    assert _refresh(fx).publication_date == MON
    clock.advance(2 * 86400)  # Wednesday
    assert _refresh(fx).publication_date == WED
    store = fx.fx._store
    assert store.newest("JPY", date(2026, 10, 11)) is None
    newest_mon = store.newest("JPY", MON)
    newest_tue = store.newest("JPY", TUE)
    newest_thu = store.newest("JPY", THU)
    assert newest_mon is not None and newest_mon.tts == "930.0"
    assert newest_tue == newest_mon
    assert newest_thu is not None and newest_thu.publication_date == WED
    assert str(newest_thu.tts_per_unit()) == "9.384"
    with pytest.raises(ValueError):
        store.newest("EUR", WED)


def test_the_snapshots_are_append_only_unique_and_exact(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = publication(WED)
    _refresh(fx)
    insert = (
        "INSERT INTO fx_snapshots (snapshot_id, publication_date, currency, rate_type,"
        " source_code, unit, tts, deal_bas_r, published_json, source, fetched_at,"
        " correlation_id) VALUES (?, ?, ?, 'TTS', ?, ?, ?, '1', '{}', 'koreaexim/AP01',"
        " '2026-10-14 03:00:00', 'cid')"
    )
    with contextlib.closing(raw(config)) as connection:
        for statement in ("UPDATE fx_snapshots SET tts = '1'", "DELETE FROM fx_snapshots"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        for values in (
            ("x-1", "2026-10-14", "USD", "USD", 1, "1400"),  # a second row for the day
            ("x-2", "2026-10-15", "USD", "USD", 1, "1,400.5"),  # separators
            ("x-3", "2026-10-15", "USD", "USD", 1, "1400.5.1"),
            ("x-4", "2026-10-15", "USD", "USD", 1, "0"),
            ("x-5", "2026-10-15", "JPY", "JPY(100)", 1, "938.4"),  # the wrong unit
            ("x-6", "2026-10-15", "CNY", "CNY", 1, "195.1"),  # the wrong published code
            ("x-7", "2026-10-15", "EUR", "EUR", 1, "1500"),
            ("x-8", "15/10/2026", "USD", "USD", 1, "1400"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, values)
    assert count(config, "fx_snapshots") == 3


# ---------------------------------------------------------------- 「지금 갱신」


def test_a_recorded_day_is_never_asked_again(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = publication(WED)
    _refresh(fx)
    again = _refresh(fx)
    assert (again.outcome, again.publication_date) == (RefreshOutcome.ALREADY_RECORDED, None)
    assert source.calls == [(KEY, WED)]
    assert [json.loads(d)["result"] for _, _, d, _ in _audit(config)] == [
        "RECORDED",
        "ALREADY_RECORDED",
    ]


def test_a_weekend_refresh_records_the_most_recent_business_day(
    fx: Container, source: FakeSource, clock: FakeClock
) -> None:
    _keyed(fx)
    clock.advance(3 * 86400)  # Saturday noon
    source.answers[THU] = publication(THU)  # Friday was a holiday
    result = _refresh(fx)
    assert (result.outcome, result.publication_date) == (RefreshOutcome.RECORDED, THU)
    assert source.calls == [(KEY, FRI), (KEY, THU)]
    assert {rate.reason for rate in result.status.rates} == {None}
    # A day not recorded is asked again (nothing tells a holiday from a late publication) ...
    source.answers[FRI] = publication(FRI)
    assert _refresh(fx).publication_date == FRI
    assert source.calls[2:] == [(KEY, FRI)]
    # ... and once Friday is held nothing newer can exist until Monday: no call is made.
    assert _refresh(fx).outcome is RefreshOutcome.ALREADY_RECORDED
    assert len(source.calls) == 3


def test_a_morning_refresh_records_yesterday(
    fx: Container, source: FakeSource, clock: FakeClock
) -> None:
    _keyed(fx)
    clock.advance(-3 * 3600)  # Wednesday 09:00 KST: today is not out yet
    source.answers[TUE] = publication(TUE)
    result = _refresh(fx)
    assert (result.outcome, result.publication_date) == (RefreshOutcome.RECORDED, TUE)
    assert source.calls == [(KEY, WED), (KEY, TUE)]
    # Later the same day, only today is asked for.
    clock.advance(3 * 3600)
    source.answers[WED] = publication(WED)
    assert _refresh(fx).publication_date == WED
    assert source.calls[2:] == [(KEY, WED)]


def test_nothing_published_inside_the_window_records_nothing(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    result = _refresh(fx)
    assert (result.outcome, result.publication_date) == (RefreshOutcome.NOT_PUBLISHED, FRI_BEFORE)
    # Today, then each earlier business day while at most three business days old.
    assert source.calls == [(KEY, WED), (KEY, TUE), (KEY, MON), (KEY, FRI_BEFORE)]
    assert count(config, "fx_snapshots") == 0
    assert {rate.reason for rate in result.status.rates} == {PRICING_FX_UNAVAILABLE}
    assert [json.loads(d)["result"] for _, _, d, _ in _audit(config)] == ["NOT_PUBLISHED"] * 4


def test_a_failure_records_nothing_and_stops(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = Ap01Failure(FX_SOURCE_AUTHKEY_INVALID)
    source.answers[TUE] = publication(TUE)
    result = _refresh(fx)
    assert (result.outcome, result.code) == (RefreshOutcome.FAILED, FX_SOURCE_AUTHKEY_INVALID)
    assert source.calls == [(KEY, WED)]
    assert count(config, "fx_snapshots") == 0
    ((_, reason, details, _),) = _audit(config)
    assert reason == FX_SOURCE_AUTHKEY_INVALID and json.loads(details)["result"] == "FAILED"


def test_a_refresh_needs_the_key(fx: Container, source: FakeSource) -> None:
    with pytest.raises(PolicyBlockedError) as refused:
        _refresh(fx)
    assert refused.value.code == FX_API_KEY_MISSING
    assert source.calls == []


def test_a_rate_turns_stale_after_three_business_days(
    fx: Container, source: FakeSource, clock: FakeClock
) -> None:
    _keyed(fx)
    assert {rate.reason for rate in fx.fx.status().rates} == {PRICING_FX_UNAVAILABLE}
    clock.advance(-2 * 86400)  # Monday
    source.answers[MON] = publication(MON)
    _refresh(fx)
    clock.advance(3 * 86400)  # Thursday: three business days on
    assert {rate.reason for rate in fx.fx.status().rates} == {None}
    clock.advance(86400)  # Friday: four
    assert {rate.reason for rate in fx.fx.status().rates} == {PRICING_FX_STALE}
    assert fx.fx.rate("CNY").reason == PRICING_FX_STALE


# ---------------------------------------------------------------- the scheduler


@pytest.mark.parametrize(
    ("moment", "due"),
    [
        (_kst(WED, 10, 59), False),
        (_kst(WED, 11, 0), True),
        (_kst(WED, 23, 59), True),
        (_kst(SAT, 12), False),
        (_kst(date(2026, 10, 18), 12), False),
        (_kst(MON, 11), True),
    ],
)
def test_the_scheduler_reads_only_on_a_business_day_after_eleven(
    config: AppConfig, source: FakeSource, moment: datetime, due: bool
) -> None:
    with _container(config, FakeClock(moment), source) as built:
        assert built.fx.due() is False  # no key yet
        _keyed(built)
        assert built.fx.due() is due
    assert source.calls == []


def test_the_scheduler_pass_records_today_once(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = publication(WED)
    result = fx.fx.run_due("cid-auto")
    assert result is not None and result.outcome is RefreshOutcome.RECORDED
    assert source.calls == [(KEY, WED)]
    ((actor, _, details, _),) = _audit(config)
    assert actor == SCHEDULER_ACTOR and json.loads(details)["trigger"] == "AUTO"
    # Today is recorded: the pass is not due again.
    assert fx.fx.due() is False and fx.fx.run_due("cid-auto-2") is None
    assert len(source.calls) == 1


def test_an_unpublished_day_is_asked_again_once_per_interval(
    fx: Container, source: FakeSource, clock: FakeClock
) -> None:
    _keyed(fx)
    first = fx.fx.run_due("cid-auto")
    assert first is not None and first.outcome is RefreshOutcome.NOT_PUBLISHED
    # The scheduler asks for today only, never a fallback day.
    assert source.calls == [(KEY, WED)]
    clock.advance(3599)
    assert fx.fx.due() is False and fx.fx.run_due("cid-auto") is None
    clock.advance(1)
    source.answers[WED] = publication(WED)
    second = fx.fx.run_due("cid-auto")
    assert second is not None and second.outcome is RefreshOutcome.RECORDED
    assert source.calls == [(KEY, WED), (KEY, WED)]


def test_a_disabled_interval_never_runs(data_dir: Path, source: FakeSource) -> None:
    config = make_config(data_dir, fx_refresh_interval_s=0.0)
    with _container(config, FakeClock(_kst(WED, 12)), source) as built:
        _keyed(built)
        assert built.fx.due() is False and built.fx.run_due("cid") is None
        scheduler = FxRefreshScheduler(built.fx, tick_s=0.01)
        before = threading.active_count()
        scheduler.start()
        assert threading.active_count() == before
        scheduler.stop()
    assert source.calls == []


def test_the_scheduler_thread_runs_the_due_pass(
    fx: Container, config: AppConfig, source: FakeSource
) -> None:
    _keyed(fx)
    source.answers[WED] = publication(WED)
    scheduler = FxRefreshScheduler(fx.fx, tick_s=0.01)
    scheduler.start()
    try:
        deadline = time.monotonic() + 10
        while count(config, "fx_snapshots") < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        scheduler.stop()
    assert count(config, "fx_snapshots") == 3
    assert source.calls == [(KEY, WED)]


# ---------------------------------------------------------------- the real reader and egress


def test_the_real_reader_is_granted_the_eximbank_host_alone(
    config: AppConfig, clock: FakeClock
) -> None:
    grants: list[tuple[str, frozenset[str]] | None] = []
    urls: list[httpx.URL] = []

    def answer(request: httpx.Request) -> httpx.Response:
        grant = egress._active_grant.get()
        grants.append(None if grant is None else (grant.owner, grant.hosts))
        urls.append(request.url)
        return httpx.Response(200, content=_body())

    reader = KoreaEximAp01(transport=httpx.MockTransport(answer))
    with _container(config, clock, reader) as built:
        _keyed(built)
        result = _refresh(built)
        assert result.outcome is RefreshOutcome.RECORDED
        assert count(config, "fx_snapshots") == 3
    assert grants == [("fx:koreaexim:ap01", frozenset({"oapi.koreaexim.go.kr"}))]
    assert [(u.host, u.params["searchdate"]) for u in urls] == [
        ("oapi.koreaexim.go.kr", "20261014")
    ]


# ---------------------------------------------------------------- the API


@pytest.fixture
def api(config: AppConfig, source: FakeSource) -> Iterator[TestClient]:
    source.always = True
    app = create_app(config, extra_jobs=TEST_JOBS, fx_source=source)
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


def test_the_key_is_write_only_and_never_kept_outside_the_secret_store(
    api: TestClient, config: AppConfig, source: FakeSource
) -> None:
    path = "/api/v1/settings/overseas/fx-key"
    assert api.get(path).json() == {"configured": False}
    # The global CSRF guard: no client header, no write.
    assert api.put(path, json={"key": KEY}).status_code == 403
    assert api.get(path).json() == {"configured": False}
    short = api.put(path, json={"key": "short-key"}, headers=HEADERS)
    assert short.status_code == 422 and short.json()["error"]["code"] == "FX_API_KEY_INVALID"
    assert "short-key" not in short.text
    extra = api.put(path, json={"key": KEY, "echo": 1}, headers=HEADERS)
    assert extra.status_code == 422 and KEY not in extra.text
    saved = api.put(path, json={"key": KEY}, headers=HEADERS)
    assert saved.status_code == 200 and saved.json() == {"configured": True}
    assert KEY not in saved.text
    assert api.get(path).json() == {"configured": True}
    refreshed = api.post("/api/v1/settings/overseas/fx/refresh", headers=HEADERS)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["outcome"] == "RECORDED" and KEY not in refreshed.text
    assert source.calls and {key for key, _ in source.calls} == {KEY}
    assert api.delete(path).status_code == 403
    assert api.get(path).json() == {"configured": True}
    deleted = api.delete(path, headers=HEADERS)
    assert deleted.status_code == 200 and deleted.json() == {"configured": False}
    changes = [json.loads(d) for _, _, d, _ in _audit(config, "FX_API_KEY_CHANGED")]
    assert changes == [{"configured": True}, {"configured": False}]
    assert KEY not in _all_audit_text(config)
    # Nowhere on disk but the secret store: not in the database, its WAL or the log.
    runtime = config.data_dir / "runtime"
    for stored in [*runtime.glob("icbm.db*"), *(config.data_dir / "logs").glob("*")]:
        assert KEY.encode() not in stored.read_bytes(), stored


def test_the_rate_routes(api: TestClient) -> None:
    unknown = api.get("/api/v1/settings/overseas/fx/EUR")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "FX_CURRENCY_UNKNOWN"
    status = api.get("/api/v1/settings/overseas/fx").json()
    assert status["key_configured"] is False and status["source"] == "koreaexim/AP01"
    assert [(r["currency"], r["tts"], r["reason"]) for r in status["rates"]] == [
        ("CNY", None, PRICING_FX_UNAVAILABLE),
        ("JPY", None, PRICING_FX_UNAVAILABLE),
        ("USD", None, PRICING_FX_UNAVAILABLE),
    ]
    refresh = "/api/v1/settings/overseas/fx/refresh"
    assert api.post(refresh).status_code == 403
    missing = api.post(refresh, headers=HEADERS)
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == FX_API_KEY_MISSING
    api.put("/api/v1/settings/overseas/fx-key", json={"key": KEY}, headers=HEADERS)
    assert api.post(refresh, headers=HEADERS).json()["outcome"] == "RECORDED"
    usd = api.get("/api/v1/settings/overseas/fx/USD").json()
    assert (usd["tts"], usd["unit"], usd["source_code"], usd["reason"]) == (
        "1399.9",
        1,
        "USD",
        None,
    )


# ---------------------------------------------------------------- migration 0064


def test_0064_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)
    # Judged at 0064 itself, whatever later revisions add (0065: ADR-0038 O2).
    command.downgrade(alembic_config(url), "0064_fx_snapshots")
    added = {"fx_snapshots", "overseas_settings_revisions"}

    def tables() -> set[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = tables()
    assert added <= before
    # Empty, it steps down to exactly what it added, and back up.
    command.downgrade(alembic_config(url), "0063_source_currency")
    assert before - tables() == added
    command.upgrade(alembic_config(url), "0064_fx_snapshots")
    assert tables() == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO fx_snapshots (snapshot_id, publication_date, currency, rate_type,"
            " source_code, unit, tts, deal_bas_r, published_json, source, fetched_at,"
            " correlation_id) VALUES ('s-1', '2026-10-14', 'JPY', 'TTS', 'JPY(100)', 100,"
            " '938.4', '929.11', '{}', 'koreaexim/AP01', '2026-10-14 03:00:00', 'cid')"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0063_source_currency")
    assert tables() == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fx_snapshots").fetchone()[0] == 1


def test_a_settings_revision_alone_also_holds_the_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO overseas_settings_revisions (revision_id, revision_no, fx_margin_bp,"
            " overseas_shipping_default_krw, duties_included_over_usd150, duty_rate_bp,"
            " smartstore_overseas_shipping_address_id, overseas_delivery_days_min,"
            " overseas_delivery_days_max, importer_label, fingerprint, changed_by,"
            " correlation_id, changed_at) VALUES ('r-1', 1, 700, NULL, 1, 800, NULL, 7, 14,"
            " NULL, ?, 'x', 'cid', '2026-10-14 03:00:00')",
            ("a" * 64,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0063_source_currency")


def test_the_fixture_clock_is_wednesday_noon_kst(clock: FakeClock) -> None:
    assert clock.now().astimezone(KST) == datetime(2026, 10, 14, 12, tzinfo=KST)
    assert clock.now() - _kst(WED, 11) == timedelta(hours=1)
