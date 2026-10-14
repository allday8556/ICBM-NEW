"""The 해외 구매대행 settings (ADR-0038 §5): one versioned card in Settings › 해외구매대행.

| setting | default |
| --- | --- |
| ``fx_margin_percent`` | 5 (owner ``6109161005``) |
| ``overseas_shipping_default_krw`` | unset |
| ``duties_included_over_usd150`` | on |
| ``duty_rate_percent`` | 8 |
| ``smartstore_overseas_shipping_address_id`` | unset |
| ``overseas_delivery_days_min`` / ``_max`` | 7 / 14 |
| ``importer_label`` | unset |

With no revision the defaults apply and nothing is seeded. The settings are inputs only: nothing
here prices, converts or decides anything (the landed cost is O2's, ADR-0038 §4).

**Exact numbers.** A percentage is stated as decimal text with at most two decimals, 0–100
(``"5"``, ``"5.25"``), and stored as integer basis points (``525``); it is never a float. It is
shown back canonically (``"5.5"``, never ``"5.50"``).

**Revisions are append-only and server-created**, exactly like the supplier sales policy
(ADR-0039 §1): a save names the revision it was edited from (``expected_revision_no``, ``None``
while none exists); a save against a moved revision is refused
(``OVERSEAS_SETTINGS_REVISION_MOVED``), never merged, and a save identical to the current settings
is refused too (``OVERSEAS_SETTINGS_UNCHANGED``). One revision, its fingerprint and its audit
event are written in one unit of work, or nothing is.

**Pins** (FX-02). :meth:`OverseasSettings.pin` is what a price of an overseas unit will pin: the
revision number and its fingerprint, or ``None`` for the defaults.
"""

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.overseas.settings_models import OverseasSettingsRevision
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass
from app.platform.db.database import Database

CONTENT_VERSION: Final = "overseas-settings/v1"
# The Settings surface name the server lists as editable (``EditableSurface``).
OVERSEAS_SETTINGS_SURFACE: Final = "OVERSEAS_SETTINGS"

OVERSEAS_SETTINGS_REVISION_MOVED: Final = "OVERSEAS_SETTINGS_REVISION_MOVED"
OVERSEAS_SETTINGS_UNCHANGED: Final = "OVERSEAS_SETTINGS_UNCHANGED"

DEFAULT_FX_MARGIN_BP: Final = 500
DEFAULT_DUTY_RATE_BP: Final = 800
DEFAULT_DELIVERY_DAYS: Final = (7, 14)
MAX_DELIVERY_DAYS: Final = 60
MAX_SHIPPING_DEFAULT_KRW: Final = 10_000_000
MAX_IMPORTER_LABEL: Final = 50
_PERCENT: Final = re.compile(r"^(?:0|[1-9]\d{0,2})(?:\.\d{1,2})?$")
_ADDRESS_ID: Final = re.compile(r"^[0-9]{1,20}$")


class OverseasSettingsConflictError(AppError):
    """A save the current settings refuse: they moved since they were read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


def percent_to_bp(text: str) -> int:
    """``"5.25"`` → ``525``. The text is validated by ``_PERCENT`` and the 0–100 bound first."""
    whole, _, fraction = text.partition(".")
    return int(whole) * 100 + int((fraction + "00")[:2])


def bp_to_percent(bp: int) -> str:
    """``525`` → ``"5.25"``, ``550`` → ``"5.5"``, ``500`` → ``"5"``: canonical decimal text."""
    whole, fraction = divmod(bp, 100)
    return str(whole) if not fraction else f"{whole}.{fraction:02d}".rstrip("0")


@dataclass(frozen=True)
class OverseasSettings:
    """The settings as they hold now. ``revision_no`` is ``None`` while the defaults apply."""

    fx_margin_bp: int = DEFAULT_FX_MARGIN_BP
    overseas_shipping_default_krw: int | None = None
    duties_included_over_usd150: bool = True
    duty_rate_bp: int = DEFAULT_DUTY_RATE_BP
    smartstore_overseas_shipping_address_id: str | None = None
    overseas_delivery_days_min: int = DEFAULT_DELIVERY_DAYS[0]
    overseas_delivery_days_max: int = DEFAULT_DELIVERY_DAYS[1]
    importer_label: str | None = None
    revision_id: str | None = None
    revision_no: int | None = None
    fingerprint: str | None = None
    changed_by: str | None = None
    changed_at: datetime | None = None

    def content(self) -> dict[str, Any]:
        """The values only: what a revision states and its fingerprint covers."""
        values = asdict(self)
        for key in ("revision_id", "revision_no", "fingerprint", "changed_by", "changed_at"):
            values.pop(key)
        return values

    def pin(self) -> tuple[int, str] | None:
        """What a price pins (FX-02): the revision number and fingerprint, ``None`` for the
        defaults."""
        if self.revision_no is None or self.fingerprint is None:
            return None
        return (self.revision_no, self.fingerprint)


def content_fingerprint(content: dict[str, Any]) -> str:
    """SHA-256 of the canonical content of one revision."""
    text = json.dumps(
        {"content_version": CONTENT_VERSION, **content},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _newest(session: Session) -> OverseasSettingsRevision | None:
    return session.scalars(
        select(OverseasSettingsRevision)
        .order_by(OverseasSettingsRevision.revision_no.desc())
        .limit(1)
    ).first()


def _settings(row: OverseasSettingsRevision | None) -> OverseasSettings:
    if row is None:
        return OverseasSettings()
    return OverseasSettings(
        fx_margin_bp=row.fx_margin_bp,
        overseas_shipping_default_krw=row.overseas_shipping_default_krw,
        duties_included_over_usd150=bool(row.duties_included_over_usd150),
        duty_rate_bp=row.duty_rate_bp,
        smartstore_overseas_shipping_address_id=row.smartstore_overseas_shipping_address_id,
        overseas_delivery_days_min=row.overseas_delivery_days_min,
        overseas_delivery_days_max=row.overseas_delivery_days_max,
        importer_label=row.importer_label,
        revision_id=row.revision_id,
        revision_no=row.revision_no,
        fingerprint=row.fingerprint,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


def overseas_settings_of(session: Session) -> OverseasSettings:
    """The settings now, read inside a caller's unit of work: the newest revision, or the
    defaults when none exists. The one read the pricing owner will use (O2)."""
    return _settings(_newest(session))


class OverseasSettingsStore:
    """The only writer of ``overseas_settings_revisions``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def current(self) -> OverseasSettings:
        with self._db.read() as session:
            return overseas_settings_of(session)

    def append(
        self,
        wanted: OverseasSettings,
        *,
        expected_revision_no: int | None,
        changed_by: str,
        correlation_id: str,
    ) -> OverseasSettings:
        """Append one revision stating ``wanted``'s values, with its audit event, in one unit of
        work — or change nothing."""
        now = self._clock.now()
        content = wanted.content()
        with self._db.write() as session:
            current = _newest(session)
            if (None if current is None else current.revision_no) != expected_revision_no:
                raise OverseasSettingsConflictError(
                    OVERSEAS_SETTINGS_REVISION_MOVED,
                    "the overseas settings changed since they were read; reload them before saving",
                    details={
                        "current_revision_no": None if current is None else current.revision_no
                    },
                )
            before = _settings(current)
            if before.content() == content:
                raise OverseasSettingsConflictError(
                    OVERSEAS_SETTINGS_UNCHANGED,
                    "the saved overseas settings are identical to the current ones",
                )
            fingerprint = content_fingerprint(content)
            row = OverseasSettingsRevision(
                revision_id=str(uuid.uuid4()),
                revision_no=1 if current is None else current.revision_no + 1,
                fingerprint=fingerprint,
                changed_by=changed_by,
                correlation_id=correlation_id,
                changed_at=now,
                **content,
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.OVERSEAS_SETTINGS_REVISED,
                    action="OVERSEAS_SETTINGS_REVISION_APPENDED",
                    actor=changed_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=OVERSEAS_SETTINGS_SURFACE,
                    before={"revision_no": before.revision_no, **before.content()},
                    after={
                        "revision_id": row.revision_id,
                        "revision_no": row.revision_no,
                        "fingerprint": fingerprint,
                        **content,
                    },
                    details={"content_version": CONTENT_VERSION},
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _settings(row)


# ------------------------------------------------------------------ the application contract

Percent = Annotated[StrictStr, Field(max_length=6)]


def _percent(value: str) -> str:
    if not _PERCENT.fullmatch(value) or percent_to_bp(value) > 10000:
        raise ValueError("a percentage is 0 to 100 with at most two decimals, as text")
    return value


def _optional_text(value: str | None, *, limit: int, what: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > limit:
        raise ValueError(f"{what} is 1 to {limit} characters, or null")
    return text


class OverseasSettingsView(BaseModel):
    """The settings as the server holds them. ``revision_no`` is ``None`` while the defaults
    apply; ``editable`` is the server's answer. Percentages are canonical decimal text."""

    surface: str
    editable: bool
    fx_margin_percent: str
    overseas_shipping_default_krw: int | None
    duties_included_over_usd150: bool
    duty_rate_percent: str
    smartstore_overseas_shipping_address_id: str | None
    overseas_delivery_days_min: int
    overseas_delivery_days_max: int
    importer_label: str | None
    revision_no: int | None
    fingerprint: str | None
    changed_by: str | None
    changed_at: datetime | None


class SaveOverseasSettingsRequest(BaseModel):
    """One save. Every field is required (a cleared one is ``null``): nothing is defaulted by the
    server. ``expected_revision_no`` is the revision the operator edited from."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_no: StrictInt | None
    fx_margin_percent: Percent
    overseas_shipping_default_krw: (
        Annotated[StrictInt, Field(ge=0, le=MAX_SHIPPING_DEFAULT_KRW)] | None
    )
    duties_included_over_usd150: StrictBool
    duty_rate_percent: Percent
    smartstore_overseas_shipping_address_id: Annotated[StrictStr, Field(max_length=40)] | None
    overseas_delivery_days_min: Annotated[StrictInt, Field(ge=1, le=MAX_DELIVERY_DAYS)]
    overseas_delivery_days_max: Annotated[StrictInt, Field(ge=1, le=MAX_DELIVERY_DAYS)]
    importer_label: Annotated[StrictStr, Field(max_length=200)] | None

    @field_validator("fx_margin_percent", "duty_rate_percent")
    @classmethod
    def _valid_percent(cls, value: str) -> str:
        return _percent(value)

    @field_validator("smartstore_overseas_shipping_address_id")
    @classmethod
    def _valid_address(cls, value: str | None) -> str | None:
        text = _optional_text(value, limit=20, what="the SmartStore address id")
        if text is not None and not _ADDRESS_ID.fullmatch(text):
            raise ValueError("the SmartStore address id is the 주소록 number, digits only")
        return text

    @field_validator("importer_label")
    @classmethod
    def _valid_importer(cls, value: str | None) -> str | None:
        return _optional_text(value, limit=MAX_IMPORTER_LABEL, what="the importer label")

    @model_validator(mode="after")
    def _days_in_order(self) -> "SaveOverseasSettingsRequest":
        if self.overseas_delivery_days_min > self.overseas_delivery_days_max:
            raise ValueError("the delivery days run from the minimum to the maximum")
        return self


class OverseasSettingsService:
    """What the 해외 구매대행 card reads and saves, and nothing else."""

    def __init__(self, store: OverseasSettingsStore) -> None:
        self._store = store

    def current(self) -> OverseasSettingsView:
        return _view(self._store.current())

    def save(
        self, request: SaveOverseasSettingsRequest, *, actor: str, correlation_id: str
    ) -> OverseasSettingsView:
        wanted = OverseasSettings(
            fx_margin_bp=percent_to_bp(request.fx_margin_percent),
            overseas_shipping_default_krw=request.overseas_shipping_default_krw,
            duties_included_over_usd150=request.duties_included_over_usd150,
            duty_rate_bp=percent_to_bp(request.duty_rate_percent),
            smartstore_overseas_shipping_address_id=request.smartstore_overseas_shipping_address_id,
            overseas_delivery_days_min=request.overseas_delivery_days_min,
            overseas_delivery_days_max=request.overseas_delivery_days_max,
            importer_label=request.importer_label,
        )
        return _view(
            self._store.append(
                wanted,
                expected_revision_no=request.expected_revision_no,
                changed_by=actor,
                correlation_id=correlation_id,
            )
        )


def _view(settings: OverseasSettings) -> OverseasSettingsView:
    return OverseasSettingsView(
        surface=OVERSEAS_SETTINGS_SURFACE,
        editable=True,
        fx_margin_percent=bp_to_percent(settings.fx_margin_bp),
        overseas_shipping_default_krw=settings.overseas_shipping_default_krw,
        duties_included_over_usd150=settings.duties_included_over_usd150,
        duty_rate_percent=bp_to_percent(settings.duty_rate_bp),
        smartstore_overseas_shipping_address_id=settings.smartstore_overseas_shipping_address_id,
        overseas_delivery_days_min=settings.overseas_delivery_days_min,
        overseas_delivery_days_max=settings.overseas_delivery_days_max,
        importer_label=settings.importer_label,
        revision_no=settings.revision_no,
        fingerprint=settings.fingerprint,
        changed_by=settings.changed_by,
        changed_at=settings.changed_at,
    )
