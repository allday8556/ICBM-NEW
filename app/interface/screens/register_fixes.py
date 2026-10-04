"""B-UX2: the fix-only projection of Registration Management (ADR-0014 §22 amendment note).

A composite read model over two owners, kept here in the screens layer so that REGISTER never
reads the review owner (ADR-0016 G2-02):

- every reason of every **pre-send** unit, exactly as the preflight owner returned it (the B-UX1
  population), and every unit the owner could not evaluate, with its real refusal code;
- every **open** REGISTRATION_ERROR item of the REGISTER execution producer. The preparation
  producer's items index the very preflight reasons listed above, so they are not listed twice.

Each row carries the server-owned actionability of :mod:`app.interface.screens.actionability`.
``fixes`` holds only what the operator can act on now (``FIX_AVAILABLE`` and ``RECHECK``); every
other row is counted by actionability and never shown as a fix. Nothing is stored, judged again or
fixed here: the screen navigates to the named surface.
"""

from collections import Counter
from datetime import datetime
from typing import Final

from pydantic import BaseModel

from app.capabilities.review.model import ReviewKind, ReviewState
from app.capabilities.review.owner import ReviewItemStore
from app.capabilities.review.register_producer import REGISTER_PRODUCER
from app.interface.screens.actionability import (
    ACTIONABILITY_LABELS,
    ACTIONABILITY_VERSION,
    SURFACE_LABELS,
    Actionability,
    Entry,
    not_evaluated_action,
    preflight_action,
    review_action,
)
from app.platform.core.clock import Clock
from app.stages.register.service import RegisterService

FIX_PROJECTION_VERSION: Final = "register-fix-projection/v1"
ACTIONABLE: Final = frozenset({Actionability.FIX_AVAILABLE, Actionability.RECHECK})


class FixRowView(BaseModel):
    """One reason or review condition, with what can be done about it."""

    source: str  # PREFLIGHT | NOT_EVALUATED | REVIEW
    draft_id: str | None
    unit_ref: str | None
    intent_id: str | None
    review_item_id: str | None
    code: str
    status: str | None
    subject: str | None
    areas: tuple[str, ...] = ()
    actionability: str
    actionability_label: str
    surface: str | None
    surface_label: str | None


class RegisterFixesView(BaseModel):
    projection_version: str
    actionability_version: str
    evaluated_at: datetime
    # Only what the operator can act on now; the rest is counted below, never offered as a fix.
    fixes: tuple[FixRowView, ...]
    counts: dict[str, int]


def _row(
    entry: Entry,
    *,
    source: str,
    code: str,
    draft_id: str | None = None,
    unit_ref: str | None = None,
    intent_id: str | None = None,
    review_item_id: str | None = None,
    status: str | None = None,
    subject: str | None = None,
    areas: tuple[str, ...] = (),
) -> FixRowView:
    actionability, surface = entry
    return FixRowView(
        source=source,
        draft_id=draft_id,
        unit_ref=unit_ref,
        intent_id=intent_id,
        review_item_id=review_item_id,
        code=code,
        status=status,
        subject=subject,
        areas=areas,
        actionability=actionability.value,
        actionability_label=ACTIONABILITY_LABELS[actionability],
        surface=None if surface is None else surface.value,
        surface_label=None if surface is None else SURFACE_LABELS[surface],
    )


class RegisterFixService:
    """Reads the REGISTER screen service and the review owner; writes nothing."""

    def __init__(self, register: RegisterService, review_items: ReviewItemStore, clock: Clock):
        self._register = register
        self._review_items = review_items
        self._clock = clock

    def fixes(self) -> RegisterFixesView:
        rows: list[FixRowView] = []
        for unit in self._register.pre_send_units():
            if unit.preflight is None:
                code = unit.preflight_unavailable_reason or "REGISTER_PREFLIGHT_NOT_WIRED"
                rows.append(
                    _row(
                        not_evaluated_action(code),
                        source="NOT_EVALUATED",
                        code=code,
                        draft_id=unit.draft_id,
                        unit_ref=unit.unit_ref,
                    )
                )
                continue
            rows.extend(
                _row(
                    preflight_action(reason.code),
                    source="PREFLIGHT",
                    code=reason.code,
                    draft_id=unit.draft_id,
                    unit_ref=unit.unit_ref,
                    status=reason.status,
                    subject=reason.subject,
                    areas=reason.areas,
                )
                for reason in unit.preflight.reasons
            )
        for item in self._review_items.items(
            kind=ReviewKind.REGISTRATION_ERROR,
            state=ReviewState.OPEN,
            producer=REGISTER_PRODUCER,
            limit=10_000,
        ):
            rows.append(
                _row(
                    review_action(item.reason_code),
                    source="REVIEW",
                    code=item.reason_code,
                    draft_id=item.scope.get("draft_id"),
                    intent_id=item.scope.get("intent_id"),
                    review_item_id=item.review_item_id,
                    subject=item.subject,
                )
            )
        tally = Counter(row.actionability for row in rows)
        return RegisterFixesView(
            projection_version=FIX_PROJECTION_VERSION,
            actionability_version=ACTIONABILITY_VERSION,
            evaluated_at=self._clock.now(),
            fixes=tuple(row for row in rows if Actionability(row.actionability) in ACTIONABLE),
            counts={a.value: tally.get(a.value, 0) for a in Actionability},
        )
