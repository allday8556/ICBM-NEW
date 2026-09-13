from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.connect.state import ConnectionState
from app.db.base import Base
from app.db.types import UTCDateTime

STATES = ", ".join(f"'{state.value}'" for state in ConnectionState)


class SupplierConnection(Base):
    """Canonical supplier connection identity (ROADMAP §4.1, Issue #7).

    One row per ``supplier_key``: the identity cannot be duplicated. Secrets live in the OS secret
    store and the encrypted session file, never here; the counters are safe diagnostics only.
    """

    __tablename__ = "supplier_connections"
    __table_args__ = (
        UniqueConstraint("supplier_key"),
        CheckConstraint(f"state IN ({STATES})", name="state_valid"),
        CheckConstraint("state <> 'READY' OR last_verified_at IS NOT NULL", name="ready_is_proven"),
        CheckConstraint(
            "consecutive_auth_failures >= 0 AND real_login_attempts >= 0 "
            "AND session_reuse_count >= 0 AND reauth_count >= 0",
            name="counters_non_negative",
        ),
    )

    connection_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str] = mapped_column(String(200))
    auth_required: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String(20))
    auto_connect: Mapped[bool] = mapped_column(Boolean)
    consecutive_auth_failures: Mapped[int] = mapped_column(Integer)
    real_login_attempts: Mapped[int] = mapped_column(Integer)
    session_reuse_count: Mapped[int] = mapped_column(Integer)
    reauth_count: Mapped[int] = mapped_column(Integer)
    last_login_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_class: Mapped[str | None] = mapped_column(String(20))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
