"""SmartStore CONNECT (M2 PR-A; ENDPOINT_MATRIX.md §5, §8; AUTH.md §14; ACCOUNT_IDENTITY.md).

The M2 network graph, in order:

    SMARTSTORE_AUTH_TOKEN
    -> durable session commit: a token candidate becomes the current session only once its bundle
       is committed under a new session generation, and the bundle is read back before use
    -> SMARTSTORE_SELLER_ACCOUNT with the bearer of that committed session
    -> identity comparison against the committed binding, or an explicit first binding
    -> AuthEvidence for the capability owner, which alone decides AUTH_READY

Token success never produces AUTH_READY, a successful read never binds an account, and a mismatch
never rebinds (ACCOUNT_IDENTITY §4, §5). Every provider call goes through the registry-gated
caller; this service holds no HTTP client, URL or path.

Reused M1 assets: the OS ``SecretStore``, the encrypted session store (``marketplace`` namespace),
``SingleFlightAuth`` for per-account serialization (AUTH §15), the audit log and safe payloads.
PR-A retries nothing and reissues no token on failure: retry budgets are policy-pending (ERRORS
§25 Q6), so every failure is recorded once and surfaced.

The proactive renewal margin is operational policy from configuration (AUTH §15); without it,
CONNECT refuses before any provider call.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.marketplace.attestation import (
    SELF_AUTH_MODE,
    SMARTSTORE_PROVIDER,
    ApplicationIdentity,
)
from app.connect.marketplace.capability import (
    AuthEvidence,
    ContractDecision,
    FailureEvidence,
    Finding,
    Generations,
    IdentityProof,
    WorkflowScope,
    freshness_allows,
)
from app.connect.marketplace.contracts import MarketplaceCapabilityView
from app.connect.marketplace.service import MarketplaceCapabilityService
from app.connect.sessions import SupplierSessionStore
from app.connect.singleflight import SingleFlightAuth
from app.connect.smartstore.credentials import ApplicationCredentialStore
from app.connect.smartstore.models import MarketplaceConnection
from app.core.clock import Clock
from app.core.errors import InputValidationError, PolicyBlockedError, UnknownOutcomeError
from app.core.safe_payload import safe_payload
from app.core.secrets import SecretStore
from app.db.database import Database
from integrations.marketplaces.smartstore.caller import (
    MARKETPLACE_KEY,
    AccountRequest,
    SellerAccount,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
    TokenGrant,
    TokenRequest,
)
from integrations.marketplaces.smartstore.registry import EndpointId
from integrations.marketplaces.smartstore.signing import (
    ApplicationCredentials,
    SignatureError,
    client_secret_sign,
)

KEY = MARKETPLACE_KEY
SYSTEM_ACTOR = "system:connect"
_TARGET = f"marketplace:{KEY}"
_SESSION_FORMAT = 1


@dataclass(frozen=True)
class CommittedSession:
    """The durable token bundle (AUTH §12). Only a bundle read back from the session store is a
    committed session; a token response in memory never is (§14)."""

    access_token: str = field(repr=False)
    token_type: str
    expires_in: int
    expires_at: datetime
    credential_generation: int
    session_generation: int
    committed_at: datetime

    def encode(self) -> bytes:
        return json.dumps(
            {
                "v": _SESSION_FORMAT,
                "provider": SMARTSTORE_PROVIDER,
                "auth_mode": SELF_AUTH_MODE,
                "access_token": self.access_token,
                "token_type": self.token_type,
                "expires_in": self.expires_in,
                "expires_at": self.expires_at.isoformat(),
                "credential_generation": self.credential_generation,
                "session_generation": self.session_generation,
                "committed_at": self.committed_at.isoformat(),
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes) -> "CommittedSession | None":
        """A complete, well-formed bundle, or None: a partial bundle is never a session."""
        try:
            record = json.loads(payload)
            if not isinstance(record, dict) or (
                record.get("v"),
                record.get("provider"),
                record.get("auth_mode"),
            ) != (_SESSION_FORMAT, SMARTSTORE_PROVIDER, SELF_AUTH_MODE):
                return None
            token, token_type = record["access_token"], record["token_type"]
            expires_in = record["expires_in"]
            credential_generation = record["credential_generation"]
            session_generation = record["session_generation"]
            expires_at = datetime.fromisoformat(record["expires_at"])
            committed_at = datetime.fromisoformat(record["committed_at"])
        except (ValueError, KeyError, TypeError):
            return None
        numbers = (expires_in, credential_generation, session_generation)
        if not (
            isinstance(token, str)
            and token
            and isinstance(token_type, str)
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in numbers)
            and expires_at.tzinfo is not None
            and committed_at.tzinfo is not None
        ):
            return None
        return cls(
            access_token=token,
            token_type=token_type,
            expires_in=expires_in,
            expires_at=expires_at,
            credential_generation=credential_generation,
            session_generation=session_generation,
            committed_at=committed_at,
        )


@dataclass(frozen=True)
class ConnectResult:
    """What one CONNECT pass established. The observed identity is returned to the operator's
    own flow (first binding needs it confirmed); it is never logged or audited."""

    capability: MarketplaceCapabilityView
    bound: bool
    observed_account_uid: str = field(repr=False)
    observed_account_id: str | None = field(repr=False)
    credential_generation: int
    session_generation: int


class SmartStoreConnectService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        secrets: SecretStore,
        sessions: SupplierSessionStore,
        capability: MarketplaceCapabilityService,
        caller: SmartStoreEndpointCaller,
        # AUTH §15: a committed session is reused while more than this remains of its lifetime;
        # inside it a new token is issued. From validated configuration; None while unset.
        renewal_margin: timedelta | None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._credentials = ApplicationCredentialStore(secrets)
        self._sessions = sessions
        self._capability = capability
        self._caller = caller
        self._renewal_margin = renewal_margin
        self._flights = SingleFlightAuth()

    # ------------------------------------------------------------------ application identity

    def current_identity(self) -> ApplicationIdentity | None:
        """The configured application (``ApplicationIdentitySource``), taken from the committed
        credential bundle only — never typed in separately (PERMISSIONS_SCOPES §7.1)."""
        credentials = self._credentials.load(KEY)
        if credentials is None:
            return None
        return ApplicationIdentity(SMARTSTORE_PROVIDER, SELF_AUTH_MODE, credentials.client_id)

    # ------------------------------------------------------------------ credentials

    def credential_status(self) -> tuple[bool, int | None]:
        """Whether a credential bundle is committed, and its generation — never its content."""
        credentials = self._credentials.load(KEY)
        if credentials is None:
            return False, None
        return True, credentials.credential_generation

    def save_credentials(self, client_id: str, client_secret: str, *, actor: str) -> int:
        """Commit a replacement credential bundle as a new credential generation (AUTH §18).

        The previous session and every proof it carried end with the previous generation: the
        session is cleared and auth converges away from READY. Returns the new generation.
        """
        client_id = client_id.strip()
        if not client_id or not client_secret.strip():
            raise InputValidationError(
                "SMARTSTORE_CREDENTIALS_INCOMPLETE", "a client id and a client secret are required"
            )
        try:
            client_secret_sign(client_id, client_secret, 1)
        except SignatureError as exc:
            raise InputValidationError(
                "SMARTSTORE_CLIENT_SECRET_UNUSABLE", "the client secret cannot sign a token request"
            ) from exc
        with self._flights.lifecycle(KEY):
            previous = self._credentials.load(KEY)
            now = self._clock.now()
            with self._db.write() as session:
                row = self._row(session, now)
                generation = 1 + max(
                    row.credential_generation_hwm, previous.credential_generation if previous else 0
                )
                row.credential_generation_hwm = generation
                row.updated_at = now
            self._credentials.save(
                KEY, ApplicationCredentials(client_id, client_secret, generation)
            )
            self._sessions.clear(KEY)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.MARKETPLACE_CREDENTIALS_UPDATED,
                    action="SAVE_CREDENTIALS",
                    actor=actor,
                    outcome=AuditOutcome.ALLOWED,
                    target_ref=_TARGET,
                    details=safe_payload(marketplace_key=KEY, credential_generation=generation),
                )
            )
        self._capability.observe_auth(KEY, self._evidence(proof=None))
        return generation

    # ------------------------------------------------------------------ CONNECT

    def connect(self) -> ConnectResult:
        """One CONNECT pass, serialized per account: callers arriving during a pass share it."""
        return self._flights.run(KEY, self._connect)

    def bind_account(self, confirmed_account_uid: str, *, actor: str) -> ConnectResult:
        """The explicit first binding (ACCOUNT_IDENTITY §5).

        A first binding is new trust, so it waits for a CURRENT contract (CAPABILITY_MAPPING F5).
        It performs a fresh authenticated read and requires the operator's confirmation of the
        account that read returns. The binding unit and its audit record then commit in the same
        transaction as the capability transition, and only under the freshness decision taken
        inside that transaction: a refusal or any failure up to the commit leaves nothing bound.
        A bound account is never rebound here.
        """
        return self._flights.run(KEY, lambda: self._bind(confirmed_account_uid, actor))

    def _connect(self) -> ConnectResult:
        credentials = self._require_credentials()
        return self._prove(self._read_account(self._session_for(credentials)))

    def _bind(self, confirmed_account_uid: str, actor: str) -> ConnectResult:
        if self._binding() is not None:
            raise PolicyBlockedError(
                "SMARTSTORE_ACCOUNT_ALREADY_BOUND", "a bound account is never rebound automatically"
            )
        # Refuse early, before any provider call. The authoritative decision is taken again inside
        # the commit transaction below, on the state the binding commits with.
        freshness = self._capability.capability(KEY).contract_freshness
        if not freshness_allows(freshness, ContractDecision.PROMOTE_UNVERIFIED_CAPABILITY):
            raise PolicyBlockedError(
                "MARKETPLACE_CONTRACT_NOT_CURRENT",
                "a first binding is new trust and waits for a CURRENT contract",
            )
        credentials = self._require_credentials()
        account = self._read_account(self._session_for(credentials))
        if account.account_uid != confirmed_account_uid:
            raise InputValidationError(
                "SMARTSTORE_BINDING_NOT_CONFIRMED",
                "the current session reads a different account than the one confirmed",
            )
        evidence = AuthEvidence(
            binding_committed=True,
            expected_account_uid=account.account_uid,
            current=self._current_generations(credentials),
            proof=self._proof(account),
        )
        now = self._clock.now()

        def commit_binding(session: Session) -> None:
            row = self._row(session, now)
            if row.provider_account_uid is not None:
                raise PolicyBlockedError(
                    "SMARTSTORE_ACCOUNT_ALREADY_BOUND",
                    "a bound account is never rebound automatically",
                )
            row.provider_account_uid = account.account_uid
            row.provider_account_id = account.account_id
            row.bound_credential_generation = account.credential_generation
            row.bound_session_generation = account.session_generation
            row.bound_at = now
            row.bound_by = actor
            row.updated_at = now
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.MARKETPLACE_ACCOUNT_BOUND,
                    action="BIND_ACCOUNT",
                    actor=actor,
                    outcome=AuditOutcome.ALLOWED,
                    target_ref=_TARGET,
                    details=safe_payload(
                        marketplace_key=KEY,
                        credential_generation=account.credential_generation,
                        session_generation=account.session_generation,
                    ),
                ),
                session=session,
            )

        view = self._capability.observe_first_binding(KEY, evidence, commit_binding=commit_binding)
        return self._result(view, bound=True, account=account)

    # ------------------------------------------------------------------ steps

    def _require_renewal_margin(self) -> timedelta:
        if self._renewal_margin is None:
            raise PolicyBlockedError(
                "SMARTSTORE_RENEWAL_POLICY_NOT_CONFIGURED",
                "set ICBM_SMARTSTORE_RENEWAL_MARGIN_S; the renewal margin is configured policy",
            )
        return self._renewal_margin

    def _require_credentials(self) -> ApplicationCredentials:
        credentials = self._credentials.load(KEY)
        if credentials is None:
            raise InputValidationError(
                "SMARTSTORE_CREDENTIALS_NOT_CONFIGURED", "no SmartStore application is configured"
            )
        return credentials

    def _session_for(self, credentials: ApplicationCredentials) -> CommittedSession:
        """The current committed session, or a newly issued and committed one (AUTH §14, §17)."""
        margin = self._require_renewal_margin()
        committed = self._committed(credentials)
        if committed is not None and committed.expires_at - self._clock.now() > margin:
            return committed
        return self._commit(credentials, self._issue(credentials))

    def _issue(self, credentials: ApplicationCredentials) -> TokenGrant:
        timestamp_ms = int(self._clock.now().timestamp() * 1000)
        try:
            return self._caller.call(
                EndpointId.SMARTSTORE_AUTH_TOKEN, TokenRequest(credentials, timestamp_ms)
            )
        except SmartStoreCallError as exc:
            self._observe_failure(exc, session_generation=None)
            raise

    def _commit(self, credentials: ApplicationCredentials, grant: TokenGrant) -> CommittedSession:
        """Commit a token candidate as a new session generation, then read the commit back."""
        now = self._clock.now()
        with self._db.write() as session:
            row = self._row(session, now)
            generation = row.session_generation_hwm + 1
            row.session_generation_hwm = generation
            row.updated_at = now
        candidate = CommittedSession(
            access_token=grant.access_token,
            token_type=grant.token_type,
            expires_in=grant.expires_in,
            expires_at=now + timedelta(seconds=grant.expires_in),
            credential_generation=grant.credential_generation,
            session_generation=generation,
            committed_at=now,
        )
        self._sessions.save(KEY, candidate.encode())
        committed = self._committed(credentials)
        if committed is None or committed.session_generation != generation:
            raise UnknownOutcomeError(
                "SMARTSTORE_SESSION_COMMIT_NOT_PROVEN", "the token session could not be read back"
            )
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.MARKETPLACE_SESSION_COMMITTED,
                action="COMMIT_SESSION",
                actor=SYSTEM_ACTOR,
                outcome=AuditOutcome.RECORDED,
                target_ref=_TARGET,
                details=safe_payload(
                    marketplace_key=KEY,
                    credential_generation=committed.credential_generation,
                    session_generation=committed.session_generation,
                    session_expires_at=committed.expires_at,
                ),
            )
        )
        return committed

    def _read_account(self, committed: CommittedSession) -> SellerAccount:
        request = AccountRequest(
            committed.access_token, committed.credential_generation, committed.session_generation
        )
        try:
            return self._caller.call(EndpointId.SMARTSTORE_SELLER_ACCOUNT, request)
        except SmartStoreCallError as exc:
            self._observe_failure(exc, session_generation=committed.session_generation)
            raise

    def _proof(self, account: SellerAccount) -> IdentityProof:
        return IdentityProof(
            Generations(account.credential_generation, account.session_generation),
            account.account_uid,
            self._clock.now(),
        )

    def _prove(self, account: SellerAccount) -> ConnectResult:
        evidence = self._evidence(self._proof(account))
        view = self._capability.observe_auth(KEY, evidence)
        return self._result(view, bound=evidence.binding_committed, account=account)

    @staticmethod
    def _result(
        view: MarketplaceCapabilityView, *, bound: bool, account: SellerAccount
    ) -> ConnectResult:
        return ConnectResult(
            capability=view,
            bound=bound,
            observed_account_uid=account.account_uid,
            observed_account_id=account.account_id,
            credential_generation=account.credential_generation,
            session_generation=account.session_generation,
        )

    def _observe_failure(
        self, error: SmartStoreCallError, *, session_generation: int | None
    ) -> None:
        self._capability.observe_failure(
            KEY,
            FailureEvidence(
                scope=WorkflowScope.AUTHENTICATION,
                error_class=error.error_class,
                # Only a retryable class is recoverable; any other failure waits for a person with
                # its class unchanged (ERRORS §18, §21).
                finding=Finding.RECOVERABLE if error.retryable else Finding.UNRESOLVED,
                # remote_outcome is the mutation axis (ERRORS §2.2) and neither M2 endpoint
                # mutates; the transmission decision stays in the call evidence.
                remote_outcome=None,
                session_generation=session_generation,
                provider_code=error.classification.provider_code,
            ),
        )

    # ------------------------------------------------------------------ durable state

    def _evidence(self, proof: IdentityProof | None) -> AuthEvidence:
        """Evidence from durable state: the committed binding and the committed generations."""
        expected = self._binding()
        credentials = self._credentials.load(KEY)
        return AuthEvidence(
            binding_committed=expected is not None,
            expected_account_uid=expected,
            current=self._current_generations(credentials) if credentials is not None else None,
            proof=proof,
        )

    def _current_generations(self, credentials: ApplicationCredentials) -> Generations | None:
        """The generations of the session committed on disk now, never of one held in memory."""
        committed = self._committed(credentials)
        if committed is None:
            return None
        return Generations(committed.credential_generation, committed.session_generation)

    def _committed(self, credentials: ApplicationCredentials) -> CommittedSession | None:
        payload = self._sessions.load(KEY)
        committed = CommittedSession.decode(payload) if payload is not None else None
        if (
            committed is None
            or committed.credential_generation != credentials.credential_generation
        ):
            return None  # AUTH §18: a session of another credential generation is not current
        return committed

    def _binding(self) -> str | None:
        with self._db.read() as session:
            row = session.get(MarketplaceConnection, KEY)
            return row.provider_account_uid if row is not None else None

    @staticmethod
    def _row(session: Session, now: datetime) -> MarketplaceConnection:
        row = session.get(MarketplaceConnection, KEY)
        if row is None:
            row = MarketplaceConnection(
                marketplace_key=KEY,
                credential_generation_hwm=0,
                session_generation_hwm=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        return row
