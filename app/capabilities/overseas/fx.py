"""The FX snapshot owner (ADR-0038 §3; FX-02, FX-03): Korea Eximbank's daily TTS, kept immutable.

**What is recorded.** One immutable row per publication day and currency — ``CNY`` (published
``CNH``), ``JPY`` (``JPY(100)``, per 100 yen) and ``USD`` — of Korea Eximbank's 현재환율
(``koreaexim/AP01``), rate type TTS (송금 보낼때, the owner's choice). The rate is kept per its
publication unit as exact decimal text (``fx_models``); :meth:`FxSnapshot.tts_per_unit` divides by
the unit exactly. A publication is read strictly by the adapter (``integrations.fx.koreaexim``):
an empty answer is a day without one, anything malformed is a typed failure, and either records
nothing. Every fetch is audited, without the key.

**"Live"** means the newest published snapshot at the time of pricing (§3). :func:`newest_snapshot`
is that read for O2: the newest snapshot of a currency published on or before a day.

**Fail closed** (FX-03), :func:`fx_reason`:

- no snapshot at all → ``PRICING_FX_UNAVAILABLE``;
- a snapshot published more than **3 business days** before the day → ``PRICING_FX_STALE``.

A business day is Monday to Friday, counted in KST. **There is no holiday calendar**: a run of
weekday holidays (설날, 추석) counts as business days, so a rate can turn stale over a long holiday.
That errs on the side of REVIEW_REQUIRED, never of a guessed rate.

**Refresh.** Once a business day, after 11:00 KST (the usual publication time), the scheduler
reads today's publication — only while the key is configured and today has no snapshot yet. The
operator's 「지금 갱신」 asks now: today's publication if it is a business day, else (or when
today's is not out yet) the most recent business day not yet recorded within the FX-03 window, so
a first setup on a weekend or a morning records the rate that holds. Both go through one lock; a
refresh never runs twice at once.

**The API key** is the operator's own, and lives only in the OS secret store under
``FX_API_KEY_REF`` (ADR-0001, the AI provider key's pattern, ADR-0027 §2): written through the API,
never returned, stored in the database, logged, audited or put in an error. Reads say only whether
it is configured.
"""

import logging
import re
import threading
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import select

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.overseas.fx_models import FxRateSnapshot

# The pure reads and rules, in their own module so a pricing owner reads a rate without loading
# the publication adapter (ADR-0038 O2); re-exported here as this owner's own names.
from app.capabilities.overseas.fx_rules import CURRENCIES as CURRENCIES
from app.capabilities.overseas.fx_rules import KST as KST
from app.capabilities.overseas.fx_rules import PRICING_FX_STALE as PRICING_FX_STALE
from app.capabilities.overseas.fx_rules import PRICING_FX_UNAVAILABLE as PRICING_FX_UNAVAILABLE
from app.capabilities.overseas.fx_rules import (
    STALE_AFTER_BUSINESS_DAYS as STALE_AFTER_BUSINESS_DAYS,
)
from app.capabilities.overseas.fx_rules import FxSnapshot as FxSnapshot
from app.capabilities.overseas.fx_rules import business_days_after as business_days_after
from app.capabilities.overseas.fx_rules import fx_reason as fx_reason
from app.capabilities.overseas.fx_rules import kst_day as kst_day
from app.capabilities.overseas.fx_rules import newest_snapshot as newest_snapshot
from app.capabilities.overseas.fx_rules import snapshot_of as _snapshot
from app.platform.core.clock import Clock
from app.platform.core.errors import (
    AppError,
    ErrorClass,
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
)
from app.platform.core.secrets import SecretStore
from app.platform.db.database import Database
from integrations.fx.koreaexim import (
    SOURCE,
    Ap01Failure,
    Ap01NotPublished,
    Ap01Publication,
    Ap01Result,
)

logger = logging.getLogger("icbm.overseas.fx")

RATE_TYPE: Final = "TTS"
# Korea Eximbank publishes the day's first rates at about 11:00 KST.
PUBLICATION_HOUR_KST: Final = 11
# The OS secret-store name of the operator's Korea Eximbank API key (ADR-0007 §3).
FX_API_KEY_REF: Final = "fx.koreaexim.authkey"
_API_KEY: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SCHEDULER_ACTOR: Final = "system:fx-refresh"

FX_API_KEY_MISSING: Final = "FX_API_KEY_MISSING"
FX_API_KEY_INVALID: Final = "FX_API_KEY_INVALID"
FX_CURRENCY_UNKNOWN: Final = "FX_CURRENCY_UNKNOWN"
FX_REFRESH_BUSY: Final = "FX_REFRESH_BUSY"


class FxRefreshBusyError(AppError):
    error_class = ErrorClass.CONFLICT


class RefreshTrigger(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class RefreshOutcome(StrEnum):
    RECORDED = "RECORDED"
    # The newest publication that could exist is already recorded: no call was made.
    ALREADY_RECORDED = "ALREADY_RECORDED"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    FAILED = "FAILED"


class FxSource(Protocol):
    """The publication source port: one read of one day with the operator's key."""

    def fetch(self, authkey: str, day: date) -> Ap01Result: ...


# ------------------------------------------------------------------ the store


class FxSnapshotStore:
    """The only writer of ``fx_snapshots``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def newest(self, currency: str, as_of: date) -> FxSnapshot | None:
        with self._db.read() as session:
            return newest_snapshot(session, currency, as_of)

    def latest(self, as_of: date) -> dict[str, FxSnapshot | None]:
        """The newest snapshot of each currency on or before ``as_of``, in ``CURRENCIES`` order."""
        with self._db.read() as session:
            return {currency: newest_snapshot(session, currency, as_of) for currency in CURRENCIES}

    def complete_through(self, as_of: date) -> date | None:
        """The newest day on or before ``as_of`` by which every currency has a snapshot: the
        oldest of their newest publication days."""
        latest = self.latest(as_of)
        days = [snap.publication_date for snap in latest.values() if snap is not None]
        return min(days) if len(days) == len(CURRENCIES) else None

    def record(
        self,
        publication: Ap01Publication,
        *,
        trigger: RefreshTrigger,
        actor: str,
        correlation_id: str,
    ) -> list[FxSnapshot]:
        """Record one publication's rates and its audit event in one unit of work. A currency the
        day already holds is kept as it is (the rows are immutable); the rest are added."""
        now = self._clock.now()
        day = publication.day.isoformat()
        with self._db.write() as session:
            held = set(
                session.scalars(
                    select(FxRateSnapshot.currency).where(FxRateSnapshot.publication_date == day)
                )
            )
            rows = [
                FxRateSnapshot(
                    snapshot_id=str(uuid.uuid4()),
                    publication_date=day,
                    currency=rate.currency,
                    rate_type=RATE_TYPE,
                    source_code=rate.source_code,
                    unit=rate.unit,
                    tts=rate.tts,
                    deal_bas_r=rate.deal_bas_r,
                    published_json=rate.published_json,
                    source=SOURCE,
                    fetched_at=now,
                    correlation_id=correlation_id,
                )
                for rate in publication.rates
                if rate.currency not in held
            ]
            session.add_all(rows)
            session.flush()
            self._audit.append(
                _fetch_entry(
                    trigger,
                    actor,
                    correlation_id,
                    publication.day,
                    RefreshOutcome.RECORDED,
                    after={
                        "publication_date": day,
                        "snapshots": [
                            {
                                "snapshot_id": row.snapshot_id,
                                "currency": row.currency,
                                "source_code": row.source_code,
                                "unit": row.unit,
                                "tts": row.tts,
                            }
                            for row in rows
                        ],
                    },
                ),
                session=session,
            )
            return [_snapshot(row) for row in rows]

    def audit_fetch(
        self,
        trigger: RefreshTrigger,
        actor: str,
        correlation_id: str,
        day: date | None,
        outcome: RefreshOutcome,
        failure: Ap01Failure | None = None,
    ) -> None:
        """Audit a fetch that recorded nothing (or a refresh that needed none)."""
        self._audit.append(_fetch_entry(trigger, actor, correlation_id, day, outcome, failure))


def _fetch_entry(
    trigger: RefreshTrigger,
    actor: str,
    correlation_id: str,
    day: date | None,
    outcome: RefreshOutcome,
    failure: Ap01Failure | None = None,
    after: dict[str, object] | None = None,
) -> AuditEntry:
    details: dict[str, object] = {
        "trigger": trigger.value,
        "source": SOURCE,
        "searchdate": None if day is None else day.isoformat(),
        "result": outcome.value,
    }
    if failure is not None and failure.http_status is not None:
        details["http_status"] = failure.http_status
    return AuditEntry(
        event_type=AuditEventType.FX_RATES_FETCHED,
        action="FX_AP01_FETCH",
        actor=actor,
        outcome=AuditOutcome.RECORDED,
        target_ref=SOURCE,
        reason_code=None if failure is None else failure.code,
        after=after,
        details=details,
        correlation_id=correlation_id,
    )


# ------------------------------------------------------------------ the key


class FxApiKey:
    """The operator's Korea Eximbank key in the OS secret store, and only there."""

    def __init__(self, secrets: SecretStore, audit: AuditLog) -> None:
        self._secrets = secrets
        self._audit = audit

    def value(self) -> str | None:
        """The key, for the one adapter call that sends it; ``None`` when it is not there."""
        try:
            return self._secrets.get(FX_API_KEY_REF)
        except Exception:  # an unreadable secret store is a missing key, never a crash
            return None

    def configured(self) -> bool:
        return bool(self.value())

    def store(self, key: str, *, actor: str, correlation_id: str) -> None:
        if not _API_KEY.fullmatch(key):
            raise InputValidationError(
                FX_API_KEY_INVALID, "the API key is 16 to 128 letters, digits, '-' or '_'"
            )
        self._secrets.set(FX_API_KEY_REF, key)
        self._changed("FX_API_KEY_SET", True, actor, correlation_id)

    def forget(self, *, actor: str, correlation_id: str) -> None:
        self._secrets.delete(FX_API_KEY_REF)
        self._changed("FX_API_KEY_DELETED", False, actor, correlation_id)

    def _changed(self, action: str, configured: bool, actor: str, correlation_id: str) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.FX_API_KEY_CHANGED,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=FX_API_KEY_REF,
                details={"configured": configured},
                correlation_id=correlation_id,
            )
        )


# ------------------------------------------------------------------ the application contract


class FxRateView(BaseModel):
    """The newest snapshot of one currency on today's KST day, and its FX-03 reason (``None``
    when it may price). Every snapshot field is ``None`` when the currency has none."""

    currency: str
    snapshot_id: str | None
    publication_date: date | None
    rate_type: str
    source_code: str | None
    unit: int | None
    tts: str | None
    deal_bas_r: str | None
    fetched_at: datetime | None
    reason: str | None


class FxStatusView(BaseModel):
    key_configured: bool
    today: date
    source: str
    rates: list[FxRateView]


class FxRefreshView(BaseModel):
    """One refresh: its outcome, the day it recorded or last asked for, a failure code."""

    outcome: RefreshOutcome
    code: str | None
    publication_date: date | None
    status: FxStatusView


class FxKeyView(BaseModel):
    """Whether the key is configured. Nothing else about it is ever returned."""

    configured: bool


class SetFxKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Never echoed: a malformed key is refused by the owner with a code, not by validation.
    key: SecretStr


def _rate_view(currency: str, snapshot: FxSnapshot | None, today: date) -> FxRateView:
    return FxRateView(
        currency=currency,
        snapshot_id=None if snapshot is None else snapshot.snapshot_id,
        publication_date=None if snapshot is None else snapshot.publication_date,
        rate_type=RATE_TYPE,
        source_code=None if snapshot is None else snapshot.source_code,
        unit=None if snapshot is None else snapshot.unit,
        tts=None if snapshot is None else snapshot.tts,
        deal_bas_r=None if snapshot is None else snapshot.deal_bas_r,
        fetched_at=None if snapshot is None else snapshot.fetched_at,
        reason=fx_reason(snapshot, today),
    )


class FxService:
    """The FX card's reads, the key, 「지금 갱신」 and the scheduler's due check."""

    def __init__(
        self,
        store: FxSnapshotStore,
        key: FxApiKey,
        source: FxSource,
        clock: Clock,
        *,
        interval_s: float,
    ) -> None:
        self._store = store
        self._key = key
        self._source = source
        self._clock = clock
        self._interval = timedelta(seconds=max(interval_s, 0.0))
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._last_auto: datetime | None = None

    # -------------------------------------------------------------- reads

    def status(self) -> FxStatusView:
        today = kst_day(self._clock.now())
        return FxStatusView(
            key_configured=self._key.configured(),
            today=today,
            source=SOURCE,
            rates=[
                _rate_view(currency, snapshot, today)
                for currency, snapshot in self._store.latest(today).items()
            ],
        )

    def rate(self, currency: str) -> FxRateView:
        if currency not in CURRENCIES:
            raise NotFoundError(FX_CURRENCY_UNKNOWN, "no FX snapshot is kept for that currency")
        today = kst_day(self._clock.now())
        return _rate_view(currency, self._store.newest(currency, today), today)

    # -------------------------------------------------------------- the key

    def key_status(self) -> FxKeyView:
        return FxKeyView(configured=self._key.configured())

    def set_key(self, request: SetFxKeyRequest, *, actor: str, correlation_id: str) -> FxKeyView:
        self._key.store(request.key.get_secret_value(), actor=actor, correlation_id=correlation_id)
        return self.key_status()

    def delete_key(self, *, actor: str, correlation_id: str) -> FxKeyView:
        self._key.forget(actor=actor, correlation_id=correlation_id)
        return self.key_status()

    # -------------------------------------------------------------- refresh

    def refresh(self, *, actor: str, correlation_id: str) -> FxRefreshView:
        """「지금 갱신」: the operator's refresh now (module docstring)."""
        key = self._key.value()
        if not key:
            raise PolicyBlockedError(FX_API_KEY_MISSING, "save the Korea Eximbank API key first")
        if not self._lock.acquire(blocking=False):
            raise FxRefreshBusyError(FX_REFRESH_BUSY, "an FX refresh is already running")
        try:
            today = kst_day(self._clock.now())
            days = self._manual_days(today)
            return self._run(key, days, RefreshTrigger.MANUAL, actor, correlation_id)
        finally:
            self._lock.release()

    def due(self) -> bool:
        """Whether the scheduler should read today's publication now: enabled, a business day at
        or after 11:00 KST, today not yet recorded, one interval since its last attempt, and the
        key configured."""
        if self.interval_s <= 0:
            return False
        now = self._clock.now()
        local = now.astimezone(KST)
        if local.weekday() >= 5 or local.hour < PUBLICATION_HOUR_KST:
            return False
        if (self._store.complete_through(local.date()) or date.min) >= local.date():
            return False
        if self._last_auto is not None and now - self._last_auto < self._interval:
            return False
        return self._key.configured()

    def run_due(self, correlation_id: str) -> FxRefreshView | None:
        """The scheduler's pass: today's publication when :meth:`due`, else nothing."""
        if not self.due():
            return None
        key = self._key.value()
        if not key or not self._lock.acquire(blocking=False):
            return None
        try:
            self._last_auto = self._clock.now()
            today = kst_day(self._last_auto)
            return self._run(key, [today], RefreshTrigger.AUTO, SCHEDULER_ACTOR, correlation_id)
        finally:
            self._lock.release()

    def _manual_days(self, today: date) -> list[date]:
        """Today when it is a business day, then each earlier business day inside the FX-03
        window, newest first, down to (not including) the newest day already recorded."""
        held = self._store.complete_through(today) or date.min
        days: list[date] = []
        day = today
        while business_days_after(day, today) <= STALE_AFTER_BUSINESS_DAYS and day > held:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        return days

    def _run(
        self,
        key: str,
        days: Sequence[date],
        trigger: RefreshTrigger,
        actor: str,
        correlation_id: str,
    ) -> FxRefreshView:
        if not days:
            self._store.audit_fetch(
                trigger, actor, correlation_id, None, RefreshOutcome.ALREADY_RECORDED
            )
            return self._outcome(RefreshOutcome.ALREADY_RECORDED, None, None)
        for day in days:
            result = self._source.fetch(key, day)
            if isinstance(result, Ap01Publication):
                self._store.record(
                    result, trigger=trigger, actor=actor, correlation_id=correlation_id
                )
                logger.info(
                    "overseas.fx_recorded",
                    extra={"publication_date": day.isoformat(), "trigger": trigger.value},
                )
                return self._outcome(RefreshOutcome.RECORDED, None, day)
            if isinstance(result, Ap01NotPublished):
                self._store.audit_fetch(
                    trigger, actor, correlation_id, day, RefreshOutcome.NOT_PUBLISHED
                )
                continue
            self._store.audit_fetch(
                trigger, actor, correlation_id, day, RefreshOutcome.FAILED, result
            )
            logger.warning(
                "overseas.fx_fetch_failed",
                extra={"searchdate": day.isoformat(), "error_code": result.code},
            )
            return self._outcome(RefreshOutcome.FAILED, result.code, day)
        return self._outcome(RefreshOutcome.NOT_PUBLISHED, None, days[-1])

    def _outcome(
        self, outcome: RefreshOutcome, code: str | None, day: date | None
    ) -> FxRefreshView:
        return FxRefreshView(outcome=outcome, code=code, publication_date=day, status=self.status())


class FxRefreshScheduler:
    """The periodic pass (ADR-0038 §3: once a business day after 11:00 KST), on its own thread.
    ``interval_s <= 0`` disables it."""

    def __init__(self, service: FxService, *, tick_s: float = 60.0) -> None:
        self._service = service
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._service.interval_s <= 0:
            return  # disabled by policy
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="icbm-fx-refresh", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.wait(self._tick_s):
            try:
                self._service.run_due(f"fx-refresh-{uuid.uuid4()}")
            except Exception:
                logger.exception("overseas.fx_refresh_error")

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = [
    "CURRENCIES",
    "FX_API_KEY_REF",
    "PRICING_FX_STALE",
    "PRICING_FX_UNAVAILABLE",
    "FxApiKey",
    "FxRefreshScheduler",
    "FxService",
    "FxSnapshot",
    "FxSnapshotStore",
    "business_days_after",
    "fx_reason",
    "kst_day",
    "newest_snapshot",
]
