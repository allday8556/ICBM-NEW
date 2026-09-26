"""The canonical side of the Adaptive seams: the one-fetch shadow (ADR-0017 §10.1; Issue #110 P3
`5824551569`) and the in-memory sample capture (Phase C C0, `5826469852`).

COLLECT core owns the run, and this is all it knows about either: protocols that something else
implements, and the plain values it hands across. Nothing here imports the Adaptive packages, so
the canonical owners never depend on them.

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

``CaptureFreezer``
    Called by the run store inside the same first-reservation unit. It answers ``FrozenCapture``:
    ``REQUESTED`` with the one pending capture request it consumes for this run's target, or
    ``OFF`` — the default. A retry never asks again, and a run whose first reservation predates
    the seam has no decision at all and is never captured.

``CaptureStep``
    Called after the canonical revision append, and after the shadow step, only for a run frozen
    ``REQUESTED``. It receives the one in-memory ``DocumentView`` and keeps only sanitized capture
    material, never the page body. It never raises into the run and cannot change its outcome.

``SendAccounting``
    Called by the collection right after the first-reservation unit, before any send (C1 PREP-0,
    ``5841947773``). For a run whose frozen capture request binds it to a Phase C campaign it
    answers the ``SendGuard`` that durably reserves each of the attempt's sends, or refuses before
    any send when the binding is missing, mismatched or unreadable. For every other run it answers
    ``None`` and the run is exactly as before.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.collect.facts import CollectedFacts, ImageReference
from app.collect.urls import UrlPolicy
from app.core.send_guard import SendGuard
from integrations.suppliers.collection import DocumentView, ImageCandidate, SourceIdentityResult

ShadowDecision = Literal["ENABLED", "DISABLED"]
CaptureDecision = Literal["REQUESTED", "OFF"]


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
class FrozenCapture:
    """One run's capture decision, frozen at its first product-read reservation (C0). Default
    ``OFF``; ``REQUESTED`` names the one capture request the run consumed."""

    decision: CaptureDecision
    request_id: str | None = None

    def __post_init__(self) -> None:
        if (self.decision == "REQUESTED") != (self.request_id is not None):
            raise ValueError("a requested capture names its request, and only it")


CAPTURE_OFF = FrozenCapture("OFF")


@dataclass(frozen=True)
class FrozenRun:
    """What a run froze, read back from the canonical run record: the only input the shadow step,
    the capture step and Phase C eligibility ever use, never the current configuration.

    ``capture`` is ``None`` for a run whose first reservation predates the capture seam: such a run
    is never captured, and nothing is backfilled.
    """

    shadow: FrozenShadow
    first_product_read_at: datetime
    capture: FrozenCapture | None = None


class ShadowFreezer(Protocol):
    def __call__(self, session: Session, supplier_key: str) -> FrozenShadow: ...


class CaptureFreezer(Protocol):
    def __call__(self, session: Session, supplier_key: str, target: str) -> FrozenCapture: ...


@dataclass(frozen=True)
class CaptureInput:
    """What a capture may use: the one document the run already read, in memory only."""

    collection_run_id: str
    supplier_key: str
    capture: FrozenCapture
    document: DocumentView = field(repr=False)
    revision_id: str


class CaptureStep(Protocol):
    def __call__(self, capture: CaptureInput) -> None: ...


class SendAccounting(Protocol):
    def bind(
        self,
        *,
        collection_run_id: str,
        supplier_key: str,
        target: str,
        capture: "FrozenCapture | None",
        attempt_no: int,
    ) -> SendGuard | None:
        """The guard that accounts this attempt's sends, ``None`` for an unaccounted run, or an
        ``AppError`` refusing the run before any send."""


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
