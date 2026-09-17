"""What a campaign may know about the accepted M1 connection without asking the supplier anything.

The M1 connection is established in the campaign's own data directory by an ordinary CONNECT
operation, outside ``m3-accept-01`` and before it is armed. The campaign never logs in and never
automates that step. What it checks here is only local: that the connection owner, reading its own
state and its own stored session, reports a connection it could hand a session out for. Whether
that session is still live is a different question, answered inside a pass by the budgeted
CONTROL/PROTECTED proof reads — never here.
"""

from app.container import Container
from app.core.errors import PolicyBlockedError
from integrations.suppliers.base import (
    Credentials,
    ProbeResponse,
    RequestKind,
    SupplierDefinition,
)
from integrations.suppliers.collection import (
    CollectionProfile,
    DocumentView,
    ImageResponse,
    ReadKind,
)
from integrations.suppliers.transport.collection import RequestBudget


class LocalCheckOnly(PolicyBlockedError):
    """A local-only check reached for a transport. Nothing was sent."""


class NoTraffic:
    """The only transport a local check is composed with: every request is refused, unsent.

    Composing the application for a local check still needs a CONNECT and a collection gateway.
    This one makes it structurally impossible for that check to become a request.
    """

    def _refuse(self) -> LocalCheckOnly:
        return LocalCheckOnly("M3_ACCEPT_LOCAL_CHECK_ONLY", "a local check never sends a request")

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        raise self._refuse()

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        raise self._refuse()

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        raise self._refuse()

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
        raise self._refuse()


def m1_session_problems(container: Container, supplier_key: str) -> list[str]:
    """Whether the M1 owner holds a locally usable connection and session. No request is made.

    The owner's own summary answers it: a READY connection reports a VERIFIED session only when its
    stored session exists and decrypts, and demotes itself when it does not. The harness neither
    decodes nor copies the session — that material stays with its owner.
    """
    summary = container.connect.supplier_connection(supplier_key)
    if summary.auth_state != "AUTHENTICATED" or summary.session_state != "VERIFIED":
        return [
            f"the M1 connection in this data directory is {summary.state}; establish it by an "
            "ordinary CONNECT operation first — a campaign never logs in or recovers"
        ]
    return []
