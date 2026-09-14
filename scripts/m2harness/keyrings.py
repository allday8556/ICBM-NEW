"""Keyring backends for the harness's application processes, selected through keyring's own
``PYTHON_KEYRING_BACKEND`` setting. No product code is involved.

The product keeps SmartStore credentials, session keys and the A0 fingerprint key in
``KeyringSecretStore`` under the service ``ICBM-NEW``. The harness runs the unmodified product in
child processes and chooses where that service lives:

* REAL campaigns use ``CampaignScopedKeyring``: the OS credential store, under a service name
  scoped to the campaign ID. Acceptance credentials and sessions never mix with the operator's
  ordinary ICBM entries, and a campaign ID cannot be initialized twice on one machine;
* DRY runs use ``DryFileKeyring``: fixture values in a file inside the dry campaign directory, so
  CI and tests never touch the OS credential store and child processes share the same values.

Neither backend is ever chosen implicitly: while unconfigured, both report themselves non-viable.
"""

import contextlib
import json
import os
import sys
import time
from pathlib import Path

import keyring.backend
import keyring.core
import keyring.errors
from jaraco.classes import properties

from app.core.secrets import SERVICE_NAME
from scripts.m2harness.ledger import CAMPAIGN_ID

SCOPE_ENV = "ICBM_M2_KEYRING_SCOPE"
DRY_FILE_ENV = "ICBM_M2_DRY_KEYRING"
SCOPED_BACKEND = f"{__name__}.CampaignScopedKeyring"
DRY_BACKEND = f"{__name__}.DryFileKeyring"
# The platform credential stores a REAL campaign may use (the v1 target is Windows).
_OS_BACKENDS = {
    "win32": "keyring.backends.Windows.WinVaultKeyring",
    "darwin": "keyring.backends.macOS.Keyring",
}


def scoped_service(campaign_id: str) -> str:
    return f"{SERVICE_NAME}/m2-acceptance/{campaign_id}"


def os_backend() -> keyring.backend.KeyringBackend:
    name = _OS_BACKENDS.get(sys.platform)
    if name is None:
        raise keyring.errors.NoKeyringError("a REAL campaign needs the Windows credential store")
    return keyring.core.load_keyring(name)


class CampaignScopedKeyring(keyring.backend.KeyringBackend):
    @properties.classproperty
    def priority(cls) -> float:
        if not CAMPAIGN_ID.fullmatch(os.environ.get(SCOPE_ENV, "")):
            raise RuntimeError("selected explicitly for one REAL campaign only")
        return 0.1

    def __init__(self, campaign_id: str | None = None) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        scope = campaign_id if campaign_id is not None else os.environ.get(SCOPE_ENV, "")
        if not CAMPAIGN_ID.fullmatch(scope):
            raise keyring.errors.InitError("no M2 campaign scope is configured")
        self._service = scoped_service(scope)
        self._store = os_backend()

    def _scoped(self, service: str) -> str:
        if service != SERVICE_NAME:
            raise keyring.errors.KeyringError("only the ICBM-NEW service is campaign-scoped")
        return self._service

    def get_password(self, service: str, username: str) -> str | None:
        value = self._store.get_password(self._scoped(service), username)
        return value if isinstance(value, str) else None

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store.set_password(self._scoped(service), username, password)

    def delete_password(self, service: str, username: str) -> None:
        self._store.delete_password(self._scoped(service), username)


class DryFileKeyring(keyring.backend.KeyringBackend):
    """Fixture secrets of one DRY run, in a file of the dry campaign directory."""

    @properties.classproperty
    def priority(cls) -> float:
        if not os.environ.get(DRY_FILE_ENV):
            raise RuntimeError("selected explicitly for DRY runs only")
        return 0.1

    def __init__(self, path: Path | None = None) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        location = path if path is not None else Path(os.environ.get(DRY_FILE_ENV, ""))
        if not location.name:
            raise keyring.errors.InitError("no DRY keyring file is configured")
        self._path = location

    def _load(self) -> dict[str, dict[str, str]]:
        for _ in range(20):
            try:
                data = json.loads(self._path.read_text("utf-8"))
            except FileNotFoundError:
                return {}
            except (PermissionError, ValueError):
                time.sleep(0.05)  # another process of the run is replacing the file
                continue
            return data if isinstance(data, dict) else {}
        raise keyring.errors.KeyringError("the DRY keyring file cannot be read")

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        staging = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        staging.write_text(json.dumps(data, indent=1, sort_keys=True), "utf-8")
        for _ in range(20):
            with contextlib.suppress(PermissionError):
                os.replace(staging, self._path)
                return
            time.sleep(0.05)
        raise keyring.errors.KeyringError("the DRY keyring file cannot be written")

    def get_password(self, service: str, username: str) -> str | None:
        return self._load().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._load()
        data.setdefault(service, {})[username] = password
        self._save(data)

    def delete_password(self, service: str, username: str) -> None:
        data = self._load()
        if username not in data.get(service, {}):
            raise keyring.errors.PasswordDeleteError("no such entry")
        del data[service][username]
        self._save(data)
