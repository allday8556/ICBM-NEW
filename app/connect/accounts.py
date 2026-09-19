"""The canonical marketplace-account owner (Issue #89 PR-B; review 5255746944, blocker 2).

CONNECT owns marketplace accounts (``docs/ARCHITECTURE.md`` §4). This module establishes the
ICBM-owned ``marketplace_account_id`` (``ACCOUNT_IDENTITY.md`` §2) from the committed M2 binding
unit and answers whether an account is still bound. It makes no provider call and never binds,
rebinds or trusts anything itself:

- an account is established only from a **complete, committed** binding of its marketplace
  connection (§5: ``binding_commit_not_proven -> NOT_BOUND``); the binding itself stays M2's
  explicit, operator-confirmed act;
- the same provider identity always resolves to the same canonical account, so no alias opens a
  second scope for one real account;
- an account whose connection no longer carries its provider identity is ``MISMATCHED`` (§4), and
  no registration state may be opened for it.

``account_id`` stays the NAVER wire parameter and never names this identity.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.account_models import MarketplaceAccount, SellerEntity
from app.connect.smartstore.models import MarketplaceConnection
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError, PolicyBlockedError
from app.core.safe_payload import safe_payload
from app.db.database import Database


class AccountBinding(StrEnum):
    """Whether a canonical account may scope registration state now."""

    BOUND = "BOUND"
    # No canonical account of that id in that marketplace.
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    # Its connection holds no committed binding.
    NOT_BOUND = "NOT_BOUND"
    # Its connection's committed binding names another provider identity (ACCOUNT_IDENTITY §4).
    MISMATCHED = "MISMATCHED"


@dataclass(frozen=True)
class MarketplaceAccountRecord:
    marketplace_account_id: str
    seller_entity_id: str
    marketplace_key: str


def binding_state(
    session: Session, marketplace_key: str, marketplace_account_id: str
) -> AccountBinding:
    """The binding of one canonical account, read in the caller's session."""
    account = session.get(MarketplaceAccount, marketplace_account_id)
    if account is None or account.marketplace_key != marketplace_key:
        return AccountBinding.UNKNOWN_ACCOUNT
    connection = session.get(MarketplaceConnection, marketplace_key)
    if connection is None or connection.provider_account_uid is None:
        return AccountBinding.NOT_BOUND
    if connection.provider_account_uid != account.provider_account_uid:
        return AccountBinding.MISMATCHED
    return AccountBinding.BOUND


def require_bound(session: Session, marketplace_key: str, marketplace_account_id: str) -> None:
    """Refuse unless the canonical account is bound to its committed provider identity."""
    state = binding_state(session, marketplace_key, marketplace_account_id)
    if state is not AccountBinding.BOUND:
        raise PolicyBlockedError(
            "MARKETPLACE_ACCOUNT_NOT_BOUND",
            "registration state is scoped only by a bound canonical marketplace account",
            details={"binding": state.value},
        )


class MarketplaceAccountStore:
    """The only production writer of ``seller_entities`` and ``marketplace_accounts``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def create_seller_entity(self, *, created_by: str, correlation_id: str) -> str:
        _require_text(created_by=created_by, correlation_id=correlation_id)
        with self._db.write() as session:
            row = SellerEntity(
                seller_entity_id=str(uuid.uuid4()),
                created_by=created_by,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.SELLER_ENTITY_RECORDED,
                    action="CREATE_SELLER_ENTITY",
                    actor=created_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=f"seller:{row.seller_entity_id}",
                    details=safe_payload(seller_entity_id=row.seller_entity_id),
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return row.seller_entity_id

    def establish(
        self,
        marketplace_key: str,
        seller_entity_id: str,
        *,
        established_by: str,
        correlation_id: str,
    ) -> MarketplaceAccountRecord:
        """The canonical account of the committed binding of ``marketplace_key``.

        Asking again for the same bound provider identity returns the same account: it is never
        re-minted under a second id. Without a complete committed binding nothing is written.
        """
        _require_text(
            marketplace_key=marketplace_key,
            established_by=established_by,
            correlation_id=correlation_id,
        )
        with self._db.write() as session:
            connection = session.get(MarketplaceConnection, marketplace_key)
            if connection is None or connection.provider_account_uid is None:
                raise PolicyBlockedError(
                    "MARKETPLACE_ACCOUNT_NOT_BOUND",
                    "a canonical account is established only from a committed binding",
                )
            existing = session.scalar(
                select(MarketplaceAccount).where(
                    MarketplaceAccount.marketplace_key == marketplace_key,
                    MarketplaceAccount.provider_account_uid == connection.provider_account_uid,
                )
            )
            if existing is not None:
                return _record(existing)
            if session.get(SellerEntity, seller_entity_id) is None:
                raise NotFoundError("SELLER_ENTITY_NOT_FOUND", "the seller entity does not exist")
            row = MarketplaceAccount(
                marketplace_account_id=f"mpa-{uuid.uuid4().hex}",
                seller_entity_id=seller_entity_id,
                marketplace_key=marketplace_key,
                provider_account_uid=connection.provider_account_uid,
                established_by=established_by,
                correlation_id=correlation_id,
                established_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.MARKETPLACE_ACCOUNT_ESTABLISHED,
                    action="ESTABLISH_MARKETPLACE_ACCOUNT",
                    actor=established_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=f"marketplace-account:{row.marketplace_account_id}",
                    details=safe_payload(
                        marketplace_key=marketplace_key,
                        marketplace_account_id=row.marketplace_account_id,
                        seller_entity_id=seller_entity_id,
                    ),
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _record(row)

    def account(self, marketplace_account_id: str) -> MarketplaceAccountRecord | None:
        with self._reading() as session:
            row = session.get(MarketplaceAccount, marketplace_account_id)
            return None if row is None else _record(row)

    def binding(self, marketplace_key: str, marketplace_account_id: str) -> AccountBinding:
        with self._reading() as session:
            return binding_state(session, marketplace_key, marketplace_account_id)

    @contextmanager
    def _reading(self) -> Iterator[Session]:
        with self._db.read() as session:
            yield session


def _record(row: MarketplaceAccount) -> MarketplaceAccountRecord:
    return MarketplaceAccountRecord(
        marketplace_account_id=row.marketplace_account_id,
        seller_entity_id=row.seller_entity_id,
        marketplace_key=row.marketplace_key,
    )


def _require_text(**values: str) -> None:
    empty = sorted(name for name, value in values.items() if not value or not value.strip())
    if empty:
        raise InputValidationError("ACCOUNT_VALUE_MISSING", f"required: {', '.join(empty)}")
