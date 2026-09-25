"""The canonical side of the one-fetch shadow seam (ADR-0017 §10.1; Issue #110 P3 `5824551569`).

COLLECT core owns the run, and this is all it knows about a shadow comparison: two protocols that
something else implements, and the plain values it hands across. Nothing here imports the
Adaptive packages, so the canonical owners never depend on them.

``ShadowFreezer``
    Called by the run store **inside** the canonical write unit of a run's first product-read
    reservation, with that unit's own session. It reads the supplier's shadow switch and answers
    ``FrozenShadow``: the identity of the switch entry in effect and the exact bundle it names, or
    an explicit *disabled*. The run store freezes that answer on the run, once. It never writes.

``ShadowStep``
    Called by the collection **after** the canonical write has committed — the revision append,
    or on the identity-unresolved path the durable ``NO_REVISION`` outcome itself — with only what
    the run already holds in memory: the one
    ``DocumentView``, the canonical facts and image candidates, the observed checksums and the
    frozen decision. It opens its own write unit, never raises into the run, and has no gateway,
    session, budget or egress handle to spend.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.collect.facts import CollectedFacts, ImageReference
from app.collect.urls import UrlPolicy
from integrations.suppliers.collection import DocumentView, ImageCandidate, SourceIdentityResult

ShadowDecision = Literal["ENABLED", "DISABLED"]


@dataclass(frozen=True)
class FrozenShadow:
    """One run's shadow decision, frozen at its first product-read reservation (ADR-0017 §10.1).

    ``ENABLED`` names the switch-history entry in effect and the exact bundle identity that entry
    enabled, as the canonical text of the Adaptive comparability key. ``DISABLED`` names neither.
    """

    decision: ShadowDecision
    switch_entry_id: str | None = None
    bundle_key: str | None = None

    def __post_init__(self) -> None:
        enabled = self.switch_entry_id is not None and self.bundle_key is not None
        if (self.decision == "ENABLED") != enabled or (
            self.decision == "DISABLED"
            and (self.switch_entry_id is not None or self.bundle_key is not None)
        ):
            raise ValueError("an enabled decision names its switch entry and bundle, and only it")


DISABLED = FrozenShadow("DISABLED")


@dataclass(frozen=True)
class FrozenRun:
    """What a run froze, read back from the canonical run record: the only input the shadow step
    and Phase C eligibility ever use, never the switch's current setting."""

    shadow: FrozenShadow
    first_product_read_at: datetime


class ShadowFreezer(Protocol):
    def __call__(self, session: Session, supplier_key: str) -> FrozenShadow: ...


@dataclass(frozen=True)
class ShadowInput:
    """Everything a shadow comparison may use: what the canonical run already holds in memory.

    ``collected`` is the exact ``CollectedFacts`` the canonical revision was appended from, or
    ``None`` when the canonical side is ``UNRESOLVED`` and appended nothing. ``candidates`` are the
    supplier's own classified image references of the one document, and ``images`` the canonical
    references with the checksums the canonical fetch observed. ``document`` is used in memory
    only and is never persisted (ADR-0010 §3).
    """

    collection_run_id: str
    supplier_key: str
    source_url: str
    frozen: FrozenRun
    document: DocumentView = field(repr=False)
    identity: SourceIdentityResult
    collected: CollectedFacts | None
    url_policy: UrlPolicy
    candidates: tuple[ImageCandidate, ...] = field(repr=False)
    images: tuple[ImageReference, ...]
    revision_id: str | None


class ShadowStep(Protocol):
    def __call__(self, shadow: ShadowInput) -> None: ...
