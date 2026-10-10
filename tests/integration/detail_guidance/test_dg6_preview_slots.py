"""ADR-0033 G6: the frozen-Snapshot preview shows the Detail Guidance notice slots (§6, §7).

The preview of a ``registration-payload/v3`` Snapshot lists, in the order ``detail-renderer/v2``
draws them, the ``TOP_GUIDANCE`` notices, the detail images, the body and the ``BOTTOM_GUIDANCE``
notices. Each notice slot is its frozen entry (SHA-256, source, template) with the Detail Guidance
owner's **local** image route — built only in the read view, so the Snapshot stays URL-free — and a
store-wide notice is labelled with its kind and period from the owner's newest revisions. A v3 unit
without a notice shows no slot and is otherwise what its v2 counterpart shows; a v1 or v2 preview is
exactly as before. Provider-zero: nothing is sent.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.capabilities.detail_guidance.renderer import Template
from app.capabilities.detail_guidance.store import DetailGuidanceStore, Kind, Placement
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.config import AppConfig
from app.container import Container
from app.stages.register.detail import DETAIL_RENDERER_VERSION, DETAIL_RENDERER_VERSION_V2
from app.stages.register.guidance import GUIDANCE_IMAGE_PATH, ResolvedGuidance
from app.stages.register.payload import build_payload
from app.stages.register.preview import PreviewGuidanceView, preview
from integrations.marketplaces.smartstore import product
from tests.integration.detail_guidance.test_dg5_placement import (  # noqa: F401 - fixtures
    APPROVAL,
    BODY,
    HOLIDAY,
    T0,
    _artifacts,
    _grant,
    _save,
    _Sender,
    _unit,
    _upload_run,
    clock,
    notices,
)
from tests.support.register_support import CID, OPERATOR
from tests.unit.register.test_b_detail_composition import DETAILS, REPRESENTATIVE, req, unit_of
from tests.unit.register.test_dg5_composition import ready, v3_req, v3_unit

pytestmark = pytest.mark.integration

HASH = "h" * 64
STARTS = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
ENDS = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
# The pre-G6 read shapes, which a v1 or v2 preview keeps exactly.
VIEW_KEYS = {
    "preview_version",
    "registration_snapshot_id",
    "payload_hash",
    "builder_version",
    "name",
    "sale_prices_krw",
    "category_id",
    "listing_identity",
    "projected",
    "refusal_code",
    "encoding_version",
    "sendable",
    "gaps",
    "document",
    "images",
    "detail",
    "not_sent",
}
DETAIL_KEYS = {"builder", "sections", "renderer", "image_slots", "paragraphs"}


@dataclass(frozen=True)
class Revision:
    """One Detail Guidance revision as the owner reports its newest (the ``GuidanceRevision``
    port REGISTER reads)."""

    revision_id: str
    guidance_id: str
    placement: str
    kind: str
    template: str
    image_sha256: str
    enabled: bool = True
    starts_at: datetime | None = None
    ends_at: datetime | None = None


# The newest revisions behind tests/unit/register/test_dg5_composition.py's GUIDED resolution: a
# standing top notice (also placed at the bottom) and a period bottom notice.
NEWEST = (
    Revision("g-top-1", "guid-top", "TOP", "STANDING", "CLEAN", "a" * 64),
    Revision("g-period-1", "guid-period", "BOTTOM", "PERIOD", "WARM", "b" * 64, True, STARTS, ENDS),
)


def url(sha256: str) -> str:
    return GUIDANCE_IMAGE_PATH.format(sha256=sha256)


def dumped(view: Any) -> dict[str, Any]:
    found: dict[str, Any] = view.model_dump(mode="json")
    return found


def test_a_v3_preview_lists_the_top_notices_first_and_the_bottom_notices_last() -> None:
    frozen = build_payload(ready(v3_req("first\n\nsecond"), v3_unit())).payload
    before = json.dumps(frozen, sort_keys=True)
    view = preview("snap-v3", HASH, frozen, product.project, guidance_revisions=NEWEST)
    assert view.projected, view.refusal_code
    detail = view.detail
    assert detail is not None and detail.guidance is not None
    assert (detail.builder, detail.renderer) == ("PLAN", DETAIL_RENDERER_VERSION_V2)
    # The renderer's order: top notices → detail images → body → bottom notices.
    assert detail.sections == ("TOP_GUIDANCE", "DETAIL_IMAGES", "BODY", "BOTTOM_GUIDANCE")
    assert detail.image_slots == ("3" * 64, "4" * 64)
    assert detail.paragraphs == ("first", "second")
    top, bottom = detail.guidance.top, detail.guidance.bottom
    assert [(s.placement, s.section, s.sha256, s.image_url) for s in top] == [
        ("TOP", "TOP_GUIDANCE", "a" * 64, url("a" * 64))
    ]
    assert [(s.placement, s.section, s.sha256, s.image_url) for s in bottom] == [
        ("BOTTOM", "BOTTOM_GUIDANCE", sha * 64, url(sha * 64)) for sha in "bca"
    ]
    # Each slot is its frozen entry, labelled from the owner's newest revisions.
    assert [(s.source, s.guidance_revision_id, s.template, s.kind) for s in top] == [
        ("STORE", "g-top-1", "CLEAN", "STANDING")
    ]
    holiday, own, standing = bottom
    assert (holiday.kind, holiday.starts_at, holiday.ends_at) == ("PERIOD", STARTS, ENDS)
    assert (own.source, own.preparation_revision_id, own.kind) == (
        "PRODUCT",
        "prep-rev-1",
        "STANDING",
    )
    assert (standing.kind, standing.starts_at) == ("STANDING", None)
    # Every notice of this Snapshot has its provider asset prepared.
    assert all(s.provider_asset_prepared for s in (*top, *bottom))
    # The image is the local ICBM route: never a provider URL in the view, nothing written back.
    shown = json.dumps(dumped(view), ensure_ascii=False)
    assert "://" not in shown and "shop-phinf" not in shown
    assert json.dumps(frozen, sort_keys=True) == before
    assert "/api/v1/" not in before


def test_a_frozen_revision_the_owner_no_longer_holds_is_shown_without_a_guessed_kind() -> None:
    frozen = build_payload(ready(v3_req(), v3_unit())).payload
    # The period notice was edited after the freeze: its frozen revision is not the newest.
    newer = Revision("g-period-2", "guid-period", "BOTTOM", "PERIOD", "WARM", "e" * 64)
    edited = (NEWEST[0], newer)
    view = preview("snap-v3", HASH, frozen, product.project, guidance_revisions=edited)
    assert view.detail is not None and view.detail.guidance is not None
    holiday = view.detail.guidance.bottom[0]
    assert (holiday.sha256, holiday.guidance_revision_id) == ("b" * 64, "g-period-1")
    assert (holiday.kind, holiday.starts_at, holiday.ends_at) == (None, None, None)
    # Without any owner read, the slots and their order are the frozen plan's all the same.
    unread = preview("snap-v3", HASH, frozen, product.project)
    assert unread.detail is not None and unread.detail.guidance is not None
    assert [s.sha256 for s in unread.detail.guidance.bottom] == [sha * 64 for sha in "bca"]
    assert {s.kind for s in unread.detail.guidance.bottom} == {None, "STANDING"}


def test_an_unprepared_notice_says_so() -> None:
    frozen = build_payload(ready(v3_req(), v3_unit())).payload
    frozen["guidance_assets"][1]["provider_asset_ref"] = None
    view = preview("snap-v3", HASH, frozen, product.project, guidance_revisions=NEWEST)
    assert view.projected is False  # the projection refuses it, with its own code
    assert view.detail is not None and view.detail.guidance is not None
    prepared = {s.sha256: s.provider_asset_prepared for s in view.detail.guidance.bottom}
    assert prepared == {"a" * 64: True, "b" * 64: False, "c" * 64: True}


def test_a_v3_unit_without_a_notice_shows_no_slot_and_otherwise_what_v2_shows() -> None:
    v3 = preview(
        "snap",
        HASH,
        build_payload(ready(v3_req(), v3_unit(ResolvedGuidance()))).payload,
        product.project,
        guidance_revisions=NEWEST,
    )
    v2 = preview(
        "snap",
        HASH,
        build_payload(ready(req("body text"), unit_of(REPRESENTATIVE, *DETAILS))).payload,
        product.project,
        guidance_revisions=NEWEST,
    )
    assert v3.detail is not None and v2.detail is not None
    assert v3.detail.guidance == PreviewGuidanceView()
    assert v2.detail.guidance is None
    shown3, shown2 = dumped(v3), dumped(v2)
    assert shown3["detail"].pop("guidance") == {"top": [], "bottom": []}
    for shown in (shown3, shown2):
        shown.pop("builder_version")
        shown["detail"].pop("sections")
        shown["detail"].pop("renderer")
    assert shown3 == shown2


def test_a_v1_and_a_v2_preview_are_unchanged() -> None:
    from tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter import payload

    v2 = preview(
        "snap",
        HASH,
        build_payload(ready(req("body text"), unit_of(REPRESENTATIVE, *DETAILS))).payload,
        product.project,
        guidance_revisions=NEWEST,
    )
    v1 = preview("snap", HASH, payload(), product.project, guidance_revisions=NEWEST)
    for view, builder in ((v2, "PLAN"), (v1, "BODY_ONLY")):
        shown = dumped(view)
        assert set(shown) == VIEW_KEYS
        assert set(shown["detail"]) == DETAIL_KEYS
        assert shown["detail"]["builder"] == builder
        assert "guidance" not in json.dumps(shown)
    assert dumped(v2)["detail"] == {
        "builder": "PLAN",
        "sections": ["DETAIL_IMAGES", "BODY"],
        "renderer": DETAIL_RENDERER_VERSION,
        "image_slots": ["3" * 64, "4" * 64],
        "paragraphs": ["body text"],
    }
    assert dumped(v1)["detail"] == {
        "builder": "BODY_ONLY",
        "sections": ["BODY"],
        "renderer": None,
        "image_slots": [],
        "paragraphs": ["본문"],
    }


def test_a_frozen_v3_snapshot_previews_its_notices_through_the_register_owner(
    container: Container,
    config: AppConfig,
    notices: DetailGuidanceStore,  # noqa: F811
) -> None:
    """End to end through the application's owners: a STANDING top notice and a PERIOD bottom
    notice are placed, uploaded provider-zero and frozen; the Registration Management preview of
    that Snapshot shows them around the detail image and the body, labelled from the owner."""
    standing = _save(notices)
    holiday = _save(
        notices,
        placement=Placement.BOTTOM,
        kind=Kind.PERIOD,
        template=Template.WARM,
        content=HOLIDAY,
        starts_at=T0 - timedelta(hours=1),
        ends_at=T0 + timedelta(hours=2),
    )
    top, bottom = standing.image_sha256, holiday.image_sha256
    unit = _unit(container, config)
    grant = _grant(container, unit, _artifacts(unit, top, bottom))
    with LiveAuthorityStore(container.db, container.clock, container.audit).transaction() as live:
        live.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    assert (
        _upload_run(container, config, notices, _Sender())
        .run(grant.grant_id, window_s=900, actor=OPERATOR, correlation_id=CID)
        .complete
    )
    frozen = container.registration_preparations.freeze_current(
        unit.preparation_id, actor=OPERATOR, correlation_id=CID
    )
    snapshot_id = frozen.snapshot.registration_snapshot_id
    stored = container.registrations.snapshot_payload(snapshot_id)

    view = container.register.snapshot_preview(snapshot_id)
    assert view.projected, view.refusal_code
    assert view.detail is not None and view.detail.guidance is not None
    assert view.detail.image_slots == (unit.detail,)
    assert view.detail.paragraphs == (BODY,)
    (top_slot,) = view.detail.guidance.top
    (bottom_slot,) = view.detail.guidance.bottom
    assert (top_slot.sha256, top_slot.image_url, top_slot.kind) == (top, url(top), "STANDING")
    assert (top_slot.guidance_revision_id, top_slot.template) == (standing.revision_id, "CLEAN")
    assert (bottom_slot.sha256, bottom_slot.image_url, bottom_slot.kind) == (
        bottom,
        url(bottom),
        "PERIOD",
    )
    assert (bottom_slot.starts_at, bottom_slot.ends_at) == (holiday.starts_at, holiday.ends_at)
    assert top_slot.provider_asset_prepared and bottom_slot.provider_asset_prepared
    # The local route serves exactly the frozen notice's bytes.
    assert notices.image(top) and notices.image(bottom)
    # A read: the Snapshot stays URL-free and unchanged.
    assert container.registrations.snapshot_payload(snapshot_id) == stored
    assert "/api/v1/" not in json.dumps(stored)
    assert "://" not in json.dumps(dumped(view), ensure_ascii=False)
