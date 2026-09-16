"""CONNECT stage owner (Issue #7 and its architect addenda, ADR-0007).

Every supplier goes through one common lifecycle:

    reusable session first → unauthenticated control + authenticated read of the same probe target
    → only when the session is missing or expired: one single-flight (re-)authentication
    → the same proof again → READY only when the proof holds
    → consecutive rejected logins reach the supplier's auth_retry_limit → PAUSED until an operator
      resumes it

Supplier definitions contribute site knowledge only; every request goes through the
policy-enforcing ``SupplierGateway``. Startup makes no supplier request (lazy connection). No
product data is read or written here.
"""

import logging
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.contracts import (
    CapabilityReport,
    MarketplaceConnectionState,
    MarketplaceConnectionSummary,
    StoredLoginView,
    SupplierConnectionSummary,
)
from app.connect.credentials import SupplierCredentialStore
from app.connect.marketplace.service import CAPABILITY_MARKETPLACES
from app.connect.models import SupplierConnection
from app.connect.proof import ProtectedReadProof, ReadClassification, ReadOutcome, classify
from app.connect.sessions import SupplierSessionStore
from app.connect.singleflight import SingleFlightAuth
from app.connect.state import (
    CapabilityStatus,
    ConnectionState,
    capability_status,
    check_transition,
    startup_state,
)
from app.core.clock import Clock
from app.core.errors import (
    AppError,
    AuthError,
    ErrorClass,
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
    RateLimitedError,
    TransientError,
    UnknownOutcomeError,
)
from app.core.safe_payload import safe_payload
from app.db.database import Database
from app.jobs.policy import RetryPolicy
from app.jobs.records import JobRecord
from app.jobs.registry import JobContext, JobDefinition
from app.jobs.service import JobService
from integrations.marketplaces.identity import MarketplaceIdentity
from integrations.suppliers.base import (
    Credentials,
    RequestKind,
    SupplierDefinition,
    SupplierGateway,
)

logger = logging.getLogger("icbm.connect")

CONNECTION_TEST_JOB = "connect.supplier_test"
# One attempt per operator request: a retry could submit the real login again, so a failed test
# is re-run only by the operator (Issue #7 comment 5653567880: minimise real-account traffic).
CONNECTION_TEST_POLICY = RetryPolicy(max_attempts=1, base_delay_s=15.0, max_delay_s=15.0)
SYSTEM_ACTOR = "system:connect"
OPERATOR_TEST = "operator_test"
AUTO_CONNECT = "auto_connect"
_MAX_SECRET_LENGTH = 500
# The UI's stored-password indicators (display only). Exactly these are refused as a password;
# a real password that merely contains "•" is accepted (PR #9 comment 5654916026 §4).
_PASSWORD_SENTINELS = frozenset({"••••••••", "•••••••• · 저장됨"})

S = ConnectionState
Mutation = Callable[[Session, SupplierConnection], None]


def _target_ref(supplier_key: str) -> str:
    return f"supplier:{supplier_key}"


class ConnectService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        jobs: JobService,
        credentials: SupplierCredentialStore,
        sessions: SupplierSessionStore,
        gateway: SupplierGateway,
        suppliers: Sequence[SupplierDefinition],
        marketplaces: Sequence[MarketplaceIdentity],
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._jobs = jobs
        self._credentials = credentials
        self._sessions = sessions
        self._gateway = gateway
        self._definitions: dict[str, SupplierDefinition] = {}
        for definition in suppliers:
            key = definition.profile.supplier_key
            if key in self._definitions:
                raise ValueError(f"duplicate supplier definition: {key}")
            self._definitions[key] = definition
        self._marketplaces = tuple(marketplaces)
        self._flights = SingleFlightAuth()

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=CONNECTION_TEST_JOB,
            handler=self._run_connection_test,
            description="Verify a supplier connection by the protected-read proof.",
            idempotent=True,
            retry_policy=CONNECTION_TEST_POLICY,
        )

    # ------------------------------------------------------------------ reads

    def _definition(self, supplier_key: str) -> SupplierDefinition:
        try:
            return self._definitions[supplier_key]
        except KeyError:
            raise NotFoundError("SUPPLIER_UNKNOWN", f"no supplier {supplier_key!r}") from None

    @staticmethod
    def _row(session: Session, supplier_key: str) -> SupplierConnection | None:
        return session.scalars(
            select(SupplierConnection).where(SupplierConnection.supplier_key == supplier_key)
        ).first()

    def _summary(
        self, definition: SupplierDefinition, row: SupplierConnection | None
    ) -> SupplierConnectionSummary:
        key = definition.profile.supplier_key
        stored = self._credentials.stored(key)
        state = S(row.state) if row else S.DISCONNECTED
        # "Usable" means the persisted session exists and decrypts; an unreadable blob is
        # discarded by this read, so it is never reported as STORED or as backing READY.
        usable = self._session_usable(key)
        if state is S.READY and not usable:
            # A READY row without a usable session never reports READY; heal it when possible.
            self._demote_unbacked_ready(definition)
            state = S.DISCONNECTED
        if state is S.PAUSED:
            auth_state = "PAUSED"
        elif state is S.READY:
            auth_state = "AUTHENTICATED"
        elif state in (S.AUTH_EXPIRED, S.REAUTHENTICATING):
            auth_state = "AUTH_EXPIRED"
        elif state in (S.SESSION_CHECK, S.AUTHENTICATING, S.VERIFYING):
            auth_state = "AUTHENTICATING"
        else:
            auth_state = "CREDENTIALS_STORED" if stored else "NOT_CONFIGURED"
        if state is S.READY:
            session_state = "VERIFIED"
        elif state in (S.SESSION_CHECK, S.VERIFYING):
            session_state = "CHECKING"
        elif state in (S.AUTH_EXPIRED, S.REAUTHENTICATING):
            session_state = "EXPIRED"
        else:
            session_state = "STORED" if usable else "NONE"
        return SupplierConnectionSummary(
            connection_id=row.connection_id if row else None,
            supplier_key=key,
            display_name=definition.profile.display_name,
            base_url=definition.profile.base_url,
            auth_required=definition.profile.auth_required,
            state=state,
            auth_state=auth_state,
            session_state=session_state,
            profile_state="NOT_ANALYZED",
            capability_status=capability_status(state, credentials_stored=stored),
            credentials_stored=stored,
            auto_connect=row.auto_connect if row else True,
            last_verified_at=row.last_verified_at if row else None,
            consecutive_auth_failures=row.consecutive_auth_failures if row else 0,
            auth_retry_limit=definition.profile.request_policy.auth_retry_limit,
            real_login_attempts=row.real_login_attempts if row else 0,
            session_reuse_count=row.session_reuse_count if row else 0,
            reauth_count=row.reauth_count if row else 0,
            last_login_attempt_at=row.last_login_attempt_at if row else None,
            last_error_class=row.last_error_class if row else None,
            last_error_code=row.last_error_code if row else None,
        )

    def supplier_connection(self, supplier_key: str) -> SupplierConnectionSummary:
        definition = self._definition(supplier_key)
        with self._db.read() as session:
            return self._summary(definition, self._row(session, supplier_key))

    def supplier_connections(self) -> list[SupplierConnectionSummary]:
        with self._db.read() as session:
            return [
                self._summary(definition, self._row(session, key))
                for key, definition in sorted(self._definitions.items())
            ]

    def capabilities(self) -> list[CapabilityReport]:
        """Supplier capabilities for readiness, derived from the state machine only."""
        try:
            summaries = self.supplier_connections()
        except Exception as exc:  # readiness must report, not raise
            return [
                CapabilityReport(
                    key=_target_ref(key),
                    status=CapabilityStatus.DEGRADED,
                    detail=f"connection state unavailable ({type(exc).__name__})",
                )
                for key in sorted(self._definitions)
            ]
        return [
            CapabilityReport(
                key=_target_ref(s.supplier_key),
                status=s.capability_status,
                detail=f"state={s.state}",
            )
            for s in summaries
        ]

    def connected_supplier_count(self) -> int:
        return sum(1 for s in self.supplier_connections() if s.state is S.READY)

    def marketplace_connections(self) -> list[MarketplaceConnectionSummary]:
        """Marketplaces without an adopted capability contract. A marketplace that has one
        (SmartStore in M2) takes its connection truth from the capability read API, so it is never
        reported here as a hard-coded NOT_CONNECTED (CAPABILITY_MAPPING §14.11)."""
        return [
            MarketplaceConnectionSummary(
                marketplace_key=m.key, connection_state=MarketplaceConnectionState.NOT_CONNECTED
            )
            for m in self._marketplaces
            if m.key not in CAPABILITY_MARKETPLACES
        ]

    def connected_marketplace_count(self) -> int:
        return sum(
            1
            for c in self.marketplace_connections()
            if c.connection_state is not MarketplaceConnectionState.NOT_CONNECTED
        )

    # ------------------------------------------------------------------ persistence helpers

    def _new_row(self, definition: SupplierDefinition) -> SupplierConnection:
        now = self._clock.now()
        return SupplierConnection(
            connection_id=str(uuid.uuid4()),
            supplier_key=definition.profile.supplier_key,
            base_url=definition.profile.base_url,
            auth_required=definition.profile.auth_required,
            state=S.DISCONNECTED,
            auto_connect=True,
            consecutive_auth_failures=0,
            real_login_attempts=0,
            session_reuse_count=0,
            reauth_count=0,
            created_at=now,
            updated_at=now,
        )

    def _move(
        self, row: SupplierConnection, target: ConnectionState, *, trigger: str | None
    ) -> None:
        source = S(row.state)
        if source is not target:
            check_transition(source, target)
            row.state = target
        row.updated_at = self._clock.now()
        logger.info(
            "supplier.connection.state",
            extra=safe_payload(
                supplier_key=row.supplier_key,
                connection_id=row.connection_id,
                state_from=source,
                state_to=target,
                trigger=trigger,
            ),
        )

    def _update(self, supplier_key: str, mutate: Mutation) -> None:
        with self._db.write() as session:
            row = self._row(session, supplier_key)
            if row is None:
                raise InputValidationError(
                    "SUPPLIER_CREDENTIALS_MISSING", "save the supplier login before connecting"
                )
            mutate(session, row)

    def _state(self, supplier_key: str) -> ConnectionState | None:
        with self._db.read() as session:
            row = self._row(session, supplier_key)
            return S(row.state) if row else None

    def _ensure_row(self, definition: SupplierDefinition) -> None:
        """Credentials live in the OS secret store, which outlives a data directory; a fresh
        directory adopts them as a new, unverified connection."""
        key = definition.profile.supplier_key
        if not self._credentials.stored(key):
            return
        with self._db.write() as session:
            if self._row(session, key) is None:
                session.add(self._new_row(definition))

    def _audit_entry(
        self,
        session: Session,
        row: SupplierConnection,
        event_type: AuditEventType,
        *,
        action: str,
        outcome: AuditOutcome = AuditOutcome.RECORDED,
        actor: str = SYSTEM_ACTOR,
        reason_code: str | None = None,
        **details: object,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=event_type,
                action=action,
                actor=actor,
                outcome=outcome,
                target_ref=_target_ref(row.supplier_key),
                reason_code=reason_code,
                details=safe_payload(
                    supplier_key=row.supplier_key, connection_id=row.connection_id, **details
                ),
            ),
            session=session,
        )

    # ------------------------------------------------------------------ operator actions

    def stored_login(self, supplier_key: str) -> StoredLoginView:
        """The saved login ID (read from the OS secret store on demand) and whether a password is
        stored. The password itself is never returned."""
        self._definition(supplier_key)
        return StoredLoginView(
            username=self._credentials.username(supplier_key),
            password_stored=self._credentials.stored(supplier_key),
        )

    def save_credentials(
        self, supplier_key: str, *, username: str, password: str | None, actor: str
    ) -> SupplierConnectionSummary:
        """Replace the stored login; ``password=None`` keeps the stored password.

        Serialised with the supplier's connection flight (PR #9 review 5191372031): a login still
        using the old credentials completes first, and its session is discarded here, so an
        old-account session can never be the READY connection after new credentials are saved.
        The replacement itself fails closed at every step (see ``_replace_credentials``).
        """
        definition = self._definition(supplier_key)
        username = username.strip()
        password = password or None
        if not username:
            raise InputValidationError("SUPPLIER_CREDENTIALS_INVALID", "an ID is required")
        if password is not None and password in _PASSWORD_SENTINELS:
            raise InputValidationError(
                "SUPPLIER_PASSWORD_MASK_REJECTED",
                "the stored-password indicator is not a password; choose 비밀번호 변경",
            )
        if len(username) > _MAX_SECRET_LENGTH or len(password or "") > _MAX_SECRET_LENGTH:
            raise InputValidationError("SUPPLIER_CREDENTIALS_INVALID", "credential is too long")
        with self._flights.lifecycle(supplier_key):
            current = self._credentials.load(supplier_key)
            if password is None:
                if current is None:
                    raise InputValidationError(
                        "SUPPLIER_PASSWORD_REQUIRED", "a password is required for the first save"
                    )
                if current.username == username:
                    return self.supplier_connection(supplier_key)  # nothing changed
                password = current.password
            self._replace_credentials(
                definition, Credentials(username=username, password=password), actor=actor
            )
        return self.supplier_connection(supplier_key)

    def _replace_credentials(
        self, definition: SupplierDefinition, credentials: Credentials, *, actor: str
    ) -> None:
        """Fail-closed replacement (PR #9 comments 5654839475, 5654916026).

        1. invalidate the old session and any READY state;
        2. write the new login as one secret-store record;
        3. record the update (counters and audit).

        Whatever fails, the worst state is "no reusable session; re-authentication required":
        new or partial credentials can never coexist with a session that could prove READY.
        """
        supplier_key = definition.profile.supplier_key
        state_from = self._invalidate_session(definition)
        self._credentials.save(supplier_key, credentials)
        self._record_credentials_update(definition, actor=actor, state_from=state_from)

    def _invalidate_session(self, definition: SupplierDefinition) -> ConnectionState:
        """Step 1, itself fail-closed (re-audit 5655076870): the canonical state leaves READY
        before the session is deleted. If the DB write fails, nothing has changed; if deleting
        the session then fails, nothing reports READY and the old login is still intact."""
        state_from = self._demote_for_replacement(definition)
        self._sessions.clear(definition.profile.supplier_key)
        return state_from

    def _demote_for_replacement(self, definition: SupplierDefinition) -> ConnectionState:
        supplier_key = definition.profile.supplier_key
        with self._db.write() as session:
            row = self._row(session, supplier_key)
            if row is None:
                row = self._new_row(definition)
                session.add(row)
                session.flush()
            source = S(row.state)
            if source in (S.READY, S.AUTH_EXPIRED, S.DEGRADED):
                self._move(row, S.DISCONNECTED, trigger="credentials_replacing")
            row.updated_at = self._clock.now()
        return source

    def _session_usable(self, supplier_key: str) -> bool:
        """The persisted session exists and decrypts (an unreadable blob is discarded)."""
        return self._sessions.load(supplier_key) is not None

    def _demote_unbacked_ready(self, definition: SupplierDefinition) -> None:
        """Self-healing invariant (comment 5655122594): READY is reported only while its
        persisted session is usable. Best effort and non-blocking — a flight in progress sets
        the state itself — and never needs the lock to stop reporting READY."""
        key = definition.profile.supplier_key
        lock = self._flights.lifecycle(key)
        if not lock.acquire(blocking=False):
            return
        try:
            with self._db.write() as session:
                row = self._row(session, key)
                if row is None or S(row.state) is not S.READY or self._session_usable(key):
                    return
                self._move(row, S.DISCONNECTED, trigger="session_unusable")
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_CONNECTION_DEMOTED,
                    action="SELF_HEAL",
                    reason_code="SESSION_MISSING_OR_UNREADABLE",
                    state_from=S.READY,
                    state_to=S.DISCONNECTED,
                )
        finally:
            lock.release()

    def _record_credentials_update(
        self, definition: SupplierDefinition, *, actor: str, state_from: ConnectionState
    ) -> None:
        """Step 3: counters and the audit record, once the new login is safely stored."""

        def mutate(session: Session, row: SupplierConnection) -> None:
            if S(row.state) is not S.PAUSED:
                row.consecutive_auth_failures = 0
            row.updated_at = self._clock.now()
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_CREDENTIALS_UPDATED,
                action="SAVE_CREDENTIALS",
                outcome=AuditOutcome.ALLOWED,
                actor=actor,
                credentials_stored=True,
                state_from=state_from,
                state_to=row.state,
            )

        self._update(definition.profile.supplier_key, mutate)

    def set_auto_connect(
        self, supplier_key: str, *, enabled: bool, actor: str
    ) -> SupplierConnectionSummary:
        self._definition(supplier_key)

        def mutate(session: Session, row: SupplierConnection) -> None:
            row.auto_connect = enabled
            row.updated_at = self._clock.now()
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_AUTO_CONNECT_CHANGED,
                action="SET_AUTO_CONNECT",
                outcome=AuditOutcome.ALLOWED,
                actor=actor,
                auto_connect=enabled,
            )

        with self._flights.lifecycle(supplier_key):
            self._update(supplier_key, mutate)
        return self.supplier_connection(supplier_key)

    def resume(self, supplier_key: str, *, actor: str) -> SupplierConnectionSummary:
        """Explicit, audited operator intervention: the only way out of PAUSED."""
        definition = self._definition(supplier_key)

        def mutate(session: Session, row: SupplierConnection) -> None:
            if S(row.state) is not S.PAUSED:
                raise InputValidationError("SUPPLIER_NOT_PAUSED", "supplier is not paused")
            failures = row.consecutive_auth_failures
            now = self._clock.now()
            self._move(row, S.DISCONNECTED, trigger="operator_resume")
            row.consecutive_auth_failures = 0
            row.paused_at = None
            row.last_error_class = None
            row.last_error_code = None
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_AUTH_RESUMED,
                action="RESUME_AUTH",
                outcome=AuditOutcome.ALLOWED,
                actor=actor,
                prior_state=S.PAUSED,
                consecutive_auth_failures=failures,
                auth_retry_limit=definition.profile.request_policy.auth_retry_limit,
                resumed_at=now,
            )

        with self._flights.lifecycle(supplier_key):
            self._update(supplier_key, mutate)
        return self.supplier_connection(supplier_key)

    def request_connection_test(self, supplier_key: str, *, actor: str) -> JobRecord:
        definition = self._definition(supplier_key)
        if not self._credentials.stored(supplier_key):
            raise InputValidationError(
                "SUPPLIER_CREDENTIALS_MISSING", "save the supplier login before testing"
            )
        with self._flights.lifecycle(supplier_key):
            self._ensure_row(definition)
        denied = False
        with self._db.write() as session:
            row = self._row(session, supplier_key)
            if row is None:
                raise InputValidationError(
                    "SUPPLIER_CREDENTIALS_MISSING", "save the supplier login before testing"
                )
            if S(row.state) is S.PAUSED:
                denied = True
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_CONNECTION_TEST_REQUESTED,
                    action="CONNECTION_TEST",
                    outcome=AuditOutcome.DENIED,
                    actor=actor,
                    reason_code="SUPPLIER_AUTH_PAUSED",
                    state_from=row.state,
                )
            else:
                job = self._jobs.enqueue(
                    CONNECTION_TEST_JOB,
                    target_ref=_target_ref(supplier_key),
                    payload={"supplier_key": supplier_key, "trigger": OPERATOR_TEST},
                    session=session,
                )
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_CONNECTION_TEST_REQUESTED,
                    action="CONNECTION_TEST",
                    outcome=AuditOutcome.ALLOWED,
                    actor=actor,
                    trigger=OPERATOR_TEST,
                )
        if denied:
            raise PolicyBlockedError(
                "SUPPLIER_AUTH_PAUSED",
                "automatic authentication is paused after repeated failures; resume it first",
            )
        self._jobs.notify_worker()
        return job

    # ------------------------------------------------------------------ lifecycle

    def normalize_on_startup(self) -> int:
        """A new process holds no proof: READY and interrupted steps restart as DISCONNECTED.

        This is a process boundary, not a live transition, so it bypasses the transition table.
        No supplier request is made (lazy connection).
        """
        changed = 0
        with self._db.write() as session:
            for row in session.scalars(select(SupplierConnection)).all():
                source = S(row.state)
                target = startup_state(source)
                if target is source:
                    continue
                row.state = target
                row.updated_at = self._clock.now()
                changed += 1
                logger.info(
                    "supplier.connection.state",
                    extra=safe_payload(
                        supplier_key=row.supplier_key,
                        connection_id=row.connection_id,
                        state_from=source,
                        state_to=target,
                        trigger="process_start",
                    ),
                )
        return changed

    def _run_connection_test(self, ctx: JobContext) -> None:
        self.verify(str(ctx.payload["supplier_key"]), trigger=OPERATOR_TEST, allow_login=True)

    def ensure_connected(self, supplier_key: str) -> ProtectedReadProof:
        """Automatic connection for a protected operation: session reuse first; a login only when
        the supplier's auto-connect setting allows it."""
        with self._db.read() as session:
            row = self._row(session, supplier_key)
            allow_login = bool(row and row.auto_connect)
        return self.verify(supplier_key, trigger=AUTO_CONNECT, allow_login=allow_login)

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        """The authenticated session payload for COLLECT reads (ADR-0010 §3, §11).

        The connection is established or reused exactly as for any protected operation: session
        reuse first, and at most one login through the single flight — when auto-connect allows
        it, or when the operator started the work, as for a connection test. COLLECT therefore
        has no login loop of its own. The payload is credential-equivalent: the caller hands it
        to the collection gateway in memory and never persists or logs it.
        """
        if operator_initiated:
            self.verify(supplier_key, trigger=OPERATOR_TEST, allow_login=True)
        else:
            self.ensure_connected(supplier_key)
        payload = self._sessions.load(supplier_key)
        if payload is None:
            raise AuthError(
                "SUPPLIER_SESSION_UNAVAILABLE", "the supplier connection holds no usable session"
            )
        return payload

    def stored_session(self, supplier_key: str) -> bytes | None:
        """The stored session payload, read without any supplier request and without proving it.

        It is credential-equivalent, so it is never a substitute for ``collection_session``: the
        only caller is local leak scanning, which must know the session values that a collected
        page could have echoed (Issue #52 comment 5689874555 §5).
        """
        self._definition(supplier_key)
        return self._sessions.load(supplier_key)

    def verify(self, supplier_key: str, *, trigger: str, allow_login: bool) -> ProtectedReadProof:
        """Establish or reuse the connection through the supplier's single flight.

        Concurrent callers share the one flight in progress and receive its outcome — the proof
        or the failure — so a burst never turns into several real logins (review 5191372031).
        """
        definition = self._definition(supplier_key)
        return self._flights.run(
            supplier_key,
            lambda: self._verify_in_flight(definition, trigger=trigger, allow_login=allow_login),
        )

    def _verify_in_flight(
        self, definition: SupplierDefinition, *, trigger: str, allow_login: bool
    ) -> ProtectedReadProof:
        key = definition.profile.supplier_key
        self._ensure_row(definition)
        state = self._state(key)
        if state is None:
            raise InputValidationError(
                "SUPPLIER_CREDENTIALS_MISSING", "save the supplier login before connecting"
            )
        if state is S.PAUSED:
            raise PolicyBlockedError(
                "SUPPLIER_AUTH_PAUSED", "authentication is paused; an operator must resume it"
            )
        return self._verify(definition, trigger=trigger, allow_login=allow_login)

    def _verify(
        self, definition: SupplierDefinition, *, trigger: str, allow_login: bool
    ) -> ProtectedReadProof:
        key = definition.profile.supplier_key
        expired = self._state(key) is S.AUTH_EXPIRED
        stored_session = self._sessions.load(key)
        if stored_session is not None:
            self._update(key, lambda _s, row: self._move(row, S.SESSION_CHECK, trigger=trigger))
            proof = self._prove(definition, stored_session)
            if proof.proven:
                self._enter_ready(definition, proof, trigger=trigger, reused=True)
                return proof
            # The same target read with the stored session shows the login page: it expired.
            self._sessions.clear(key)
            self._update(key, self._expire(trigger))
            expired = True
        elif self._state(key) is S.READY:
            self._update(key, lambda _s, row: self._move(row, S.DISCONNECTED, trigger=trigger))

        if not allow_login:
            raise AuthError(
                "SUPPLIER_AUTO_CONNECT_DISABLED",
                "automatic connection is off for this supplier; run a manual connection test",
            )
        credentials = self._credentials.load(key)
        if credentials is None:
            raise AuthError("SUPPLIER_CREDENTIALS_MISSING", "no stored supplier login")
        new_session = self._authenticate(definition, credentials, expired=expired, trigger=trigger)
        self._update(key, lambda _s, row: self._move(row, S.VERIFYING, trigger=trigger))
        proof = self._prove(definition, new_session)
        if proof.proven:
            self._sessions.save(key, new_session)
            self._enter_ready(definition, proof, trigger=trigger, refreshed=expired)
            return proof
        # The login was accepted, yet the protected target still demands a login.
        raise self._auth_failure(
            definition, expired=expired, code="SUPPLIER_LOGIN_NOT_EFFECTIVE", trigger=trigger
        )

    def _expire(self, trigger: str) -> Mutation:
        def mutate(_session: Session, row: SupplierConnection) -> None:
            self._move(row, S.AUTH_EXPIRED, trigger=trigger)
            row.last_error_class = ErrorClass.AUTH
            row.last_error_code = "SUPPLIER_SESSION_EXPIRED"

        return mutate

    def _read(
        self, definition: SupplierDefinition, kind: RequestKind, session: bytes | None
    ) -> ReadOutcome:
        try:
            response = self._gateway.fetch(definition, kind=kind, session=session)
        except (TransientError, RateLimitedError) as exc:
            raise self._degrade(definition, exc) from exc
        return classify(definition.probe, response)

    def _prove(self, definition: SupplierDefinition, session: bytes) -> ProtectedReadProof:
        control = self._read(definition, RequestKind.CONTROL_READ, None)
        if control.classification is not ReadClassification.LOGIN_REQUIRED:
            raise self._degrade(
                definition,
                PolicyBlockedError(
                    "SUPPLIER_TARGET_NOT_AUTH_GATED",
                    "the unauthenticated control did not prove the target requires a login",
                ),
            )
        authenticated = self._read(definition, RequestKind.PROTECTED_READ, session)
        if authenticated.classification is ReadClassification.UNRECOGNIZED:
            raise self._degrade(
                definition,
                TransientError(
                    "SUPPLIER_PROTECTED_READ_UNRECOGNIZED",
                    "the protected read proved neither an authenticated nor a login page",
                ),
            )
        return ProtectedReadProof(definition.probe.target, control, authenticated)

    def _authenticate(
        self,
        definition: SupplierDefinition,
        credentials: Credentials,
        *,
        expired: bool,
        trigger: str,
    ) -> bytes:
        key = definition.profile.supplier_key

        def start(_session: Session, row: SupplierConnection) -> None:
            self._move(row, S.REAUTHENTICATING if expired else S.AUTHENTICATING, trigger=trigger)
            row.real_login_attempts += 1
            row.reauth_count += 1 if expired else 0
            row.last_login_attempt_at = self._clock.now()

        self._update(key, start)
        try:
            new_session = self._gateway.login(definition, credentials)
        except AuthError as exc:
            raise self._auth_failure(
                definition, expired=expired, code=exc.code, trigger=trigger
            ) from exc
        except (TransientError, RateLimitedError) as exc:
            raise self._degrade(definition, exc) from exc
        except Exception as exc:
            raise self._degrade(
                definition, UnknownOutcomeError("SUPPLIER_LOGIN_ERROR", type(exc).__name__)
            ) from exc

        def succeeded(session: Session, row: SupplierConnection) -> None:
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_AUTH_SUCCEEDED,
                action="AUTHENTICATE",
                trigger=trigger,
                real_login_attempts=row.real_login_attempts,
                reauth_count=row.reauth_count,
            )

        self._update(key, succeeded)
        return new_session

    def _auth_failure(
        self, definition: SupplierDefinition, *, expired: bool, code: str, trigger: str
    ) -> AuthError:
        limit = definition.profile.request_policy.auth_retry_limit
        paused = False

        def mutate(session: Session, row: SupplierConnection) -> None:
            nonlocal paused
            row.consecutive_auth_failures += 1
            row.last_error_class = ErrorClass.AUTH
            row.last_error_code = code
            paused = row.consecutive_auth_failures >= limit
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_AUTH_FAILED,
                action="AUTHENTICATE",
                reason_code=code,
                trigger=trigger,
                consecutive_auth_failures=row.consecutive_auth_failures,
                auth_retry_limit=limit,
            )
            if paused:
                self._move(row, S.PAUSED, trigger=trigger)
                row.paused_at = self._clock.now()
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_AUTH_PAUSED,
                    action="PAUSE_AUTH",
                    reason_code="AUTH_RETRY_LIMIT_REACHED",
                    consecutive_auth_failures=row.consecutive_auth_failures,
                    auth_retry_limit=limit,
                )
            else:
                self._move(row, S.AUTH_EXPIRED if expired else S.DISCONNECTED, trigger=trigger)

        self._update(definition.profile.supplier_key, mutate)
        if paused:
            return AuthError(
                "SUPPLIER_AUTH_PAUSED",
                f"{limit} consecutive login failures; automatic authentication is paused",
            )
        return AuthError(code, "the supplier did not accept the stored login")

    def _degrade(self, definition: SupplierDefinition, error: AppError) -> AppError:
        def mutate(session: Session, row: SupplierConnection) -> None:
            self._move(row, S.DEGRADED, trigger="failure")
            row.last_error_class = error.error_class
            row.last_error_code = error.code
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_CONNECTION_FAILED,
                action="VERIFY_CONNECTION",
                reason_code=error.code,
                error_class=error.error_class,
            )

        self._update(definition.profile.supplier_key, mutate)
        return error

    def _enter_ready(
        self,
        definition: SupplierDefinition,
        proof: ProtectedReadProof,
        *,
        trigger: str,
        reused: bool = False,
        refreshed: bool = False,
    ) -> None:
        def mutate(session: Session, row: SupplierConnection) -> None:
            self._move(row, S.READY, trigger=trigger)
            row.last_verified_at = self._clock.now()
            row.consecutive_auth_failures = 0
            row.last_error_class = None
            row.last_error_code = None
            if reused:
                row.session_reuse_count += 1
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_SESSION_REUSED,
                    action="REUSE_SESSION",
                    trigger=trigger,
                    session_reuse_count=row.session_reuse_count,
                )
            if refreshed:
                self._audit_entry(
                    session,
                    row,
                    AuditEventType.SUPPLIER_SESSION_REFRESHED,
                    action="REFRESH_SESSION",
                    trigger=trigger,
                    reauth_count=row.reauth_count,
                )
            self._audit_entry(
                session,
                row,
                AuditEventType.SUPPLIER_CONNECTION_VERIFIED,
                action="VERIFY_CONNECTION",
                trigger=trigger,
                target=proof.target,
                control_result=proof.control.classification,
                authenticated_result=proof.authenticated.classification,
                signals=sorted(set(proof.control.signals) | set(proof.authenticated.signals)),
            )

        self._update(definition.profile.supplier_key, mutate)
