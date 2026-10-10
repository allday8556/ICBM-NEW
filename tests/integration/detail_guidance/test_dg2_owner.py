"""ADR-0033 G2: the Detail Guidance owner — its migration, store, image store and settings API.

A save validates the operator's plain text, renders it with the one renderer, stores the PNG by its
SHA-256 and appends one audited revision in one write unit. Nothing is updated or deleted: turning
a notice off, or ending a period early, appends a revision (DG-03). A period's status is judged by
the injected clock over the half-open ``[starts_at, ends_at)`` (DG-04). The preview stores nothing.
"""

import base64
import contextlib
import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from PIL import Image

from app.capabilities.audit.models import AuditEventType
from app.capabilities.audit.service import AuditLog
from app.capabilities.detail_guidance import renderer
from app.capabilities.detail_guidance.content import GUIDANCE_TEXT_INVALID
from app.capabilities.detail_guidance.image_store import (
    GUIDANCE_IMAGE_CORRUPT,
    GUIDANCE_IMAGE_MISSING,
    GUIDANCE_IMAGE_PATH_CONFLICT,
    GuidanceImageIntegrityError,
    GuidanceImageStore,
)
from app.capabilities.detail_guidance.presets import PRESETS, PRESETS_VERSION
from app.capabilities.detail_guidance.renderer import (
    GUIDANCE_TEXT_TOO_WIDE,
    RENDERER_VERSION,
    Template,
)
from app.capabilities.detail_guidance.store import (
    GUIDANCE_CURRENT_MOVED,
    GUIDANCE_IDENTITY_MISMATCH,
    GUIDANCE_PERIOD_INVALID,
    GUIDANCE_UNCHANGED,
    DetailGuidanceConflictError,
    DetailGuidanceStore,
    GuidanceRevisionRecord,
    GuidanceStatus,
    Kind,
    Placement,
)
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.migrate import alembic_config, upgrade_to_head
from tests.support.jobs_support import FakeClock

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
TABLES = ("detail_guidances", "guidance_image_artifacts", "detail_guidance_revisions")
BEFORE = "0057_m65_dispatch_stage"
TOP: dict[str, Any] = {"blocks": [{"heading": "당일발송", "lines": ["12시 이전 주문시 당일발송"]}]}
BOTTOM: dict[str, Any] = {
    "blocks": [
        {"heading": "배송 안내", "lines": ["○○택배로 발송됩니다"]},
        {"heading": "C/S 안내", "lines": ["평일 10:00 ~ 17:00"]},
    ]
}


@pytest.fixture
def world(
    container: Container, config: AppConfig
) -> Iterator[tuple[DetailGuidanceStore, FakeClock]]:
    clock = FakeClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    store = DetailGuidanceStore(
        container.db,
        clock,
        AuditLog(container.db, clock),
        GuidanceImageStore(config.guidance_images_dir),
    )
    yield store, clock


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
        "actor": "operator",
        "correlation_id": "cid-dg2",
    }
    values.update(overrides)
    return store.save(**values)


def _counts(config: AppConfig) -> list[int]:
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        return [raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES]


def _events(config: AppConfig) -> list[tuple[str, str, str, str, str]]:
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        return raw.execute(
            "SELECT action, target_ref, COALESCE(before_json, ''), after_json, details_json"
            " FROM audit_events WHERE event_type = ? ORDER BY seq",
            (AuditEventType.DETAIL_GUIDANCE_REVISED.value,),
        ).fetchall()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ------------------------------------------------------------------ the store


def test_a_standing_save_creates_its_identity_revision_one_its_image_and_its_bytes(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, clock = world
    assert _counts(config) == [0, 0, 0]
    record = _save(store)
    assert (record.seq, record.kind, record.placement, record.template) == (
        1,
        Kind.STANDING,
        Placement.TOP,
        Template.CLEAN,
    )
    assert record.enabled and record.starts_at is None and record.ends_at is None
    assert record.authored_at == clock.now() and record.authored_by == "operator"
    assert record.content == TOP
    expected = hashlib.sha256(
        json.dumps(TOP, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert record.content_fingerprint == expected
    assert _counts(config) == [1, 1, 1]
    # The bytes are the renderer's, at their content address, and decode to the recorded size.
    path = config.guidance_images_dir / "sha256" / record.image_sha256[:2] / record.image_sha256
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == record.image_sha256
    assert data == store.image(record.image_sha256)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (record.image_width, record.image_height)
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        artifact = raw.execute(
            "SELECT width, height, byte_size, renderer_version, template, font_sha256"
            " FROM guidance_image_artifacts"
        ).fetchone()
    assert artifact == (
        renderer.CANVAS_WIDTH,
        record.image_height,
        len(data),
        RENDERER_VERSION,
        "CLEAN",
        renderer.FONT_SHA256,
    )


def test_a_second_save_appends_the_next_revision_and_turning_off_appends_one_too(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, _ = world
    first = _save(store)
    # The placement's standing notice is found without its id, at the sequence it was read.
    second = _save(store, template=Template.WARM, expected_current_seq=1)
    assert (second.guidance_id, second.seq) == (first.guidance_id, 2)
    assert second.image_sha256 != first.image_sha256
    off = _save(
        store,
        guidance_id=first.guidance_id,
        template=Template.WARM,
        enabled=False,
        expected_current_seq=2,
    )
    assert (off.seq, off.enabled, off.image_sha256) == (3, False, second.image_sha256)
    # The disabled revision reuses the WARM image: one artifact row per distinct rendering.
    assert _counts(config) == [1, 2, 3]
    assert [r.seq for r in store.history(first.guidance_id)] == [3, 2, 1]
    current = store.current()
    top = next(p for p in current.placements if p.placement is Placement.TOP)
    assert top.standing is not None and top.standing.record.revision_id == off.revision_id
    assert top.standing.status is None and top.periods == ()


def test_an_unchanged_save_and_a_stale_read_are_refused_and_write_nothing(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, _ = world
    first = _save(store)
    cases = (
        ({"expected_current_seq": 1}, GUIDANCE_UNCHANGED),
        # The notice exists now: a save that read none, or another sequence, is stale.
        ({"expected_current_seq": None, "template": Template.MODERN}, GUIDANCE_CURRENT_MOVED),
        ({"expected_current_seq": 2, "template": Template.MODERN}, GUIDANCE_CURRENT_MOVED),
        # A new period notice has no revision yet.
        (
            {
                "kind": Kind.PERIOD,
                "starts_at": datetime(2026, 10, 11, tzinfo=UTC),
                "ends_at": datetime(2026, 10, 12, tzinfo=UTC),
                "expected_current_seq": 1,
            },
            GUIDANCE_CURRENT_MOVED,
        ),
    )
    for overrides, code in cases:
        with pytest.raises(DetailGuidanceConflictError) as caught:
            _save(store, **overrides)
        assert caught.value.code == code, overrides
    # Whitespace is trimmed before the fingerprint, so padded text is the same notice.
    padded = {"blocks": [{"heading": " 당일발송 ", "lines": ["  12시 이전 주문시 당일발송"]}]}
    with pytest.raises(DetailGuidanceConflictError) as caught:
        _save(store, content=padded, expected_current_seq=1)
    assert caught.value.code == GUIDANCE_UNCHANGED
    assert _counts(config) == [1, 1, 1]
    assert [r.seq for r in store.history(first.guidance_id)] == [1]


def test_text_the_renderer_refuses_is_refused_before_anything_is_written(
    world: tuple[DetailGuidanceStore, FakeClock],
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = world
    with pytest.raises(InputValidationError) as caught:
        _save(store, content={"blocks": [{"heading": "", "lines": ["줄\u0007바꿈"]}]})
    assert caught.value.code == GUIDANCE_TEXT_INVALID
    monkeypatch.setattr(renderer, "TEXT_BOX_WIDTH", 100)
    with pytest.raises(InputValidationError) as caught:
        _save(store)
    assert caught.value.code == GUIDANCE_TEXT_TOO_WIDE
    assert _counts(config) == [0, 0, 0]
    assert _files(config.guidance_images_dir) == []
    assert _events(config) == []


@pytest.mark.parametrize(
    ("kind", "starts_at", "ends_at"),
    [
        (Kind.PERIOD, None, None),
        (Kind.PERIOD, datetime(2026, 10, 11, tzinfo=UTC), None),
        # A naive time is no instant.
        (Kind.PERIOD, datetime(2026, 10, 11), datetime(2026, 10, 12)),
        # Half-open: an empty or a reversed period is no period.
        (Kind.PERIOD, datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC)),
        (Kind.PERIOD, datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC)),
        # A standing notice has none.
        (Kind.STANDING, datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC)),
        (Kind.STANDING, None, datetime(2026, 10, 12, tzinfo=UTC)),
    ],
)
def test_a_period_notice_needs_a_half_open_period_and_a_standing_notice_refuses_one(
    world: tuple[DetailGuidanceStore, FakeClock],
    config: AppConfig,
    kind: Kind,
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> None:
    store, _ = world
    with pytest.raises(InputValidationError) as caught:
        _save(store, kind=kind, starts_at=starts_at, ends_at=ends_at)
    assert caught.value.code == GUIDANCE_PERIOD_INVALID
    assert _counts(config) == [0, 0, 0]


def test_a_period_is_stored_in_utc_and_each_period_is_its_own_notice(
    world: tuple[DetailGuidanceStore, FakeClock],
) -> None:
    store, _ = world
    seoul = timezone(timedelta(hours=9))
    holiday = _save(
        store,
        kind=Kind.PERIOD,
        placement=Placement.BOTTOM,
        content=BOTTOM,
        starts_at=datetime(2026, 10, 12, 9, 0, tzinfo=seoul),
        ends_at=datetime(2026, 10, 14, 0, 0, tzinfo=seoul),
    )
    assert holiday.starts_at == datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    assert holiday.ends_at == datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
    earlier = _save(
        store,
        kind=Kind.PERIOD,
        placement=Placement.BOTTOM,
        starts_at=datetime(2026, 10, 11, tzinfo=UTC),
        ends_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    assert earlier.guidance_id != holiday.guidance_id and earlier.seq == 1
    bottom = next(p for p in store.current().placements if p.placement is Placement.BOTTOM)
    assert bottom.standing is None
    # Ordered by (starts_at, guidance_id), as the resolution will be (ADR-0033 §6).
    assert [n.record.guidance_id for n in bottom.periods] == [
        earlier.guidance_id,
        holiday.guidance_id,
    ]


def test_a_placement_has_one_standing_notice_and_a_notice_keeps_its_kind(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, _ = world
    top = _save(store)
    bottom = _save(store, placement=Placement.BOTTOM, content=BOTTOM)
    assert top.guidance_id != bottom.guidance_id
    period = _save(
        store,
        kind=Kind.PERIOD,
        starts_at=datetime(2026, 10, 11, tzinfo=UTC),
        ends_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    # A notice keeps its placement and kind.
    for overrides in (
        {"guidance_id": period.guidance_id, "expected_current_seq": 1},
        {"guidance_id": top.guidance_id, "placement": Placement.BOTTOM, "expected_current_seq": 1},
    ):
        with pytest.raises(InputValidationError) as caught:
            _save(store, template=Template.MODERN, **overrides)
        assert caught.value.code == GUIDANCE_IDENTITY_MISMATCH
    with pytest.raises(NotFoundError):
        _save(store, guidance_id="no-such-notice", expected_current_seq=1)
    # The database refuses a second standing identity of a placement, by any path.
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO detail_guidances VALUES ('second', 'TOP', 'STANDING',"
                " '2026-10-10 00:00:00')"
            )
        raw.execute(
            "INSERT INTO detail_guidances VALUES ('another-period', 'TOP', 'PERIOD',"
            " '2026-10-10 00:00:00')"
        )
        raw.rollback()
    assert _counts(config) == [3, 2, 3]


def test_the_database_refuses_to_rewrite_drop_or_misnumber_any_row(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, _ = world
    record = _save(store)
    period = _save(
        store,
        kind=Kind.PERIOD,
        starts_at=datetime(2026, 10, 11, tzinfo=UTC),
        ends_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    sha = record.image_sha256
    columns = (
        "revision_id, guidance_id, seq, kind, template, content_json, content_fingerprint,"
        " enabled, starts_at, ends_at, image_sha256, authored_by, correlation_id, authored_at"
    )

    def revision(
        seq: int,
        *,
        guidance: str = record.guidance_id,
        kind: str = "STANDING",
        template: str = "CLEAN",
        period: str = "NULL, NULL",
    ) -> str:
        return (
            f"INSERT INTO detail_guidance_revisions ({columns}) VALUES ('r-{seq}', '{guidance}',"
            f" {seq}, '{kind}', '{template}', '{{\"blocks\":[]}}', '{'0' * 64}', 1, {period},"
            f" '{sha}', 'operator', 'cid', '2026-10-10 00:00:00')"
        )

    statements = (
        ("UPDATE detail_guidances SET placement = 'BOTTOM'", "never updated"),
        ("DELETE FROM detail_guidances", "never deleted"),
        ("UPDATE guidance_image_artifacts SET height = 1", "never updated"),
        ("DELETE FROM guidance_image_artifacts", "never deleted"),
        ("UPDATE detail_guidance_revisions SET enabled = 0", "never updated"),
        ("DELETE FROM detail_guidance_revisions", "never deleted"),
        # A gap in the numbering, and a number taken twice.
        (revision(3), "follows the one before it"),
        (revision(1), "follows the one before it"),
        # A kind that is not the identity's, and a template that is not the image's.
        (
            revision(2, kind="PERIOD", period="'2026-10-11 00:00:00', '2026-10-12 00:00:00'"),
            "the kind of its notice",
        ),
        (revision(2, template="WARM"), "an image of its template"),
        # A period revision without a period, and a reversed one.
        (revision(2, guidance=period.guidance_id, kind="PERIOD"), "period_matches_kind"),
        (
            revision(
                2,
                guidance=period.guidance_id,
                kind="PERIOD",
                period="'2026-10-12 00:00:00', '2026-10-11 00:00:00'",
            ),
            "period_matches_kind",
        ),
        # A template outside the five official ones.
        (
            "INSERT INTO guidance_image_artifacts VALUES ('" + "a" * 64 + "', 1, 1, 1, 'v',"
            " 'FANCY', '" + "b" * 64 + "', '2026-10-10 00:00:00')",
            "template_known",
        ),
    )
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        for statement, refusal in statements:
            with pytest.raises(sqlite3.DatabaseError, match=refusal):
                raw.execute(statement)
        # The well-formed next revision is accepted, so each refusal above was its own rule.
        raw.execute(revision(2))
        raw.rollback()
    assert _counts(config) == [2, 1, 2]


def test_a_period_status_is_judged_by_the_injected_clock_over_a_half_open_period(
    world: tuple[DetailGuidanceStore, FakeClock],
) -> None:
    store, clock = world
    start = clock.now() + timedelta(seconds=60)
    record = _save(store, kind=Kind.PERIOD, starts_at=start, ends_at=start + timedelta(seconds=60))

    def status() -> GuidanceStatus | None:
        top = next(p for p in store.current().placements if p.placement is Placement.TOP)
        (notice,) = top.periods
        return notice.status

    assert status() is GuidanceStatus.SCHEDULED
    clock.advance(59)
    assert status() is GuidanceStatus.SCHEDULED
    clock.advance(1)  # t == starts_at
    assert status() is GuidanceStatus.ACTIVE
    clock.advance(59)
    assert status() is GuidanceStatus.ACTIVE
    clock.advance(1)  # t == ends_at
    assert status() is GuidanceStatus.ENDED
    # Ending a scheduled period early appends a disabled revision; it is ENDED at once.
    later = clock.now() + timedelta(hours=1)
    other = _save(store, kind=Kind.PERIOD, starts_at=later, ends_at=later + timedelta(hours=1))
    ended = _save(
        store,
        kind=Kind.PERIOD,
        guidance_id=other.guidance_id,
        enabled=False,
        starts_at=later,
        ends_at=later + timedelta(hours=1),
        expected_current_seq=1,
    )
    assert ended.seq == 2
    top = next(p for p in store.current().placements if p.placement is Placement.TOP)
    assert {n.record.guidance_id: n.status for n in top.periods} == {
        record.guidance_id: GuidanceStatus.ENDED,
        other.guidance_id: GuidanceStatus.ENDED,
    }


def test_every_save_appends_one_audit_event_that_never_holds_the_text(
    world: tuple[DetailGuidanceStore, FakeClock], config: AppConfig
) -> None:
    store, _ = world
    first = _save(store)
    second = _save(store, enabled=False, expected_current_seq=1)
    events = _events(config)
    assert [(action, target) for action, target, *_ in events] == [
        ("DETAIL_GUIDANCE_CREATED", first.guidance_id),
        ("DETAIL_GUIDANCE_REVISED", first.guidance_id),
    ]
    _, _, before, after, details = events[1]
    assert json.loads(before)["seq"] == 1 and json.loads(after)["seq"] == 2
    assert json.loads(after)["enabled"] is False
    assert json.loads(after)["image_sha256"] == second.image_sha256
    assert json.loads(details) == {"placement": "TOP", "kind": "STANDING"}
    for row in events:
        assert "당일발송" not in "".join(row)


# ------------------------------------------------------------------ the byte store


def test_the_byte_store_is_content_addressed_atomic_and_fails_closed(tmp_path: Path) -> None:
    store = GuidanceImageStore(tmp_path / "guidance")
    data = b"\x89PNG not really"
    sha = store.put(data)
    assert sha == hashlib.sha256(data).hexdigest()
    path = tmp_path / "guidance" / "sha256" / sha[:2] / sha
    assert path.read_bytes() == data
    # Equal bytes are reused; no staging file is left behind.
    assert store.put(data) == sha
    assert _files(tmp_path / "guidance") == [path]
    # Another object at an address is left untouched and refuses the write.
    other = b"other bytes"
    occupied = store.path(hashlib.sha256(other).hexdigest())
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(b"tampered")
    with pytest.raises(GuidanceImageIntegrityError) as caught:
        store.put(other)
    assert caught.value.code == GUIDANCE_IMAGE_PATH_CONFLICT
    assert occupied.read_bytes() == b"tampered"
    # A read verifies the checksum again.
    with pytest.raises(GuidanceImageIntegrityError) as caught:
        store.get(hashlib.sha256(other).hexdigest())
    assert caught.value.code == GUIDANCE_IMAGE_CORRUPT
    with pytest.raises(GuidanceImageIntegrityError) as caught:
        store.get("c" * 64)
    assert caught.value.code == GUIDANCE_IMAGE_MISSING
    # Only a SHA-256 names a file: nothing outside the store is reachable.
    for name in ("../../etc", "C" * 64, "c" * 63):
        with pytest.raises(NotFoundError):
            store.get(name)


# ------------------------------------------------------------------ the settings API


def test_the_settings_api_lists_the_notices_templates_and_presets(client: TestClient) -> None:
    body = client.get("/api/v1/settings/detail-guidance", headers=CLIENT).json()
    assert body["renderer_version"] == RENDERER_VERSION
    datetime.fromisoformat(body["now"])
    assert [p["placement"] for p in body["placements"]] == ["TOP", "BOTTOM"]
    assert all(p["standing"] is None and p["periods"] == [] for p in body["placements"])
    assert [(t["name"], t["label"]) for t in body["templates"]] == [
        ("CLEAN", "클린"),
        ("MODERN", "모던"),
        ("WARM", "웜"),
        ("DOMESTIC", "국내배송"),
        ("OVERSEAS", "해외배송"),
    ]
    assert body["presets"] == [
        {
            "key": preset.key,
            "label": preset.label,
            "template": preset.template.value,
            "top": preset.top,
            "bottom": preset.bottom,
            "version": PRESETS_VERSION,
        }
        for preset in PRESETS
    ]


def test_the_settings_api_saves_a_revision_and_serves_only_recorded_images(
    client: TestClient, config: AppConfig
) -> None:
    request = {
        "actor": "operator",
        "placement": "BOTTOM",
        "kind": "STANDING",
        "template": "DOMESTIC",
        "content": BOTTOM,
    }
    # A state-changing request without the client header is refused before the owner.
    refused = client.post("/api/v1/settings/detail-guidance/revisions", json=request)
    assert (refused.status_code, refused.json()["error"]["code"]) == (
        403,
        "CLIENT_HEADER_REQUIRED",
    )
    assert _counts(config) == [0, 0, 0]
    saved = client.post("/api/v1/settings/detail-guidance/revisions", json=request, headers=CLIENT)
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert (view["seq"], view["template"], view["status"], view["enabled"]) == (
        1,
        "DOMESTIC",
        None,
        True,
    )
    assert view["image_url"] == f"/api/v1/settings/detail-guidance/images/{view['image_sha256']}"
    listed = client.get("/api/v1/settings/detail-guidance", headers=CLIENT).json()
    bottom = listed["placements"][1]
    assert bottom["standing"]["revision_id"] == view["revision_id"]
    image = client.get(view["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert hashlib.sha256(image.content).hexdigest() == view["image_sha256"]
    for unknown in ("d" * 64, "not-a-sha"):
        missing = client.get(f"/api/v1/settings/detail-guidance/images/{unknown}")
        assert missing.status_code == 404
    # A stale or unchanged save, and an invalid period, keep the envelope's codes.
    again = client.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={**request, "expected_current_seq": 1},
        headers=CLIENT,
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, GUIDANCE_UNCHANGED)
    stale = client.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={**request, "template": "WARM"},
        headers=CLIENT,
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, GUIDANCE_CURRENT_MOVED)
    naive = client.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={
            **request,
            "kind": "PERIOD",
            "starts_at": "2026-10-11T00:00:00",
            "ends_at": "2026-10-12T00:00:00",
        },
        headers=CLIENT,
    )
    assert (naive.status_code, naive.json()["error"]["code"]) == (422, GUIDANCE_PERIOD_INVALID)
    period = client.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={
            **request,
            "kind": "PERIOD",
            "starts_at": "2020-01-01T09:00:00+09:00",
            "ends_at": "2999-01-01T00:00:00Z",
        },
        headers=CLIENT,
    )
    assert period.status_code == 200, period.text
    assert (period.json()["status"], period.json()["status_label"]) == ("ACTIVE", "진행 중")
    assert _counts(config) == [2, 1, 2]


def test_the_preview_renders_the_five_templates_and_stores_nothing(
    client: TestClient, config: AppConfig
) -> None:
    before = (_counts(config), _files(config.guidance_images_dir))
    response = client.post(
        "/api/v1/settings/detail-guidance/preview", json={"content": TOP}, headers=CLIENT
    )
    assert response.status_code == 200, response.text
    previews = response.json()
    assert [p["template"] for p in previews] == [t.value for t in Template]
    for item in previews:
        png = base64.b64decode(item["png_base64"])
        assert hashlib.sha256(png).hexdigest() == item["sha256"]
        with Image.open(io.BytesIO(png)) as image:
            assert image.size == (item["width"], item["height"])
    assert (_counts(config), _files(config.guidance_images_dir)) == before
    bad = client.post(
        "/api/v1/settings/detail-guidance/preview",
        json={"content": {"blocks": []}},
        headers=CLIENT,
    )
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, GUIDANCE_TEXT_INVALID)
    # A preview is no save: none of its images is served.
    shown = client.get(f"/api/v1/settings/detail-guidance/images/{previews[0]['sha256']}")
    assert shown.status_code == 404


# ------------------------------------------------------------------ the migration


def test_0058_is_additive_starts_empty_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def tables() -> set[str]:
        with contextlib.closing(sqlite3.connect(database)) as raw:
            return {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    before = tables()
    assert set(TABLES) <= before
    with contextlib.closing(sqlite3.connect(database)) as raw:
        assert [raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES] == [0] * 3
    # Empty, it steps down to exactly the tables it added, and back up.
    command.downgrade(alembic_config(url), BEFORE)
    assert before - tables() == set(TABLES)
    command.upgrade(alembic_config(url), "head")
    assert tables() == before
    sha = "e" * 64
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute("INSERT INTO detail_guidances VALUES ('g-1', 'TOP', 'STANDING', '2026-10-10')")
        raw.execute(
            "INSERT INTO guidance_image_artifacts VALUES"
            f" ('{sha}', 860, 100, 10, 'guidance-renderer/v1', 'CLEAN', '{'f' * 64}', '2026-10-10')"
        )
        raw.execute(
            "INSERT INTO detail_guidance_revisions VALUES ('r-1', 'g-1', 1, 'STANDING', 'CLEAN',"
            f" '{{}}', '{'0' * 64}', 1, NULL, NULL, '{sha}', 'operator', 'cid', '2026-10-10')"
        )
        raw.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    assert tables() == before
    with contextlib.closing(sqlite3.connect(database)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM detail_guidance_revisions").fetchone()[0] == 1
