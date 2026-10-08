"""What is actually serving the sidecar endpoint (ADR-0012 §2, §3, §5; ADR-0027 §3, §5).

An open port or an answering endpoint is never proof. The probe reads the process that listens on
the endpoint's port: its executable path, the SHA-256 of that file and its command line. From
the command line and the sidecar's own configuration file it derives the routing identity: a
SHA-256 over the configuration with every secret removed and the launch arguments, plus whether
the remote model catalog (``-local-model``) and the panel auto-update are off.

The probe reads; it never starts, stops, edits or downloads anything (ADR-0027 AIS-05). It never
reads a secret: every key, secret, token and password line is removed before the configuration is
fingerprinted, and the client key ICBM uses is its own, in the OS secret store (AIS-01).
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

# One YAML field: its indent and an optional list dash, its name, and its value.
_FIELD: Final = re.compile(
    r"^(\s*(?:-\s+)?)([^\s:#\-\"'][^:#]*?|\"[^\"]+\"|'[^']+')\s*:(?:\s+(.*))?$"
)
_SECRET_NAME: Final = re.compile(r"(key|secret|token|password)", re.IGNORECASE)
_SECRET_FLAG: Final = re.compile(r"(key|secret|token|password|auth)", re.IGNORECASE)
_LOCAL_MODEL_FLAGS: Final = frozenset({"-local-model", "--local-model"})
_TRUE: Final = frozenset({"true", "1", "t"})


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


def file_sha256(path: str) -> str | None:
    """The SHA-256 of the file's bytes, read now. Never cached: a replaced binary that keeps the
    same size and modification time is still a different identity (ADR-0012 §3, AIS-02)."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


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


def config_path(process: ServingProcess) -> Path | None:
    """The configuration the serving process actually reads, or ``None`` when that cannot be
    known. Only an explicit, absolute ``-config`` names it: the last occurrence, as Go flags take
    it. A relative path or the default resolves against the sidecar's own working directory, which
    the probe cannot read, so it is never guessed (fail-closed)."""
    args = _arguments(process.command_line)
    named: str | None = None
    for index, arg in enumerate(args):
        if arg in ("-config", "--config") and index + 1 < len(args):
            named = args[index + 1]
        elif arg.startswith(("-config=", "--config=")):
            named = arg.split("=", 1)[1]
    if not named or not Path(named).is_absolute():
        return None
    return Path(named)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


_BLOCK_SCALAR: Final = frozenset({"|", ">", "|-", ">-", "|+", ">+"})


def _without_secrets(text: str) -> str:
    """The configuration with every secret value replaced, and nothing else removed.

    - A key whose name is secret (a key, secret, token or password) keeps its line with its value
      replaced: ``api-key: <redacted>``. A block scalar under it is dropped.
    - The bare list items directly under a secret header (``api-keys:`` then ``- "…"``) are
      replaced: ``- <redacted>``.
    - Every other field stays, so a mapping such as ``- api-key: …`` with its ``base-url: …`` keeps
      the destination in the fingerprint.
    - Comments and blank lines are dropped, so no secret written in a comment reaches it."""
    kept: list[str] = []
    header: tuple[int, bool] | None = None  # the newest header above: its indent, and if secret
    hidden: int | None = None  # the indent of a secret block scalar being dropped
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = _indent(line)
        if hidden is not None:
            if indent > hidden:
                continue
            hidden = None
        while (
            header is not None
            and indent <= header[0]
            and not (indent == header[0] and line.lstrip().startswith("- ") and header[1])
        ):
            header = None
        field = _FIELD.match(line)
        if field is None:
            if header is not None and header[1] and line.lstrip().startswith("-"):
                kept.append(" " * indent + "- <redacted>")
            else:
                kept.append(line)
            continue
        lead, name, value = field.group(1), field.group(2), (field.group(3) or "").strip()
        secret = bool(_SECRET_NAME.search(name))
        if not value:
            header = (indent, secret)
            kept.append(line)
        elif secret:
            if value in _BLOCK_SCALAR:
                hidden = indent
            kept.append(f"{lead}{name}: <redacted>")
        else:
            kept.append(line)
    return "\n".join(kept).strip()


def _redacted_args(args: list[str]) -> list[str]:
    """The launch arguments with the value of every secret-named flag replaced."""
    redacted: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next and not arg.startswith("-"):
            redacted.append("<redacted>")
            hide_next = False
            continue
        hide_next = False
        name, sep, _ = arg.partition("=")
        if arg.startswith("-") and _SECRET_FLAG.search(name):
            if sep:
                redacted.append(f"{name}=<redacted>")
                continue
            hide_next = True
        redacted.append(arg)
    return redacted


def _local_model(args: list[str]) -> bool:
    """Whether ``-local-model`` is in effect: a Go boolean flag, where the last occurrence wins and
    ``-local-model=false`` turns it off again."""
    effective = False
    for arg in args:
        name, sep, value = arg.partition("=")
        if name in _LOCAL_MODEL_FLAGS:
            effective = not sep or value.strip().lower() in _TRUE
    return effective


def _panel_off(text: str) -> bool:
    """Whether the panel auto-update is off, read strictly: exactly one top-level
    ``remote-management:`` mapping whose own direct field ``disable-auto-update-panel`` is ``true``,
    and that key nowhere else at any depth. A shadowed, duplicated, nested or misplaced setting is
    never taken as off."""
    sections: list[str] = []
    child_indent: int | None = None  # the indent of the current top-level section's own fields
    found: list[tuple[str | None, bool, str]] = []  # (section, a direct field, value)
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = _indent(line)
        field = _FIELD.match(line)
        if indent == 0:
            child_indent = None
            if field is not None and not field.group(1).strip():
                name = field.group(2).strip("\"'")
                sections.append(name)
                if name == "disable-auto-update-panel":
                    found.append((None, False, (field.group(3) or "").strip().lower()))
            continue
        if child_indent is None:
            child_indent = indent
        if field is None:
            continue
        name = field.group(2).strip("\"'")
        if name == "disable-auto-update-panel":
            direct = indent == child_indent and not field.group(1).strip().startswith("-")
            value = (field.group(3) or "").strip().strip("\"'").lower()
            found.append((sections[-1] if sections else None, direct, value))
    return (
        sections.count("remote-management") == 1
        and len(found) == 1
        and found[0] == ("remote-management", True, "true")
    )


def routing(process: ServingProcess) -> RoutingObservation | None:
    path = config_path(process)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    args = _arguments(process.command_line)
    payload = json.dumps(
        # In their own order: a Go flag's last occurrence wins, so order is part of the routing.
        {"config": _without_secrets(text), "args": _redacted_args(args)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return RoutingObservation(
        fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        local_model=_local_model(args),
        panel_auto_update_disabled=_panel_off(text),
    )
