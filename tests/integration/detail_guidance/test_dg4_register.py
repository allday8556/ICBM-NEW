"""ADR-0033 G4: REGISTER consumes the Detail Guidance notices, without placing them.

A product's preparation carries a choice per placement — ``DEFAULT``, ``OFF`` or ``CUSTOM`` (§5).
The preflight resolves it at the Detail Guidance owner's injected clock (§6): the active period
notices in ``(starts_at, guidance_id)`` order, then the product's own notice or else the current
standing one. The resolved lists join the candidate fingerprint only when one is non-empty, and a
unit with any resolved notice is ``BLOCKED`` by ``PUBLICATION_GUIDANCE_UNPLACED`` because no
composition places a notice before G5 (§8). A unit with no resolved notice is exactly what it was:
same reasons, same candidate and final fingerprints, same payload and Snapshot.

No provider is reached and nothing is placed: composition, payload builder and Snapshot are G5's.
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.audit.service import AuditLog
from app.capabilities.detail_guidance.content import GUIDANCE_TEXT_INVALID
from app.capabilities.detail_guidance.image_store import GuidanceImageStore
from app.capabilities.detail_guidance.renderer import Template
from app.capabilities.detail_guidance.service import IMAGE_PATH
from app.capabilities.detail_guidance.store import (
    GUIDANCE_TEMPLATE_UNKNOWN,
    DetailGuidanceStore,
    GuidanceRevisionRecord,
    Kind,
    Placement,
)
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.core.errors import InputValidationError
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring import AuthoredInputs, encode_inputs
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.execution import decode_send_request, encode_send_request
from app.stages.register.guidance import (
    DEFAULT_CHOICE,
    GUIDANCE_CHOICE_INVALID,
    SOURCE_PRODUCT,
    SOURCE_STORE,
    GuidanceChoice,
    GuidanceMode,
    PlacementChoice,
    resolve,
)
from app.stages.register.model import RegistrationConflictError
from app.stages.register.payload import build_payload
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.preparation import (
    PUBLICATION_GUIDANCE_UNPLACED,
    PreflightRequest,
    PreflightResult,
)
from app.stages.register.service import GUIDANCE_IMAGE_PATH
from tests.conftest import LOCAL
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections
from tests.support.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    ReadyItem,
    draft,
    establish,
    preparation,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
CLIENT = {"X-ICBM-Client": "pytest"}
TOP: dict[str, Any] = {"blocks": [{"heading": "당일발송", "lines": ["12시 이전 주문시 당일발송"]}]}
BOTTOM: dict[str, Any] = {
    "blocks": [
        {"heading": "배송 안내", "lines": ["○○택배로 발송됩니다"]},
        {"heading": "C/S 안내", "lines": ["평일 10:00 ~ 17:00"]},
    ]
}
HOLIDAY: dict[str, Any] = {"blocks": [{"heading": "휴무 안내", "lines": ["연휴 기간 출고 휴무"]}]}
OWN: dict[str, Any] = {
    "blocks": [{"heading": "이 상품 안내", "lines": ["  주문 제작 상품입니다  "]}]
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-dg4")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    # The application's own preflight owner reads the same sources, as a deployment configures.
    return preparation(container, account, served=True)


@pytest.fixture
def notices(container: Container, config: AppConfig, clock: FakeClock) -> DetailGuidanceStore:
    """The Detail Guidance owner over the application's database and the injected clock."""
    return DetailGuidanceStore(
        container.db,
        clock,
        AuditLog(container.db, clock),
        GuidanceImageStore(config.guidance_images_dir),
    )


@dataclass(frozen=True)
class Unit:
    item: ReadyItem
    draft_id: str
    request: PreflightRequest


@pytest.fixture
def unit(container: Container, sources: Collections, account: str) -> Unit:
    item = ready_item(container, sources, "1234")
    draft_id = draft(container.registrations, account, [item])
    return Unit(item, draft_id, request(container.registrations, draft_id, account, [item]))


def _guided(container: Container, prep: Preparation, store: DetailGuidanceStore) -> Preparation:
    """The test preflight owner of ``prep``, with the Detail Guidance owner wired."""
    service = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=prep.capability,
        metadata=prep.metadata,
        policies=prep.policies,
        guidance=store,
    )
    return Preparation(service, prep.capability, prep.metadata, prep.policies)


def _save(store: DetailGuidanceStore, **overrides: Any) -> GuidanceRevisionRecord:
    values: dict[str, Any] = {
        "placement": Placement.TOP,
        "kind": Kind.STANDING,
        "guidance_id": None,
        "template": Template.CLEAN,
        "content": TOP,
        "enabled": True,
        "starts_at": None,
        "ends_at": None,
        "expected_current_seq": None,
        "actor": OPERATOR,
        "correlation_id": CID,
    }
    values.update(overrides)
    return store.save(**values)


def _period(
    store: DetailGuidanceStore, start: timedelta, end: timedelta, **overrides: Any
) -> GuidanceRevisionRecord:
    return _save(
        store,
        kind=Kind.PERIOD,
        content=HOLIDAY,
        starts_at=T0 + start,
        ends_at=T0 + end,
        **overrides,
    )


def _codes(result: PreflightResult) -> set[str]:
    return set(result.codes)


def _store(revision: GuidanceRevisionRecord) -> dict[str, Any]:
    return {
        "source": SOURCE_STORE,
        "guidance_revision_id": revision.revision_id,
        "template": revision.template.value,
        "sha256": revision.image_sha256,
    }


def _top(result: PreflightResult) -> list[dict[str, Any]]:
    return [entry.canonical() for entry in result.resolved.guidance.top]


# ---------------------------------------------------------------- no notice: nothing changes


def test_a_unit_without_a_resolved_notice_is_exactly_what_it_was(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
) -> None:
    guided = _guided(container, prep, notices)
    before = prep.service.candidate(unit.request)
    now = guided.service.candidate(unit.request)
    assert now.resolved.guidance.empty
    assert (now.status, now.reasons) == (before.status, before.reasons)
    assert now.candidate_fingerprint == before.candidate_fingerprint
    assert now.dependencies == before.dependencies
    assert "guidance" not in now.dependencies
    # To the end: the same final fingerprint, the same payload and a Snapshot with no guidance.
    _req, final_before = ready_final(prep, unit.request)
    _req, final_now = ready_final(guided, unit.request)
    assert final_now.dependency_fingerprint == final_before.dependency_fingerprint
    payload_before, payload_now = build_payload(final_before), build_payload(final_now)
    assert payload_now.payload_digest == payload_before.payload_digest
    assert payload_now.payload["builder_version"] == "registration-payload/v1"
    assert "guidance" not in json.dumps(payload_now.payload)
    snapshot = RegistrationSnapshotBuilder(
        preflight=guided.service, registrations=container.registrations
    ).freeze(final_now, created_by=OPERATOR, correlation_id=CID)
    assert snapshot.payload_hash == payload_before.payload_digest
    frozen = container.registrations.snapshot_payload(snapshot.registration_snapshot_id) or {}
    assert "guidance" not in json.dumps(frozen) and "guidance_assets" not in frozen


def test_off_is_empty_and_never_blocks(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
) -> None:
    guided = _guided(container, prep, notices)
    before = prep.service.candidate(unit.request)
    _save(notices)
    _save(notices, placement=Placement.BOTTOM, content=BOTTOM, template=Template.DOMESTIC)
    off = PlacementChoice(GuidanceMode.OFF)
    request = replace(unit.request, guidance=GuidanceChoice(top=off, bottom=off))
    result = guided.service.candidate(request)
    assert result.resolved.guidance.empty
    assert PUBLICATION_GUIDANCE_UNPLACED not in result.codes
    assert result.candidate_fingerprint == before.candidate_fingerprint
    # OFF at one placement only: the other still resolves, and only it blocks.
    half = guided.service.candidate(replace(unit.request, guidance=GuidanceChoice(top=off)))
    assert half.resolved.guidance.top == ()
    assert [r.subject for r in half.reasons if r.code == PUBLICATION_GUIDANCE_UNPLACED] == [
        "guidance:bottom"
    ]


# ---------------------------------------------------------------- a standing notice


def test_a_standing_notice_resolves_changes_the_fingerprint_and_blocks_the_unit(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
) -> None:
    guided = _guided(container, prep, notices)
    before = guided.service.candidate(unit.request)
    standing = _save(notices)
    result = guided.service.candidate(unit.request)
    assert _top(result) == [_store(standing)]
    assert result.resolved.guidance.bottom == ()
    assert result.status is ReadinessStatus.BLOCKED
    assert _codes(result) - _codes(before) == {PUBLICATION_GUIDANCE_UNPLACED}
    blocked = [r for r in result.reasons if r.code == PUBLICATION_GUIDANCE_UNPLACED]
    assert [(r.status, r.subject) for r in blocked] == [(ReadinessStatus.BLOCKED, "guidance:top")]
    assert result.candidate_fingerprint != before.candidate_fingerprint
    assert result.dependencies["guidance"] == {"top": [_store(standing)], "bottom": []}
    assert not result.upload_permitted
    # A new revision is a new fingerprint.
    revised = _save(
        notices,
        guidance_id=standing.guidance_id,
        expected_current_seq=1,
        template=Template.MODERN,
    )
    again = guided.service.candidate(unit.request)
    assert _top(again) == [_store(revised)]
    assert again.candidate_fingerprint != result.candidate_fingerprint
    # Turning the standing notice off resolves nothing: the unit is what it was before.
    _save(
        notices,
        guidance_id=standing.guidance_id,
        expected_current_seq=2,
        template=Template.MODERN,
        enabled=False,
    )
    off = guided.service.candidate(unit.request)
    assert off.resolved.guidance.empty
    assert off.candidate_fingerprint == before.candidate_fingerprint
    assert off.reasons == before.reasons


def test_no_partial_send_a_blocked_unit_freezes_nothing(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
) -> None:
    guided = _guided(container, prep, notices)
    _req, final = ready_final(guided, unit.request)
    _save(notices)
    # The notice appeared after the READY final: the builder re-evaluates and refuses it.
    with pytest.raises(RegistrationConflictError) as refused:
        RegistrationSnapshotBuilder(
            preflight=guided.service, registrations=container.registrations
        ).freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert refused.value.code == "REGISTER_PREFLIGHT_STALE"
    assert container.registrations.snapshots_of_draft(unit.draft_id) == ()


# ---------------------------------------------------------------- periods (DG-04)


def test_period_notices_are_half_open_and_ordered_before_the_standing_notice(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
    clock: FakeClock,
) -> None:
    guided = _guided(container, prep, notices)
    standing = _save(notices)
    early = _period(notices, timedelta(minutes=30), timedelta(hours=4))
    first = _period(notices, timedelta(hours=1), timedelta(hours=3))
    second = _period(notices, timedelta(hours=1), timedelta(hours=2))
    ended = _period(notices, timedelta(hours=1), timedelta(hours=5))
    _period(
        notices,
        timedelta(hours=1),
        timedelta(hours=5),
        guidance_id=ended.guidance_id,
        expected_current_seq=1,
        enabled=False,
    )
    same_start = sorted([first, second], key=lambda r: r.guidance_id)

    def top() -> list[dict[str, Any]]:
        return _top(guided.service.candidate(unit.request))

    assert top() == [_store(standing)]
    clock.advance(30 * 60 - 0.000001)
    assert top() == [_store(standing)]
    clock.advance(0.000001)  # t == starts_at: included
    assert top() == [_store(early), _store(standing)]
    clock.advance(30 * 60)
    assert top() == [_store(early), *map(_store, same_start), _store(standing)]
    clock.advance(60 * 60 - 0.000001)  # one microsecond before the second ends
    assert top() == [_store(early), *map(_store, same_start), _store(standing)]
    clock.advance(0.000001)  # t == ends_at: excluded
    assert top() == [_store(early), _store(first), _store(standing)]
    clock.advance(2 * 60 * 60)
    assert top() == [_store(standing)]


def test_the_resolution_is_one_pure_rule() -> None:
    @dataclass(frozen=True)
    class Revision:
        revision_id: str
        guidance_id: str
        placement: str
        kind: str
        template: str
        enabled: bool
        starts_at: datetime | None
        ends_at: datetime | None
        image_sha256: str

    def period(name: str, start: int, end: int, enabled: bool = True) -> Revision:
        return Revision(
            f"r-{name}",
            name,
            "TOP",
            "PERIOD",
            "WARM",
            enabled,
            T0 + timedelta(hours=start),
            T0 + timedelta(hours=end),
            name[0] * 64,
        )

    standing = Revision("r-s", "s", "TOP", "STANDING", "CLEAN", True, None, None, "5" * 64)
    revisions = [period("b", 1, 2), period("a", 1, 2), period("c", 0, 1), standing]
    resolved = resolve(DEFAULT_CHOICE, T0 + timedelta(hours=1), revisions, "prep-rev-1")
    assert [e.guidance_revision_id for e in resolved.top] == ["r-a", "r-b", "r-s"]
    assert resolved.bottom == ()
    custom = PlacementChoice(GuidanceMode.CUSTOM, "MODERN", {"blocks": []}, "e" * 64)
    own = resolve(GuidanceChoice(top=custom), T0 + timedelta(hours=1), revisions, "prep-rev-1")
    assert [e.canonical() for e in own.top][-1] == {
        "source": SOURCE_PRODUCT,
        "preparation_revision_id": "prep-rev-1",
        "template": "MODERN",
        "sha256": "e" * 64,
    }
    assert [e.guidance_revision_id for e in own.top] == ["r-a", "r-b", None]
    off = GuidanceChoice(top=PlacementChoice(GuidanceMode.OFF))
    assert resolve(off, T0 + timedelta(hours=1), revisions, None).empty
    disabled = [replace(standing, enabled=False), period("a", 1, 2, enabled=False)]
    assert resolve(DEFAULT_CHOICE, T0 + timedelta(hours=1), disabled, None).empty


# ---------------------------------------------------------------- the product's own notice


def _authored(unit: Unit, guidance: GuidanceChoice) -> AuthoredInputs:
    req = unit.request
    return AuthoredInputs(req.category, req.listing, req.detail, guidance)


def test_a_custom_notice_is_rendered_stored_and_replaces_the_standing_one(
    container: Container,
    prep: Preparation,
    notices: DetailGuidanceStore,
    unit: Unit,
    clock: FakeClock,
) -> None:
    authoring = container.registration_preparations
    _save(notices)
    bottom = _save(notices, placement=Placement.BOTTOM, content=BOTTOM, template=Template.DOMESTIC)
    holiday = _period(notices, timedelta(hours=0), timedelta(hours=1))
    own = PlacementChoice(GuidanceMode.CUSTOM, "WARM", OWN, None)
    record = authoring.create(
        unit.draft_id,
        item_ids=[unit.item.item_id],
        inputs=_authored(unit, GuidanceChoice(top=own)),
        actor=OPERATOR,
    )
    stored = record.current.listing["guidance"]
    # The owner's validated text (trimmed) and the server's own rendering are recorded.
    assert stored["top"]["content"] == {
        "blocks": [{"heading": "이 상품 안내", "lines": ["주문 제작 상품입니다"]}]
    }
    assert stored["bottom"] == {"mode": "DEFAULT"}
    sha = stored["top"]["sha256"]
    assert hashlib.sha256(notices.image(sha)).hexdigest() == sha
    result = authoring.evaluate(record.preparation_id)
    assert _top(result) == [
        _store(holiday),
        {
            "source": SOURCE_PRODUCT,
            "preparation_revision_id": record.current.preparation_revision_id,
            "template": "WARM",
            "sha256": sha,
        },
    ]
    assert [e.canonical() for e in result.resolved.guidance.bottom] == [_store(bottom)]
    assert result.status is ReadinessStatus.BLOCKED
    assert PUBLICATION_GUIDANCE_UNPLACED in result.codes
    # The choice is read back in the shape a client sends; the image is never a client's.
    view = container.register.preparation(record.preparation_id).inputs.guidance
    assert view is not None and view.top.mode == "CUSTOM" and view.top.template == "WARM"
    assert view.bottom.mode == "DEFAULT"
    # A revision that changes something else keeps the choice; the entry names that revision.
    revised = authoring.update(
        record.preparation_id,
        item_ids=[unit.item.item_id],
        inputs=_authored(unit, GuidanceChoice(top=own)),
        actor=OPERATOR,
    )
    assert revised.current.listing["guidance"] == stored
    clock.advance(60 * 60)  # the holiday has ended
    again = authoring.evaluate(record.preparation_id)
    assert [e.preparation_revision_id for e in again.resolved.guidance.top] == [
        revised.current.preparation_revision_id
    ]
    assert again.candidate_fingerprint != result.candidate_fingerprint


def test_invalid_custom_text_or_template_is_refused_and_nothing_is_written(
    container: Container,
    prep: Preparation,
    unit: Unit,
) -> None:
    authoring = container.registration_preparations
    cases = [
        ({"blocks": [{"heading": "", "lines": ["https://example.com 방문"]}]}, "CLEAN"),
        ({"blocks": [{"heading": "", "lines": ["줄\u0007바꿈"]}]}, "CLEAN"),
        ({"blocks": []}, "CLEAN"),
    ]
    for content, template in cases:
        with pytest.raises(InputValidationError) as refused:
            authoring.create(
                unit.draft_id,
                item_ids=[unit.item.item_id],
                inputs=_authored(
                    unit,
                    GuidanceChoice(
                        bottom=PlacementChoice(GuidanceMode.CUSTOM, template, content, None)
                    ),
                ),
                actor=OPERATOR,
            )
        assert refused.value.code == GUIDANCE_TEXT_INVALID
    with pytest.raises(InputValidationError) as unknown:
        authoring.create(
            unit.draft_id,
            item_ids=[unit.item.item_id],
            inputs=_authored(
                unit, GuidanceChoice(top=PlacementChoice(GuidanceMode.CUSTOM, "NEON", OWN, None))
            ),
            actor=OPERATOR,
        )
    assert unknown.value.code == GUIDANCE_TEMPLATE_UNKNOWN
    assert container.registrations.preparations_of_draft(unit.draft_id) == ()


def test_a_default_choice_encodes_exactly_as_before_and_a_send_request_keeps_the_choice(
    unit: Unit,
) -> None:
    req = unit.request
    plain = encode_inputs(AuthoredInputs(req.category, req.listing, req.detail))
    assert "guidance" not in plain.listing
    off = GuidanceChoice(bottom=PlacementChoice(GuidanceMode.OFF))
    chosen = encode_inputs(AuthoredInputs(req.category, req.listing, req.detail, off))
    assert chosen.listing["guidance"] == {"top": {"mode": "DEFAULT"}, "bottom": {"mode": "OFF"}}
    assert chosen.fingerprint != plain.fingerprint
    default = encode_send_request(
        "intent-1", req, (), listing_identity="icbm-x", identity_generation=0
    )
    assert "guidance" not in default and "preparation_revision_id" not in default
    carried = encode_send_request(
        "intent-1",
        replace(req, guidance=off, preparation_revision_id="prep-rev-1"),
        (),
        listing_identity="icbm-x",
        identity_generation=0,
    )
    decoded, _assets = decode_send_request(carried)
    assert decoded.guidance == off and decoded.preparation_revision_id == "prep-rev-1"
    assert decode_send_request(default)[0].guidance == DEFAULT_CHOICE


# ---------------------------------------------------------------- the API read


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


def test_the_editor_reads_the_resolved_notices_and_saves_the_choice_through_the_api(
    api: TestClient, config: AppConfig
) -> None:
    from tests.support.register_support import CATEGORY, TAXONOMY

    served: Container = api.app.state.container
    account = establish(served, config, MARKET, "uid-market-dg4-api")
    preparation(served, account, served=True)
    item = ready_item(served, Collections.of(served, config), "1234")
    draft_id = draft(served.registrations, account, [item])
    assert GUIDANCE_IMAGE_PATH == IMAGE_PATH
    saved = api.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={"actor": OPERATOR, "placement": "TOP", "kind": "STANDING", "template": "CLEAN"}
        | {"content": TOP},
        headers=CLIENT,
    )
    assert saved.status_code == 200, saved.text
    # A unit not yet authored reads the store-wide defaults.
    default = api.get("/api/v1/register/detail-guidance").json()
    assert [p["placement"] for p in default["placements"]] == ["TOP", "BOTTOM"]
    top, bottom = default["placements"]
    assert top["choice"] == {"mode": "DEFAULT", "template": None, "content": None}
    assert [e["guidance_revision_id"] for e in top["entries"]] == [saved.json()["revision_id"]]
    assert top["unplaced"] is True and bottom == {
        "placement": "BOTTOM",
        "choice": {"mode": "DEFAULT", "template": None, "content": None},
        "entries": [],
        "unplaced": False,
    }
    inputs = {
        "category": {
            "category_id": CATEGORY,
            "mapping_revision": "mapping-test-1",
            "taxonomy_revision": TAXONOMY,
            "confirmation": "OPERATOR_CONFIRMED",
        },
        "name": {"value": "authored listing name"},
        "attributes": {"brand": {"value": "authored brand"}},
        "notices": {"manufacturer": {"value": "maker"}, "origin": {"detail_page_reference": True}},
        "detail_composition_revision": "detail-test-1",
        "detail_body": "authored body text",
        "detail_sections": ["BODY"],
        "guidance": {
            "top": {"mode": "OFF"},
            "bottom": {"mode": "CUSTOM", "template": "DOMESTIC", "content": BOTTOM},
        },
    }
    created = api.post(
        "/api/v1/register/preparations",
        json={"draft_id": draft_id, "item_ids": [item.item_id], "actor": OPERATOR}
        | {"inputs": inputs},
        headers=CLIENT,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["inputs"]["guidance"] == {
        "top": {"mode": "OFF", "template": None, "content": None},
        "bottom": {"mode": "CUSTOM", "template": "DOMESTIC", "content": BOTTOM},
    }
    read = api.get(
        "/api/v1/register/detail-guidance",
        params={"preparation_id": body["preparation_id"]},
    ).json()
    top, bottom = read["placements"]
    assert top["entries"] == [] and top["unplaced"] is False
    (own,) = bottom["entries"]
    assert own["source"] == SOURCE_PRODUCT and own["template"] == "DOMESTIC"
    image = api.get(own["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert hashlib.sha256(image.content).hexdigest() == own["sha256"]
    # The unit's preflight states the reason; the editor shows it from there.
    evaluated = api.post(
        f"/api/v1/register/preparations/{body['preparation_id']}/evaluate", headers=CLIENT
    ).json()
    assert PUBLICATION_GUIDANCE_UNPLACED in evaluated["preflight"]["reason_codes"]
    # A client cannot name a CUSTOM notice without its text, nor send an image of its own.
    for bad in (
        {"bottom": {"mode": "CUSTOM", "template": "DOMESTIC"}},
        {"bottom": {"mode": "LOUD"}},
    ):
        refused = api.post(
            f"/api/v1/register/preparations/{body['preparation_id']}",
            json={"item_ids": [item.item_id], "actor": OPERATOR}
            | {"inputs": inputs | {"guidance": bad}},
            headers=CLIENT,
        )
        assert refused.json()["error"]["code"] == GUIDANCE_CHOICE_INVALID, refused.text
    smuggled = api.post(
        f"/api/v1/register/preparations/{body['preparation_id']}",
        json={"item_ids": [item.item_id], "actor": OPERATOR}
        | {"inputs": inputs | {"guidance": {"bottom": {"mode": "OFF", "sha256": "a" * 64}}}},
        headers=CLIENT,
    )
    assert smuggled.status_code == 422
    invalid = api.post(
        f"/api/v1/register/preparations/{body['preparation_id']}",
        json={"item_ids": [item.item_id], "actor": OPERATOR}
        | {
            "inputs": inputs
            | {
                "guidance": {
                    "bottom": {
                        "mode": "CUSTOM",
                        "template": "CLEAN",
                        "content": {"blocks": [{"heading": "", "lines": ["<b>굵게</b>"]}]},
                    }
                }
            }
        },
        headers=CLIENT,
    )
    assert invalid.json()["error"]["code"] == GUIDANCE_TEXT_INVALID
    assert served.registrations.preparation(body["preparation_id"]).current.revision_no == 1
