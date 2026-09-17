"""The frozen contract of ``m3-accept-01`` (Issue #52 rulings 5711123764 §2–§5, 5711187191).

Everything a REAL pass may do is written here once, and nothing an operator types can widen it:
the request classes and their ceilings are code, so a different budget is a different commit, a
different SHA, a new audit and a new approval — never a flag.

The budget in this module is **proposed for review and NOT authorized for REAL**. It becomes
executable only after the prep PR is merged, the exact SHA is frozen, and the operator types the
approval phrase for that SHA and this campaign id.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

CAMPAIGN_ID = "m3-accept-01"
SUPPLIER_KEY = "kmretail"
PASSES = ("A", "B")
MIB = 1024 * 1024


class RequestClass(StrEnum):
    """Every kind of external request a pass could conceivably make.

    A class without a ceiling here has no reservation path at all, so it cannot happen.
    """

    # COLLECT, through the production policed collection gateway.
    PRODUCT_READ = "PRODUCT_READ"
    IMAGE_REQUEST = "IMAGE_REQUEST"
    POLICY_READ = "POLICY_READ"
    # CONNECT, through the M1 connection owner's gateway.
    CONNECT_CONTROL_READ = "CONNECT_CONTROL_READ"
    CONNECT_PROTECTED_READ = "CONNECT_PROTECTED_READ"
    CONNECT_AUTHENTICATE = "CONNECT_AUTHENTICATE"


# Categories that have no transport in the collection path at all. They are asserted zero at
# closeout and at PREP, not reserved: there is nothing to reserve them against.
HARD_ZERO = ("AI", "OCR", "MARKETPLACE", "SUPPLIER_WRITE")
# Hosts the accepted reconnaissance observed and excluded. No request may name them.
EXCLUDED_IMAGE_HOSTS = frozenset({"img.cafe24.com", "img.echosting.cafe24.com"})


@dataclass(frozen=True)
class Ceiling:
    per_pass: int
    campaign: int

    def __post_init__(self) -> None:
        if self.per_pass < 0 or self.campaign < 0 or self.per_pass > self.campaign:
            raise ValueError("a ceiling is non-negative and a pass never exceeds its campaign")


@dataclass(frozen=True)
class CampaignBudget:
    ceilings: Mapping[RequestClass, Ceiling]
    per_image_bytes: int
    new_image_bytes_per_pass: int
    same_product_interval_s: float
    # The accepted reconnaissance recognised this many product-evidence references. It is a hard
    # maximum per pass, never a target to fill.
    product_evidence_baseline: int

    def ceiling(self, request: RequestClass) -> Ceiling:
        return self.ceilings.get(request, Ceiling(0, 0))

    def as_json(self) -> dict[str, Any]:
        return {
            "ceilings": {
                request.value: {"per_pass": c.per_pass, "campaign": c.campaign}
                for request, c in sorted(self.ceilings.items())
            },
            "per_image_bytes": self.per_image_bytes,
            "new_image_bytes_per_pass": self.new_image_bytes_per_pass,
            "same_product_interval_s": self.same_product_interval_s,
            "product_evidence_baseline": self.product_evidence_baseline,
            "hard_zero": list(HARD_ZERO),
            "excluded_image_hosts": sorted(EXCLUDED_IMAGE_HOSTS),
        }

    def digest(self) -> str:
        return _digest(self.as_json())


# Ruling 5711123764 §4 and 5711187191. PROPOSED — NOT AUTHORIZED FOR REAL.
#
# The CONNECT classes are zero because the ruling freezes AUTH/login/recovery at zero. The M1
# connection owner, however, proves a stored session with a control read and a protected read
# before it hands the session out, so under this budget a pass stops before its product read. That
# is deliberate fail-closed behaviour, and the question of budgeting the proof is raised for a
# ruling rather than decided here.
M3_ACCEPT_01_BUDGET = CampaignBudget(
    ceilings=MappingProxyType(
        {
            RequestClass.PRODUCT_READ: Ceiling(per_pass=2, campaign=4),
            RequestClass.IMAGE_REQUEST: Ceiling(per_pass=13, campaign=26),
            RequestClass.POLICY_READ: Ceiling(per_pass=0, campaign=0),
            RequestClass.CONNECT_CONTROL_READ: Ceiling(per_pass=0, campaign=0),
            RequestClass.CONNECT_PROTECTED_READ: Ceiling(per_pass=0, campaign=0),
            RequestClass.CONNECT_AUTHENTICATE: Ceiling(per_pass=0, campaign=0),
        }
    ),
    per_image_bytes=2 * MIB,
    new_image_bytes_per_pass=24 * MIB,
    same_product_interval_s=60.0,
    product_evidence_baseline=13,
)


@dataclass(frozen=True)
class ByteFacts:
    """What retained reconnaissance metadata proves about image sizes, and nothing more.

    Only the references phase B actually sampled have a known size. Every other reference is
    unknown, and no request is made to find out. Whether 24 MiB suffices for all of them is
    therefore not claimed.
    """

    sampled: int
    known_bytes: tuple[int, ...]
    unknown_references: int
    sufficiency_claimed: bool = False

    @property
    def known_total(self) -> int:
        return sum(self.known_bytes)

    @property
    def largest_known(self) -> int:
        return max(self.known_bytes, default=0)

    def as_json(self) -> dict[str, Any]:
        return {
            "sampled": self.sampled,
            "known_bytes": list(self.known_bytes),
            "known_total": self.known_total,
            "largest_known": self.largest_known,
            "unknown_references": self.unknown_references,
            "sufficiency_claimed": self.sufficiency_claimed,
        }


def byte_facts(phase_b: Mapping[str, Any], *, baseline: int) -> ByteFacts:
    """Read the sizes phase B proved, from its sanitized findings only. No request, no guess."""
    images = phase_b.get("images")
    if not isinstance(images, list):
        raise ValueError("the phase B findings carry no image list")
    sizes = []
    for image in images:
        size = image.get("bytes") if isinstance(image, Mapping) else None
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            sizes.append(size)
    return ByteFacts(
        sampled=len(sizes),
        known_bytes=tuple(sorted(sizes)),
        unknown_references=max(0, baseline - len(sizes)),
    )


def target_digest(canonical_product_url: str) -> str:
    """The target as GitHub may see it: a digest of the canonical URL, never the URL itself."""
    return hashlib.sha256(f"{CAMPAIGN_ID}\n{canonical_product_url}".encode()).hexdigest()


def approval_phrase(code_sha: str) -> str:
    """The exact words the operator types. It is shown, never generated on their behalf."""
    return f"APPROVE {CAMPAIGN_ID} {code_sha[:12]} TWO-PASS-REAL"


@dataclass(frozen=True)
class Manifest:
    """What is armed, and after that can never change."""

    campaign_id: str
    code_sha: str
    target_digest: str
    budget: CampaignBudget
    byte_facts: ByteFacts
    mode: str  # "REAL" or "DRY"
    passes: Sequence[str] = field(default=PASSES)

    def as_json(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "code_sha": self.code_sha,
            "target_digest": self.target_digest,
            "budget": self.budget.as_json(),
            "budget_digest": self.budget.digest(),
            "byte_facts": self.byte_facts.as_json(),
            "mode": self.mode,
            "passes": list(self.passes),
        }

    def digest(self) -> str:
        return _digest(self.as_json())


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
