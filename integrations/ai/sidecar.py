"""What is actually serving the sidecar endpoint (ADR-0012 §2, §3, §5; ADR-0027 §3, §5).

An open port or an answering endpoint is never proof. The probe reads the process that listens on
the endpoint's port: its executable path, the SHA-256 of that file and its command line. From
the command line and the sidecar's own configuration file it derives the routing identity: a
SHA-256 over the configuration with every secret removed and the launch arguments, plus whether
the remote model catalog (``-local-model``) and the panel auto-update are off.

The probe reads; it never starts, stops, edits or downloads anything (ADR-0027 AIS-05). The client
key is read from the sidecar's configuration at call time and returned only to the adapter; it is
never logged, stored or fingerprinted (AIS-01).
"""

import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

_SECRET_LINE: Final = re.compile(r"(keys?|secrets?|tokens?|passwords?)\s*:", re.IGNORECASE)
_LIST_ITEM: Final = re.compile(r"^\s*-\s")
_PANEL_OFF: Final = re.compile(r"^\s*disable-auto-update-panel\s*:\s*true\s*$", re.MULTILINE)
_LOCAL_MODEL_FLAGS: Final = frozenset({"-local-model", "--local-model"})


@dataclass(frozen=True)
class ServingProcess:
    pid: int
    path: str
    sha256: str
    command_line: str


@dataclass(frozen=True)
class RoutingObservation:
    fingerprint: str
    local_model: bool
    panel_auto_update_disabled: bool


class ServingProcessProbe(Protocol):
    def serving(self, port: int) -> ServingProcess | None:
        """The process listening on this loopback port, or ``None`` when it cannot be read."""
        ...


_HASHES: dict[tuple[str, int, int], str] = {}


def file_sha256(path: str) -> str | None:
    file = Path(path)
    try:
        stat = file.stat()
    except OSError:
        return None
    key = (str(file), stat.st_size, stat.st_mtime_ns)
    if key not in _HASHES:
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        _HASHES[key] = digest.hexdigest()
    return _HASHES[key]


_PROBE_SCRIPT: Final = (
    "$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue"
    " | Where-Object {{ $_.LocalAddress -eq '127.0.0.1' }} | Select-Object -First 1;"
    ' if ($c) {{ $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)";'
    " @{{pid=$p.ProcessId; path=$p.ExecutablePath; cmd=$p.CommandLine}}"
    " | ConvertTo-Json -Compress }}"
)


class WindowsProcessProbe:
    """The Windows probe: the TCP table and the process image, read through PowerShell. On any
    other platform nothing can be verified, so it reads nothing (fail-closed)."""

    def __init__(self, timeout_s: float = 15.0) -> None:
        self._timeout_s = timeout_s

    def serving(self, port: int) -> ServingProcess | None:
        if sys.platform != "win32":
            return None
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _PROBE_SCRIPT.format(port=int(port)),
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
            found = json.loads(completed.stdout) if completed.stdout.strip() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not isinstance(found, dict) or not found.get("path"):
            return None
        sha256 = file_sha256(str(found["path"]))
        if sha256 is None:
            return None
        return ServingProcess(
            pid=int(found["pid"]),
            path=str(found["path"]),
            sha256=sha256,
            command_line=str(found.get("cmd") or ""),
        )


def _arguments(command_line: str) -> list[str]:
    try:
        parts = shlex.split(command_line, posix=False)
    except ValueError:
        return []
    return [part.strip('"') for part in parts[1:]]


def config_path(process: ServingProcess) -> Path:
    args = _arguments(process.command_line)
    for index, arg in enumerate(args):
        if arg in ("-config", "--config") and index + 1 < len(args):
            return Path(args[index + 1])
        if arg.startswith(("-config=", "--config=")):
            return Path(arg.split("=", 1)[1])
    return Path(process.path).parent / "config.yaml"


def _without_secrets(text: str) -> str:
    """The configuration with every secret line removed: a key, secret, token or password, and
    every list item under such a key."""
    kept: list[str] = []
    in_secret_list = False
    for line in text.splitlines():
        if in_secret_list and _LIST_ITEM.match(line):
            continue
        in_secret_list = False
        if _SECRET_LINE.search(line.split("#", 1)[0]):
            in_secret_list = line.rstrip().endswith(":")
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def routing(process: ServingProcess) -> RoutingObservation | None:
    try:
        text = config_path(process).read_text(encoding="utf-8")
    except OSError:
        return None
    args = sorted(_arguments(process.command_line))
    payload = json.dumps(
        {"config": _without_secrets(text), "args": args}, ensure_ascii=False, sort_keys=True
    )
    return RoutingObservation(
        fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        local_model=bool(_LOCAL_MODEL_FLAGS & set(args)),
        panel_auto_update_disabled=bool(_PANEL_OFF.search(text)),
    )


def client_key(process: ServingProcess) -> str | None:
    """The first client key of the sidecar's ``api-keys`` list. Returned to the adapter only."""
    try:
        text = config_path(process).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^api-keys\s*:\s*$", line):
            for item in lines[index + 1 :]:
                match = re.match(r'^\s*-\s*["\']?([^"\'\s#]+)["\']?\s*$', item)
                if match:
                    return match.group(1)
                if item.strip() and not item.lstrip().startswith("#"):
                    return None
    return None
