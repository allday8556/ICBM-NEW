"""FIRST-TOKEN-CRASH fault injection without a production hook (Issue #46 §2.3; M2.md §5.4).

The crash phase runs the unmodified application in a child process. In that child only, the
harness takes the place of one existing transaction boundary of ``SmartStoreConnectService``:
``_session_for`` hands the typed ``TokenGrant`` that the caller returned (after the provider's
response passed the token success predicate) to ``_commit``, which would durably commit it. For
this one process, ``_commit`` is replaced by a function that

1. checks that it received a ``TokenGrant``, and that the campaign ledger holds exactly this
   process's one token request of the crash attempt, completed with HTTP 200, so the response
   really arrived;
2. writes and fsyncs a sanitized ``CRASH_BOUNDARY_REACHED`` marker, authenticated with a
   per-attempt nonce that the parent passed on stdin and never stored;
3. terminates the process with a dedicated exit code. ``_commit`` never runs.

The parent accepts the boundary only when all of these agree: the exit code, an authentic marker
for this campaign and attempt, the ledger's single completed token request of the attempt from
the same process id, a crash data directory with no committed session (no session file, no
session generation, no commit audit row), and a candidate scan with zero hits. Any other exit is
``BOUNDARY_NOT_EXERCISED``: before the token call, a failed call, a forged or stale marker. It is
``COMMITTED_BEFORE_CRASH`` (a FAIL) when a session was committed.
"""

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

from app import __version__
from app.audit.models import AuditEventType
from app.config import database_path
from app.connect.sessions import MARKETPLACE_SESSIONS_DIR_NAME
from app.connect.smartstore.service import SmartStoreConnectService
from app.core.ownership import acquire_data_dir, runtime_dir
from app.system.secret_scan import scan
from integrations.marketplaces.smartstore.caller import MARKETPLACE_KEY, TokenGrant
from integrations.marketplaces.smartstore.signing import ApplicationCredentials
from scripts.m2harness.evidence import write_bytes
from scripts.m2harness.ledger import CRASH_ATTEMPTS, TOKEN, CrashVerdict, Ledger, Phase, now

MARKER = "CRASH_BOUNDARY_REACHED"
EXIT_AT_BOUNDARY = 86
# The replacement ran, but the evidence of a completed token response was missing: no marker.
EXIT_BOUNDARY_REFUSED = 87
PHASE_OF_ATTEMPT: Mapping[int, Phase] = {
    number: phase for phase, (number, _) in CRASH_ATTEMPTS.items()
}
MARKER_FIELDS = (
    "marker",
    "campaign_id",
    "phase",
    "crash_attempt",
    "label",
    "reservation_seq",
    "pid",
    "at",
    "token_type",
    "expires_in",
    "credential_generation",
    "candidate_scan",
)


class Reason(StrEnum):
    EXIT_NOT_AT_BOUNDARY = "EXIT_NOT_AT_BOUNDARY"
    NO_MARKER = "NO_MARKER"
    MARKER_UNREADABLE = "MARKER_UNREADABLE"
    MARKER_NOT_AUTHENTIC = "MARKER_NOT_AUTHENTIC"
    MARKER_MISMATCH = "MARKER_MISMATCH"
    NO_COMPLETED_TOKEN_RESPONSE = "NO_COMPLETED_TOKEN_RESPONSE"
    SESSION_COMMITTED = "SESSION_COMMITTED"
    CANDIDATE_FOUND_IN_ARTIFACTS = "CANDIDATE_FOUND_IN_ARTIFACTS"
    # The harness process died while the attempt was open; its nonce is gone with it.
    ATTEMPT_INTERRUPTED = "ATTEMPT_INTERRUPTED"


def marker_mac(nonce: bytes, fields: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(fields), sort_keys=True, separators=(",", ":"))
    return hmac.new(nonce, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def write_marker(path: Path, fields: Mapping[str, Any], nonce: bytes) -> None:
    document = {**fields, "mac": marker_mac(nonce, fields)}
    write_bytes(path, json.dumps(document, sort_keys=True).encode("utf-8"))


# ---------------------------------------------------------------- the child side


def install_boundary(
    service: SmartStoreConnectService,
    *,
    ledger: Ledger,
    campaign_id: str,
    crash_attempt: int,
    marker_path: Path,
    nonce: bytes,
    scan_paths: Iterable[Path],
) -> None:
    """Replace ``_commit`` on this service instance, in this child process only."""
    phase = PHASE_OF_ATTEMPT[crash_attempt]
    _, label = CRASH_ATTEMPTS[phase]
    targets = list(scan_paths)

    def at_boundary(credentials: ApplicationCredentials, grant: TokenGrant) -> NoReturn:
        del credentials
        requests = ledger.requests_for(phase, 1)
        responded = (
            isinstance(grant, TokenGrant)
            and len(requests) == 1
            and requests[0]["endpoint_id"] == TOKEN
            and requests[0]["outcome"] == "RESPONDED"
            and requests[0]["http_status"] == 200
            and requests[0]["pid"] == os.getpid()
        )
        if not responded:
            os._exit(EXIT_BOUNDARY_REFUSED)
        # Counts only: is the uncommitted candidate anywhere on disk at the boundary?
        report = scan(targets, {"t4_candidate": grant.access_token})
        fields = {
            "marker": MARKER,
            "campaign_id": campaign_id,
            "phase": phase.value,
            "crash_attempt": crash_attempt,
            "label": label,
            "reservation_seq": requests[0]["seq"],
            "pid": os.getpid(),
            "at": now(),
            "token_type": grant.token_type,
            "expires_in": grant.expires_in,
            "credential_generation": grant.credential_generation,
            "candidate_scan": {
                "files_scanned": report["files_scanned"],
                "total_hits": report["total_hits"],
            },
        }
        write_marker(marker_path, fields, nonce)
        os._exit(EXIT_AT_BOUNDARY)

    service._commit = at_boundary  # type: ignore[method-assign]


# ---------------------------------------------------------------- the parent side


@dataclass(frozen=True)
class DataDirState:
    session_file: bool
    session_generation_hwm: int
    bound: bool
    commit_audits: int

    @property
    def committed(self) -> bool:
        return self.session_file or self.session_generation_hwm > 0 or self.commit_audits > 0


def data_dir_state(data_dir: Path) -> DataDirState:
    """Read a data directory under its own lease, so no process can hold it meanwhile."""
    with acquire_data_dir(data_dir, app_version=__version__):
        session_file = (
            runtime_dir(data_dir) / MARKETPLACE_SESSIONS_DIR_NAME / f"{MARKETPLACE_KEY}.enc"
        ).is_file()
        uri = f"{database_path(data_dir).resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            row = db.execute(
                "SELECT session_generation_hwm, provider_account_uid FROM marketplace_connections"
                " WHERE marketplace_key = ?",
                (MARKETPLACE_KEY,),
            ).fetchone()
            audits = db.execute(
                "SELECT count(*) FROM audit_events WHERE event_type = ?",
                (AuditEventType.MARKETPLACE_SESSION_COMMITTED.value,),
            ).fetchone()[0]
    return DataDirState(
        session_file=session_file,
        session_generation_hwm=int(row[0]) if row else 0,
        bound=bool(row and row[1]),
        commit_audits=int(audits),
    )


@dataclass(frozen=True)
class CrashResult:
    crash_attempt: int
    label: str
    verdict: CrashVerdict
    reasons: tuple[Reason, ...]
    exit_code: int | None
    reservation_seq: int | None
    marker: dict[str, Any] | None

    def evidence(self, connect_result: str | None) -> dict[str, Any]:
        marker = None
        if self.marker is not None:
            marker = {name: self.marker.get(name) for name in MARKER_FIELDS}
        return {
            "crash_attempt": self.crash_attempt,
            "label": self.label,
            "verdict": self.verdict.value,
            "reasons": [reason.value for reason in self.reasons],
            "exit_code": self.exit_code,
            "reservation_seq": self.reservation_seq,
            "connect_result": connect_result,
            "marker": marker,
        }


def _read_marker(path: Path, nonce: bytes, reasons: list[Reason]) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        reasons.append(Reason.NO_MARKER)
        return None
    except (OSError, ValueError):
        reasons.append(Reason.MARKER_UNREADABLE)
        return None
    if not isinstance(raw, dict):
        reasons.append(Reason.MARKER_UNREADABLE)
        return None
    mac = raw.pop("mac", None)
    if not isinstance(mac, str) or not hmac.compare_digest(mac, marker_mac(nonce, raw)):
        # A marker this attempt's nonce did not authenticate: stale, copied or forged.
        reasons.append(Reason.MARKER_NOT_AUTHENTIC)
        return None
    return raw


def verify_boundary(
    *,
    exit_code: int | None,
    marker_path: Path,
    nonce: bytes,
    ledger: Ledger,
    campaign_id: str,
    crash_attempt: int,
    data_dir: Path,
) -> CrashResult:
    """Decide whether the child died at the intended boundary. Evidence must all agree."""
    phase = PHASE_OF_ATTEMPT[crash_attempt]
    _, label = CRASH_ATTEMPTS[phase]
    reasons: list[Reason] = []
    if exit_code != EXIT_AT_BOUNDARY:
        reasons.append(Reason.EXIT_NOT_AT_BOUNDARY)
    marker = _read_marker(marker_path, nonce, reasons)
    expected = {
        "marker": MARKER,
        "campaign_id": campaign_id,
        "phase": phase.value,
        "crash_attempt": crash_attempt,
        "label": label,
    }
    if marker is not None and any(marker.get(key) != value for key, value in expected.items()):
        reasons.append(Reason.MARKER_MISMATCH)
    requests = ledger.requests_for(phase, 1)
    token = requests[0] if len(requests) == 1 else None
    if token is None or (token["endpoint_id"], token["outcome"], token["http_status"]) != (
        TOKEN,
        "RESPONDED",
        200,
    ):
        reasons.append(Reason.NO_COMPLETED_TOKEN_RESPONSE)
    elif marker is not None and (marker.get("reservation_seq"), marker.get("pid")) != (
        token["seq"],
        token["pid"],
    ):
        reasons.append(Reason.MARKER_MISMATCH)
    state = data_dir_state(data_dir)
    if state.committed:
        reasons.append(Reason.SESSION_COMMITTED)
    leaked = marker is not None and (marker.get("candidate_scan") or {}).get("total_hits") != 0
    if leaked:
        reasons.append(Reason.CANDIDATE_FOUND_IN_ARTIFACTS)
    reasons = list(dict.fromkeys(reasons))
    if state.committed:
        verdict = CrashVerdict.COMMITTED_BEFORE_CRASH
    elif any(reason is not Reason.CANDIDATE_FOUND_IN_ARTIFACTS for reason in reasons):
        verdict = CrashVerdict.BOUNDARY_NOT_EXERCISED
    elif leaked:
        verdict = CrashVerdict.CANDIDATE_LEAKED
    else:
        verdict = CrashVerdict.BOUNDARY_EXERCISED
    return CrashResult(
        crash_attempt=crash_attempt,
        label=label,
        verdict=verdict,
        reasons=tuple(reasons),
        exit_code=exit_code,
        reservation_seq=token["seq"] if token is not None else None,
        marker=marker if verdict is not CrashVerdict.BOUNDARY_NOT_EXERCISED else None,
    )
