"""Offline guards for one M4 acceptance run (Issue #80 PR-F kickoff 5739459941 §B, §C).

Three guards, armed for the run only. Each counts what it refuses, so the report states measured
numbers rather than assumed ones:
- **Network.** The process egress guard of ``app.core.egress`` blocks every non-loopback socket.
  The run's blocked attempts and any supplier egress grant opened during it are counted.
- **Imports.** A meta-path finder refuses to load AI, OCR, marketplace, CONNECT, supplier
  transport, browser or HTTP-client code, and counts each attempt. A module of that kind already
  loaded when the guard arms is listed too: the harness's own import graph is loaded before the run
  starts, so a forbidden module it pulled in transitively can never pass as absent. The modules of
  that kind loaded during the run are listed.
- **Paths.** An audit hook watches file opens, directory listings and database connections. A path
  naming a preserved campaign runtime is refused and counted. Every write outside the acceptance
  root is counted; bytecode caches are the only exception. A SQLite connection can write unless its
  URI opens it ``mode=ro``, so every other file connection counts as a write.

Audit hooks and the egress guard stay installed for the life of the process, but only an armed
run counts or refuses anything through them.
"""

import importlib.abc
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, unquote

from app.core.egress import EGRESS
from scripts.m4accept.root import names_preserved_campaign

# AI and OCR (the COLLECT source-truth hard-zero list), and every provider-facing path M4 must
# never reach: marketplace callers, the SmartStore and supplier CONNECT services with their
# credentials and sessions, supplier transports and the KM collection, browser automation, HTTP
# clients, and the collection job. ORM table definitions are not provider paths: the schema
# aggregate (`app.db.metadata`) loads them, SmartStore's and the marketplace's included.
FORBIDDEN_MODULES = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "cohere",
    "mistralai",
    "groq",
    "ollama",
    "litellm",
    "langchain",
    "llama_index",
    "transformers",
    "torch",
    "tensorflow",
    "onnxruntime",
    "pytesseract",
    "tesserocr",
    "easyocr",
    "paddleocr",
    "cv2",
    "azure.ai",
    "azure.cognitiveservices",
    "app.ai",
    "integrations.ai",
    "integrations.marketplaces",
    "app.connect.marketplace.service",
    "app.connect.marketplace.attestation_service",
    "app.connect.marketplace.sources",
    "app.connect.marketplace.revision",
    "app.connect.smartstore.service",
    "app.connect.smartstore.credentials",
    "app.connect.service",
    "app.connect.sessions",
    "app.connect.credentials",
    "integrations.suppliers.transport",
    "integrations.suppliers.kmretail",
    "app.collect.collection",
    "playwright",
    "httpx",
    "requests",
    "urllib3",
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def forbidden(name: str, modules: Sequence[str] | None = None) -> bool:
    """Whether loading ``name`` is refused by the armed run's list (the M4 default by default)."""
    return any(
        name == f or name.startswith(f"{f}.")
        for f in (FORBIDDEN_MODULES if modules is None else modules)
    )


@dataclass
class _State:
    armed: bool = False
    root: Path | None = None
    blocked_imports: list[str] = field(default_factory=list)
    preserved_paths: int = 0
    writes_outside_root: int = 0
    # The provider surface the armed run may not load. M4 uses the default above; a later
    # milestone whose owners legitimately import an adapter arms its own list (M5 PR-F).
    modules: tuple[str, ...] = FORBIDDEN_MODULES


_STATE = _State()
_INSTALLED = {"hooks": False}


class _ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if _STATE.armed and forbidden(fullname, _STATE.modules):
            _STATE.blocked_imports.append(fullname)
            raise ImportError(f"this acceptance run may not import {fullname}")
        return None


def _paths(event: str, args: tuple[Any, ...]) -> tuple[list[str], bool]:
    """The paths an audited event touches, and whether it writes."""
    if event == "open":
        target, mode, flags = (*args, None, None, None)[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        return [_text(target)], writes
    if event == "sqlite3.connect":
        database = _text(args[0] if args else None)
        if _in_memory(database):
            return [], False
        return [database], not _read_only(database)
    if event in ("os.listdir", "os.scandir"):
        return [_text(args[0] if args else None)], False
    if event in ("os.mkdir", "os.remove", "os.rmdir"):
        return [_text(args[0] if args else None)], True
    if event == "os.rename":
        return [_text(args[0] if args else None), _text(args[1] if len(args) > 1 else None)], True
    return [], False


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str | os.PathLike):
        text = os.fspath(value)
        return text if isinstance(text, str) else text.decode("utf-8", "replace")
    return ""


def _uri_mode(database: str) -> list[str]:
    if not database.startswith("file:") or "?" not in database:
        return []
    return parse_qs(database.split("?", 1)[1]).get("mode", [])


def _read_only(database: str) -> bool:
    """Only a URI that opens the database ``mode=ro`` cannot write it."""
    return _uri_mode(database) == ["ro"]


def _in_memory(database: str) -> bool:
    return _local(database) in ("", ":memory:") or _uri_mode(database) == ["memory"]


def _local(text: str) -> str:
    """A database URI's file part; any other path as given."""
    if text.startswith("file:"):
        text = unquote(text[len("file:") :].split("?", 1)[0])
        if text.startswith("///"):
            text = text[3:]
        elif text.startswith("//"):
            text = text[2:]
    return text


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if not _STATE.armed:
        return
    paths, writes = _paths(event, args)
    for raw in paths:
        text = _local(raw)
        if not text:
            continue
        if names_preserved_campaign(PurePath(text)):
            _STATE.preserved_paths += 1
            raise PermissionError("an acceptance run never touches a preserved campaign")
        if writes and _STATE.root is not None:
            absolute = Path(os.path.abspath(text))
            inside = absolute == _STATE.root or absolute.is_relative_to(_STATE.root)
            if not inside and "__pycache__" not in absolute.parts:
                _STATE.writes_outside_root += 1


def _install() -> None:
    if _INSTALLED["hooks"]:
        return
    sys.meta_path.insert(0, _ImportGuard())
    sys.addaudithook(_hook)
    _INSTALLED["hooks"] = True


@dataclass(frozen=True)
class GuardEvidence:
    external_network_attempts: int
    egress_grants_opened: int
    forbidden_imports_blocked: tuple[str, ...]
    forbidden_modules_preloaded: tuple[str, ...]
    forbidden_modules_loaded_during_run: tuple[str, ...]
    preserved_campaign_paths_refused: int
    writes_outside_root: int

    def as_json(self) -> dict[str, object]:
        return {
            "external_network_attempts": self.external_network_attempts,
            "egress_grants_opened": self.egress_grants_opened,
            "forbidden_imports_blocked": list(self.forbidden_imports_blocked),
            "forbidden_modules_preloaded": list(self.forbidden_modules_preloaded),
            "forbidden_modules_loaded_during_run": list(self.forbidden_modules_loaded_during_run),
            "preserved_campaign_paths_refused": self.preserved_campaign_paths_refused,
            "writes_outside_root": self.writes_outside_root,
        }


@dataclass
class Guarded:
    evidence: GuardEvidence | None = None


def _grants(snapshot: dict[str, Any]) -> int:
    granted = snapshot.get("granted_events") or {}
    return sum(int(count) for count in granted.values())


@contextmanager
def offline(root: Path, *, modules: Sequence[str] = FORBIDDEN_MODULES) -> Iterator[Guarded]:
    """Arm every guard for the block; the evidence is filled in when it ends.

    ``modules`` is the provider surface this run may not load. It is the M4 list by default; a
    run whose own owners import an adapter passes the list that is forbidden to *it*.
    """
    EGRESS.install()
    _install()
    _STATE.modules = tuple(modules)
    before_egress = EGRESS.snapshot()
    preloaded = tuple(sorted(name for name in sys.modules if forbidden(name, modules)))
    _STATE.root = root
    _STATE.blocked_imports.clear()
    _STATE.preserved_paths = 0
    _STATE.writes_outside_root = 0
    _STATE.armed = True
    guarded = Guarded()
    try:
        yield guarded
    finally:
        _STATE.armed = False
        after_egress = EGRESS.snapshot()
        loaded = sorted(
            name for name in sys.modules if forbidden(name, modules) and name not in preloaded
        )
        guarded.evidence = GuardEvidence(
            external_network_attempts=int(after_egress["external_attempts"])
            - int(before_egress["external_attempts"]),
            egress_grants_opened=_grants(after_egress) - _grants(before_egress),
            forbidden_imports_blocked=tuple(_STATE.blocked_imports),
            forbidden_modules_preloaded=preloaded,
            forbidden_modules_loaded_during_run=tuple(loaded),
            preserved_campaign_paths_refused=_STATE.preserved_paths,
            writes_outside_root=_STATE.writes_outside_root,
        )
        _STATE.root = None
