"""Typed, immutable Phase C stage grants (Issue #110; review `5313663701` B2).

A stage opens only through a grant: one JSON object whose every field is fixed. The architect's
authorization carries it, and the operator copies it verbatim into a file. The harness reads that
file's bytes once: the approval phrase names the SHA-256 of exactly those bytes, ``check_grant``
parses exactly those bytes, and the ledger records exactly those bytes. A grant names the
campaign, its exact code SHA, the stage, its authorization and the stage's scope:

* **C0**: the frozen C0 ceilings. Its authorization is pinned in this audited code
  (``issuecomment-5826469852``); a campaign is created only under it.

An authorization is an opaque GitHub anchor, ``issuecomment-<id>`` or ``pullrequestreview-<id>``:
it is used once per campaign and never compared by size. The order of the stages is the
campaign ledger's own: C0 → C1 → C2 → C3 → C4, once each.
* **C1**: the supplier, exactly two distinct target digests and the frozen C1 ceilings.
* **C2**: the exact EPR, sample set and PASS ValidationRun this campaign's own C1 recorded, and
  the window size K = 3.
* **C3**: the exact window this campaign's own C2 declared for that bundle, and the C3 ceilings.
* **C4**: that same window, for end and close.

``check_grant`` refuses anything else: another campaign or SHA, a skipped or repeated stage, an
authorization already used, extra or missing fields, other ceilings, or a scope this campaign's
own ledger does not already hold. The ``grants`` table enforces the order and the single use
again.
"""

import re
from collections.abc import Mapping
from typing import Any

from scripts.phasec.ceilings import CEILINGS, STAGES
from scripts.phasec.ledger import (
    AUTHORIZATION,
    Campaign,
    LedgerRefused,
    bytes_digest,
    canonical,
    parse_grant,
)

GRANT_SCHEMA = "icbm-adaptive-phase-c-grant/v1"
C0_AUTHORIZATION = "issuecomment-5826469852"  # Issue #110: the C0 tooling authorization
FIELDS = frozenset({"schema", "campaign_id", "code_sha", "stage", "authorization", "scope"})
SCOPE_FIELDS: Mapping[str, frozenset[str]] = {
    "C0": frozenset({"ceilings"}),
    "C1": frozenset({"supplier_key", "target_digests", "ceilings"}),
    "C2": frozenset(
        {
            "supplier_key",
            "epr_digest",
            "sample_digests",
            "validation_run_id",
            "window_min_size",
            "ceilings",
        }
    ),
    "C3": frozenset({"supplier_key", "window_id", "bundle_key", "ceilings"}),
    "C4": frozenset({"supplier_key", "window_id", "ceilings"}),
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class GrantRefused(LedgerRefused):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="PHASE_C_GRANT_REFUSED")


def grant_digest(raw: bytes) -> str:
    """The SHA-256 of a grant's exact bytes: what the approval phrase names and the ledger keeps."""
    return bytes_digest(raw)


def c0_grant(campaign_id: str, code_sha: str, authorization: str) -> bytes:
    if authorization != C0_AUTHORIZATION:
        raise GrantRefused(
            f"a campaign is created only under the C0 authorization {C0_AUTHORIZATION}"
        )
    grant = {
        "schema": GRANT_SCHEMA,
        "campaign_id": campaign_id,
        "code_sha": code_sha,
        "stage": "C0",
        "authorization": authorization,
        "scope": {"ceilings": dict(CEILINGS["C0"])},
    }
    return canonical(grant).encode("utf-8")


def _one(campaign: Campaign, kind: str, **match: Any) -> Mapping[str, Any] | None:
    found = [
        payload
        for payload in campaign.outcomes(kind)
        if all(payload.get(key) == value for key, value in match.items())
    ]
    return found[-1] if found else None


def check_grant(campaign: Campaign, raw: bytes) -> dict[str, Any]:
    """The grant these exact bytes hold, if they may be recorded for ``campaign``'s next stage."""
    grant = parse_grant(raw)
    if not isinstance(grant, dict) or set(grant) != FIELDS:
        raise GrantRefused(f"a grant holds exactly {sorted(FIELDS)}")
    if grant["schema"] != GRANT_SCHEMA:
        raise GrantRefused(f"a grant's schema is {GRANT_SCHEMA}")
    if grant["campaign_id"] != campaign.campaign_id or grant["code_sha"] != campaign.code_sha:
        raise GrantRefused("the grant names another campaign or another exact code SHA")
    stage = grant["stage"]
    following = STAGES.index(campaign.current_stage) + 1
    if stage not in STAGES or following >= len(STAGES) or stage != STAGES[following]:
        upcoming = STAGES[following] if following < len(STAGES) else "none"
        raise GrantRefused(
            f"the next stage of this campaign is {upcoming}; stages advance once each, in order"
        )
    authorization = grant["authorization"]
    if not isinstance(authorization, str) or not AUTHORIZATION.fullmatch(authorization):
        raise GrantRefused(
            "a grant names its authorization as issuecomment-<id> or pullrequestreview-<id>"
        )
    if any(authorization == g.authorization for g in campaign.grants.values()):
        raise GrantRefused("an authorization opens one stage of a campaign, once")
    scope = grant["scope"]
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS[stage]:
        raise GrantRefused(f"a {stage} scope holds exactly {sorted(SCOPE_FIELDS[stage])}")
    if scope["ceilings"] != dict(campaign.ceilings[stage]):
        raise GrantRefused(f"a {stage} grant carries this campaign's frozen {stage} ceilings")
    _SCOPES[stage](campaign, scope)
    return grant


def supplier_of(campaign: Campaign) -> str:
    supplier: str = campaign.grants["C1"].scope["supplier_key"]
    return supplier


def _c1(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    supplier, targets = scope["supplier_key"], scope["target_digests"]
    if not isinstance(supplier, str) or not supplier.strip():
        raise GrantRefused("a C1 grant names its supplier")
    wanted = campaign.ceilings["C1"]["target_identities"]
    if (
        not isinstance(targets, list)
        or len(targets) != wanted
        or len(set(targets)) != wanted
        or not all(isinstance(t, str) and _HEX64.fullmatch(t) for t in targets)
        or targets != sorted(targets)
    ):
        raise GrantRefused(f"a C1 grant names exactly {wanted} distinct sorted target digests")


def _same_supplier(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    if scope["supplier_key"] != supplier_of(campaign):
        raise GrantRefused("every later grant keeps the C1 grant's supplier")


def _c2(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    _same_supplier(campaign, scope)
    samples = scope["sample_digests"]
    if not isinstance(samples, list) or samples != sorted(set(samples)) or not samples:
        raise GrantRefused("a C2 grant names its sorted, distinct sample digests")
    if scope["window_min_size"] != campaign.ceilings["C3"]["window_min_size"]:
        raise GrantRefused("a C2 grant declares the K = 3 window")
    validated = _one(
        campaign,
        "VALIDATE",
        epr=scope["epr_digest"],
        run_id=scope["validation_run_id"],
        samples=samples,
        verdict="PASS",
    )
    if validated is None:
        raise GrantRefused(
            "a C2 grant names the EPR, samples and PASS run this campaign's own C1 recorded"
        )


def _c3(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    _same_supplier(campaign, scope)
    epr = campaign.grants["C2"].scope["epr_digest"]
    enabled = _one(campaign, "ENABLE", epr=epr)
    declared = _one(campaign, "DECLARE", window_id=scope["window_id"], epr=epr)
    if (
        enabled is None
        or declared is None
        or declared.get("bundle_key") != scope["bundle_key"]
        or enabled.get("bundle_key") != scope["bundle_key"]
    ):
        raise GrantRefused(
            "a C3 grant names the window this campaign's own C2 declared for its enabled bundle"
        )


def _c4(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    _same_supplier(campaign, scope)
    if scope["window_id"] != campaign.grants["C3"].scope["window_id"]:
        raise GrantRefused("a C4 grant ends and closes the same window as the C3 grant")


def _c0(campaign: Campaign, scope: Mapping[str, Any]) -> None:
    raise GrantRefused("C0 is granted only when the campaign is created")


_SCOPES = {"C0": _c0, "C1": _c1, "C2": _c2, "C3": _c3, "C4": _c4}
