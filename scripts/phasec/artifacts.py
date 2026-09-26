"""Immutable, sanitized campaign artifacts (Issue #110 C0 item 7; review `5312911203` Q7).

A mismatch resolution is chosen by the human operator — never by AI, Claude or GPT — and before
the ledger's ``RESOLUTION_RECORDED`` is appended its artifact is written here: canonical JSON, named
by its own SHA-256, never overwritten. ``evidence_ref`` is the stable content address
``phase-c:<campaign-id>:resolution:<sha256>``; a resolution is appended only when the artifact it
names exists and hashes to it.

An artifact holds identifiers, dimensions, digests and the operator's answer only. It never holds a
page body, a URL, a cookie, a header, a credential, session material or private data: anything that
looks like one refuses the artifact.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.collect.adaptive.capture import residual_findings
from app.collect.adaptive_shadow.evidence import Resolution

RESOLUTIONS = "resolutions"
ARTIFACT_SCHEMA = "icbm-adaptive-phase-c-resolution/v1"
EVIDENCE_REF = re.compile(
    r"^phase-c:(?P<campaign>phase-c-[a-z0-9-]+):resolution:(?P<sha>[0-9a-f]{64})$"
)
_FORBIDDEN = re.compile(r"(?i)(://|<[a-z!/]|cookie|set-cookie|authorization:|bearer\s)")
FIELDS = frozenset(
    {
        "schema",
        "campaign_id",
        "collection_run_id",
        "revision_id",
        "mismatch_dimensions",
        "source_evidence",
        "resolution",
        "adaptive_failed_closed",
        "actor",
        "correlation_id",
        "at",
    }
)


class ArtifactRefused(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _strings(item)]
    return []


def evidence_ref(campaign_id: str, sha256: str) -> str:
    return f"phase-c:{campaign_id}:resolution:{sha256}"


def _checked(artifact: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    content = dict(artifact)
    if set(content) != FIELDS or content.get("schema") != ARTIFACT_SCHEMA:
        raise ArtifactRefused("a resolution artifact holds exactly its fields")
    Resolution(content["resolution"])  # a closed vocabulary
    if not isinstance(content["adaptive_failed_closed"], bool):
        raise ArtifactRefused("the operator answers whether the Adaptive side failed closed")
    for text in _strings(content):
        if _FORBIDDEN.search(text) or residual_findings(text):
            raise ArtifactRefused("a resolution artifact never holds page, URL or secret material")
    serialized = canonical(content)
    return content, serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolution_reference(artifact: Mapping[str, Any]) -> str:
    """The ``evidence_ref`` an artifact will have, checked, before anything is written: a command
    reserves it first, so its reconciliation can look for exactly that reference."""
    content, _, sha = _checked(artifact)
    return evidence_ref(content["campaign_id"], sha)


def write_resolution(campaign_root: Path, artifact: Mapping[str, Any]) -> str:
    """Write one resolution artifact and return its ``evidence_ref``."""
    content, serialized, sha = _checked(artifact)
    directory = campaign_root / RESOLUTIONS
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.json"
    if path.exists():
        if path.read_text("utf-8") != serialized:
            raise ArtifactRefused("an artifact already holds this name with other content")
    else:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    return evidence_ref(content["campaign_id"], sha)


def read_resolution(campaign_root: Path, reference: str) -> dict[str, Any]:
    """The artifact an ``evidence_ref`` names, only if it exists and hashes to that name."""
    match = EVIDENCE_REF.fullmatch(reference)
    if match is None:
        raise ArtifactRefused("not a Phase C resolution evidence_ref")
    path = campaign_root / RESOLUTIONS / f"{match.group('sha')}.json"
    if not path.is_file():
        raise ArtifactRefused("the resolution artifact is missing")
    text = path.read_text("utf-8")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != match.group("sha"):
        raise ArtifactRefused("the resolution artifact does not hash to its reference")
    loaded: dict[str, Any] = json.loads(text)
    if loaded.get("campaign_id") != match.group("campaign"):
        raise ArtifactRefused("the resolution artifact belongs to another campaign")
    return loaded
