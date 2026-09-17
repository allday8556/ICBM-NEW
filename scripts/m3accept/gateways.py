"""The campaign's hold on every request the production path makes (ruling 5711123764 §4).

Two thin wrappers, and neither of them collects anything:

* ``LedgeredCollectionGateway`` sits in front of the production collection gateway. It does not
  check targets and does not send: it hands the canonical transport a budget whose ``reserve``
  first spends the run's own in-memory allowance and then takes a durable campaign reservation.
  The canonical transport calls ``reserve`` after its own target check and before it sends, so a
  target it refuses spends nothing, and a request the campaign refuses is never sent. After every
  call it verifies that a reservation was actually taken — a transport that sent without reserving
  is a defect, and the campaign stops on it rather than trusting it.

* ``LedgeredConnectGateway`` sits directly in front of the M1 connection owner's gateway, and it
  is the one place CONNECT traffic is accounted for. Each session-proof read is reserved in the
  ledger immediately before it is delegated, so the ledger records the proof reads that were
  actually sent — one pair for an ordinary attempt, another for a job retry — and a read over the
  ceiling is refused before it reaches the transport. Nothing is reserved in advance of need. A
  login is refused outright and recorded; there is no recovery path in a campaign, only a stop.

``CampaignSessions`` hands the production collection path the M1 owner itself. It counts nothing
and decides nothing about the budget: it only refuses the operator-initiated form of the call,
because a campaign never starts a login.
"""

from dataclasses import dataclass

from app.collect.collection import CollectionGateway, SessionProvider
from app.core.errors import AuthError, PolicyBlockedError
from integrations.suppliers.base import (
    Credentials,
    ProbeResponse,
    RequestKind,
    SupplierDefinition,
    SupplierGateway,
)
from integrations.suppliers.collection import (
    CollectionProfile,
    DocumentView,
    ImageResponse,
    ReadKind,
)
from integrations.suppliers.transport.collection import RequestBudget
from scripts.m3accept.ledger import CampaignLedger
from scripts.m3accept.manifest import RequestClass

_COLLECT_CLASS = {
    ReadKind.PRODUCT_READ: RequestClass.PRODUCT_READ,
    ReadKind.IMAGE_REQUEST: RequestClass.IMAGE_REQUEST,
    ReadKind.POLICY_READ: RequestClass.POLICY_READ,
}
_CONNECT_CLASS = {
    RequestKind.CONTROL_READ: RequestClass.CONNECT_CONTROL_READ,
    RequestKind.PROTECTED_READ: RequestClass.CONNECT_PROTECTED_READ,
}


class UnreservedRequest(PolicyBlockedError):
    """A transport returned from a request without having reserved it. The campaign stops."""


@dataclass
class _CampaignBudget:
    """The run's own allowance first, then the campaign's durable reservation."""

    run: RequestBudget
    ledger: CampaignLedger
    pass_id: str
    reserved: int = 0

    def reserve(self, kind: ReadKind, subject: str) -> None:
        self.run.reserve(kind, subject)
        self.ledger.reserve(self.pass_id, _COLLECT_CLASS[kind], subject)
        self.reserved += 1


class LedgeredCollectionGateway:
    def __init__(self, inner: CollectionGateway, ledger: CampaignLedger, pass_id: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._pass = pass_id

    def _budget(self, run: RequestBudget) -> _CampaignBudget:
        return _CampaignBudget(run=run, ledger=self._ledger, pass_id=self._pass)

    @staticmethod
    def _verify(budget: _CampaignBudget, kind: ReadKind) -> None:
        if budget.reserved != 1:
            raise UnreservedRequest(
                "M3_ACCEPT_UNRESERVED_REQUEST",
                f"the transport completed a {kind.value} without a campaign reservation",
            )

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        campaign = self._budget(budget)
        document = self._inner.read_document(
            profile, url, kind=kind, budget=campaign, session=session
        )
        self._verify(campaign, kind)
        return document

    def read_image(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        budget: RequestBudget,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> ImageResponse:
        campaign = self._budget(budget)
        response = self._inner.read_image(
            profile,
            url,
            budget=campaign,
            etag=etag,
            last_modified=last_modified,
            max_bytes=max_bytes,
        )
        self._verify(campaign, ReadKind.IMAGE_REQUEST)
        return response


class LedgeredConnectGateway:
    """The M1 owner's gateway, with every read reserved and every login refused."""

    def __init__(self, inner: SupplierGateway, ledger: CampaignLedger, pass_id: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self._pass = pass_id

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        request = _CONNECT_CLASS.get(kind)
        if request is None:
            self._ledger.refuse(self._pass, RequestClass.CONNECT_AUTHENTICATE, kind.value)
            raise AuthError("M3_ACCEPT_AUTH_REFUSED", "authentication is never part of a campaign")
        self._ledger.reserve(self._pass, request, definition.probe.target)
        return self._inner.fetch(definition, kind=kind, session=session)

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        self._ledger.refuse(self._pass, RequestClass.CONNECT_AUTHENTICATE, "LOGIN_NOT_IN_CAMPAIGN")
        raise AuthError(
            "M3_ACCEPT_LOGIN_REFUSED",
            "the accepted M1 session must already be usable; a campaign never logs in",
        )


class CampaignSessions:
    """The production M1 connection owner, as the collection path's session provider.

    Its proof reads are accounted for where they are sent — in ``LedgeredConnectGateway`` — and
    nowhere else.
    """

    def __init__(self, owner: SessionProvider) -> None:
        self._owner = owner

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        if operator_initiated:
            # An operator-initiated collection may log in. A campaign never does.
            raise PolicyBlockedError(
                "M3_ACCEPT_OPERATOR_LOGIN_REFUSED", "a campaign never starts a login"
            )
        return self._owner.collection_session(supplier_key)
