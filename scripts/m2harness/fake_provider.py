"""The DRY-mode SmartStore stand-in (Issue #46 §2: the default mode is dry/local).

It sits behind the real caller and the budget gate, exactly where the live network transport
would, and answers like the adopted contracts (EM §6, §7), so the whole campaign runs with zero
provider requests: the real application, restarts, the crash boundary, the ledger and the evidence.
It checks the SELF token form and its bcrypt signature, and it is stateless across processes: an
issued token is a MAC under the scenario key, so a restarted process can use a bearer that an
earlier process committed.

Scenario values are generated fixtures. None is a real credential or account.
"""

import hashlib
import hmac
import json
import secrets
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from integrations.marketplaces.smartstore.registry import AUTH_MODE
from integrations.marketplaces.smartstore.signing import (
    GRANT_TYPE,
    TOKEN_FORM_FIELDS,
    client_secret_sign,
)
from scripts.m2harness.ledger import SELLER, TOKEN
from scripts.m2harness.transport import resolve_target

_BCRYPT_ALPHABET = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_TOKEN_PREFIX = "dry-bearer."


@dataclass(frozen=True)
class Scenario:
    client_id: str
    client_secret: str
    account_uid: str
    account_id: str
    mac_key: str
    # Scripted provider failures by step label (T1, A1, A1b, A2, T4a, T4b, T5, A3): HTTP status.
    failures: dict[str, int] = field(default_factory=dict)
    # A different account returned at a step, e.g. A3, to exercise a measured mismatch.
    account_uid_at: dict[str, str] = field(default_factory=dict)
    expires_in: int = 10800

    @classmethod
    def fixture(cls, **changes: Any) -> "Scenario":
        # A bcrypt salt, as the SmartStore client secret is: cost 4 keeps the fixture fast.
        salt = "".join(secrets.choice(_BCRYPT_ALPHABET) for _ in range(21))
        values: dict[str, Any] = {
            "client_id": f"dry-client-{secrets.token_hex(8)}",
            "client_secret": f"$2a$04${salt}{secrets.choice('.Oeu')}",
            "account_uid": f"dry-account-uid-{secrets.token_hex(8)}",
            "account_id": f"dry-account-id-{secrets.token_hex(6)}",
            "mac_key": secrets.token_hex(32),
        }
        values.update(changes)
        return cls(**values)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        return cls(**json.loads(path.read_text("utf-8")))


class FakeSmartStore(httpx.BaseTransport):
    """Answers one reserved request; ``label`` is the step the ledger reserved it as."""

    def __init__(self, scenario: Scenario, label: str) -> None:
        self._scenario = scenario
        self._label = label

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        endpoint = resolve_target(request)
        trace = {"traceId": f"dry-trace-{self._label}"}
        failure = self._scenario.failures.get(self._label)
        if failure is not None:
            return _json(failure, {"code": "GW.DRY_SCRIPTED_FAILURE", **trace})
        if endpoint == TOKEN:
            return self._token(request, trace)
        if endpoint == SELLER:
            return self._account(request, trace)
        return _json(404, {"code": "GW.DRY_NOT_FOUND", **trace})

    def _mac(self, value: str) -> str:
        key = bytes.fromhex(self._scenario.mac_key)
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _token(self, request: httpx.Request, trace: dict[str, str]) -> httpx.Response:
        scenario = self._scenario
        form = dict(urllib.parse.parse_qsl(request.content.decode("ascii")))
        timestamp = form.get("timestamp", "")
        valid = (
            set(form) == TOKEN_FORM_FIELDS
            and form["grant_type"] == GRANT_TYPE
            and form["type"] == AUTH_MODE
            and form["client_id"] == scenario.client_id
            and timestamp.isdigit()
            and hmac.compare_digest(
                form["client_secret_sign"],
                client_secret_sign(scenario.client_id, scenario.client_secret, int(timestamp)),
            )
        )
        if not valid:
            return _json(400, {"code": "GW.DRY_BAD_TOKEN_REQUEST", **trace})
        nonce = f"{self._label}.{timestamp}"
        token = f"{_TOKEN_PREFIX}{nonce}.{self._mac(nonce)}"
        body = {"access_token": token, "expires_in": scenario.expires_in, "token_type": "Bearer"}
        return _json(200, body | trace)

    def _account(self, request: httpx.Request, trace: dict[str, str]) -> httpx.Response:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        head, _, mac = token.rpartition(".")
        nonce = head.removeprefix(_TOKEN_PREFIX)
        if scheme != "Bearer" or not head.startswith(_TOKEN_PREFIX) or mac != self._mac(nonce):
            return _json(401, {"code": "GW.AUTHN", **trace})
        scenario = self._scenario
        uid = scenario.account_uid_at.get(self._label, scenario.account_uid)
        return _json(200, {"accountUid": uid, "accountId": scenario.account_id, **trace})


def _json(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)
