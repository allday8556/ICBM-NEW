"""The append-only, hash-chained Phase C campaign ledger (Issue #110 C0 item 1).

One JSON line per event in ``<campaign-root>/campaign.jsonl``. Each event carries its sequence,
time, kind, actor, correlation and payload, the hash of the event before it and its own hash over
the canonical serialization of everything else. Nothing is ever rewritten: an event is only
appended, and every read and every append first verifies the whole chain, so an edit, deletion,
reordering or truncation of an earlier line is detected and refused.

The first event, ``CAMPAIGN_CREATED``, freezes the campaign identity, the exact code SHA, the
creating authorization and every stage ceiling. A stage is usable only after a
``STAGE_AUTHORIZED`` event names the architect authorization that opened it.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.phasec.ceilings import CEILINGS, STAGES

LEDGER = "campaign.jsonl"
SCHEMA = "icbm-adaptive-phase-c-campaign/v1"
GENESIS = "0" * 64
CAMPAIGN_ID = re.compile(r"^phase-c-[a-z0-9][a-z0-9-]{2,40}$")
AUTHORIZATION = re.compile(r"^[0-9]{6,20}$")


class LedgerRefused(RuntimeError):
    """The campaign ledger is absent, malformed, tampered with, or refuses this event."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(event: Mapping[str, Any]) -> str:
    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    code_sha: str
    ceilings: Mapping[str, Mapping[str, int]]
    authorized: Mapping[str, str]  # stage -> authorization comment id
    events: tuple[Mapping[str, Any], ...]


class CampaignLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / LEDGER

    # -------------------------------------------------------------- reads

    def events(self) -> tuple[dict[str, Any], ...]:
        if not self.path.is_file():
            raise LedgerRefused("no campaign ledger in this campaign root")
        events: list[dict[str, Any]] = []
        previous = GENESIS
        for index, line in enumerate(self.path.read_text("utf-8").splitlines(), start=1):
            try:
                event = json.loads(line)
            except ValueError:
                raise LedgerRefused(f"campaign ledger line {index} does not parse") from None
            if (
                not isinstance(event, dict)
                or event.get("seq") != index
                or event.get("prev") != previous
                or event.get("hash") != _hash(event)
            ):
                raise LedgerRefused(f"campaign ledger line {index} breaks the hash chain")
            events.append(event)
            previous = event["hash"]
        if not events or events[0].get("kind") != "CAMPAIGN_CREATED":
            raise LedgerRefused("the campaign ledger does not start with CAMPAIGN_CREATED")
        return tuple(events)

    def campaign(self) -> Campaign:
        events = self.events()
        created = events[0]["payload"]
        if created.get("schema") != SCHEMA or created.get("ceilings") != _ceilings():
            raise LedgerRefused("the campaign was created with other ceilings than this harness")
        authorized = {
            event["payload"]["stage"]: event["payload"]["authorization"]
            for event in events
            if event["kind"] == "STAGE_AUTHORIZED"
        }
        return Campaign(
            campaign_id=created["campaign_id"],
            code_sha=created["code_sha"],
            ceilings=created["ceilings"],
            authorized=authorized,
            events=events,
        )

    # -------------------------------------------------------------- appends

    def create(self, *, campaign_id: str, code_sha: str, authorization: str, actor: str) -> None:
        if not CAMPAIGN_ID.fullmatch(campaign_id):
            raise LedgerRefused("a campaign id is phase-c-<lowercase words>")
        if not AUTHORIZATION.fullmatch(authorization):
            raise LedgerRefused("an authorization is the numeric id of its GitHub comment")
        if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise LedgerRefused("the campaign binds an exact 40-hex code SHA")
        if self.path.exists():
            raise LedgerRefused("this campaign root already holds a campaign")
        self.root.mkdir(parents=True, exist_ok=True)
        self._write(
            [],
            "CAMPAIGN_CREATED",
            actor,
            f"{campaign_id}:created",
            {
                "schema": SCHEMA,
                "campaign_id": campaign_id,
                "code_sha": code_sha,
                "ceilings": _ceilings(),
            },
        )
        self.append(
            "STAGE_AUTHORIZED",
            actor,
            f"{campaign_id}:stage:C0",
            {"stage": "C0", "authorization": authorization},
        )

    def append(
        self, kind: str, actor: str, correlation_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        events = list(self.events())
        if kind == "STAGE_AUTHORIZED":
            stage = payload.get("stage")
            if stage not in STAGES or not AUTHORIZATION.fullmatch(
                str(payload.get("authorization", ""))
            ):
                raise LedgerRefused("a stage authorization names a stage and its comment id")
            if any(
                e["kind"] == "STAGE_AUTHORIZED" and e["payload"]["stage"] == stage for e in events
            ):
                raise LedgerRefused(f"stage {stage} is already authorized in this campaign")
        return self._write(events, kind, actor, correlation_id, dict(payload))

    def _write(
        self,
        events: list[dict[str, Any]],
        kind: str,
        actor: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not actor.strip() or not correlation_id.strip():
            raise LedgerRefused("every campaign event names its actor and correlation")
        event: dict[str, Any] = {
            "seq": len(events) + 1,
            "at": now(),
            "kind": kind,
            "actor": actor,
            "correlation_id": correlation_id,
            "payload": payload,
            "prev": events[-1]["hash"] if events else GENESIS,
        }
        event["hash"] = _hash(event)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(event) + "\n")
        return event


def _ceilings() -> dict[str, dict[str, int]]:
    return {stage: dict(values) for stage, values in CEILINGS.items()}


def approval_phrase(campaign: Campaign, command: str) -> str:
    """What the operator types, verbatim, before a command that can affect real evidence."""
    return f"I APPROVE {command} FOR {campaign.campaign_id} AT {campaign.code_sha[:12]}"
