"""ADR-0041 I5: the Studio panel's routes over the served application (§6, §7; IT-01..IT-10).

The application runs with its real worker. Production binds no OCR engine and no approved AI
profile, so the capability reads NOT_CONFIGURED and a run is refused before anything is written.
``make_ready`` then swaps in the fake OCR engine and answers the application's own profiled
provider through the AIS-1 fakes: the whole path runs — the job, the candidate store, the edit
revisions, the renderer and the M4 image owner's derivation, QA and selection — with nothing sent.
"""

import contextlib
import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.ai.prompts import ReviseRequest
from app.capabilities.image_studio.task import TASK_KEY
from app.capabilities.image_studio.translation import TRANSLATE_JOB
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.products.image_model import ImageAssetKind, SourceDecisionKind
from tests.conftest import LOCAL
from tests.support.image_studio_support import (
    OPERATOR,
    TEXTS,
    StudioProduct,
    make_ready,
    studio_product,
)
from tests.support.onboarding_support import wait_until
from tests.support.product_support import count, raw

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/image-studio"
STUDIO_TABLES = ("image_ocr_observations", "image_translation_candidates", "image_edit_revisions")
IMAGE_TABLES = ("derived_image_derivations", "image_qa_results", "image_selection_revisions")


@pytest.fixture
def client(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as test_client:
        yield test_client


@pytest.fixture
def container(client: TestClient) -> Container:
    served: Container = client.app.state.container  # type: ignore[attr-defined]
    return served


@pytest.fixture
def item(container: Container, config: AppConfig) -> StudioProduct:
    return studio_product(container, config)


def _image(item: StudioProduct, tail: str = "") -> str:
    return f"{BASE}/items/{item.item_id}/images/{item.detail}{tail}"


def _run(client: TestClient, container: Container, item: StudioProduct) -> str:
    response = client.post(
        f"{BASE}/products/{item.group}/translations",
        json={"images": [item.detail], "source_language": "zh", "actor": OPERATOR},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    job_id = str(response.json()["job_id"])
    wait_until(lambda: container.jobs.get(job_id).state in ("SUCCEEDED", "DEAD"))
    assert container.jobs.get(job_id).state == "SUCCEEDED"
    return job_id


def _state(client: TestClient, item: StudioProduct) -> dict[str, Any]:
    response = client.get(_image(item))
    assert response.status_code == 200, response.json()
    state: dict[str, Any] = response.json()
    return state


def _draft(state: dict[str, Any], **texts: str | None) -> list[dict[str, Any]]:
    """Every region: those named in ``texts`` included with that text (``None``: its
    suggestion), the others kept."""
    items = []
    for region in state["regions"]:
        rid = region["region_id"]
        if rid in texts:
            text = texts[rid]
            items.append(
                {
                    "region_id": rid,
                    "include": True,
                    "text": region["suggestion"] if text is None else text,
                    "erase": region["default_erase"],
                }
            )
        else:
            items.append({"region_id": rid, "include": False})
    return items


def _save(
    client: TestClient, item: StudioProduct, state: dict[str, Any], **texts: str | None
) -> Any:
    return client.post(
        _image(item, "/revisions"),
        json={
            "observation_id": state["observation_id"],
            "source_language": "zh",
            "items": _draft(state, **texts),
            "expected_seq": state["expected_seq"],
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )


def _apply(client: TestClient, item: StudioProduct, revision_id: str, confirmed: list[str]) -> Any:
    return client.post(
        _image(item, "/apply"),
        json={
            "revision_id": revision_id,
            "source_language": "zh",
            "confirmed": confirmed,
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )


def _dump(config: AppConfig) -> str:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute("SELECT * FROM audit_events").fetchall()
    return "\n".join(str(row) for row in rows)


def _source_untouched(config: AppConfig, sha: str) -> None:
    stored = config.source_assets_dir / sha[:2] / sha
    files = [stored] if stored.is_file() else list(config.source_assets_dir.rglob(sha + "*"))
    assert files, "the source asset is stored"
    for path in files:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


# ---------------------------------------------------------------- production: NOT_CONFIGURED


def test_production_reads_not_configured_and_refuses_a_run_before_any_write(
    client: TestClient, container: Container, config: AppConfig, item: StudioProduct
) -> None:
    capability = client.get(f"{BASE}/capability").json()
    assert capability["status"] == "NOT_CONFIGURED"
    assert capability["missing"] == ["ocr", "ai provider"]
    assert capability["max_images_per_run"] == 30 and capability["languages"] == ["zh", "ja"]
    body = {"images": [item.detail], "source_language": "zh", "actor": OPERATOR}
    # A write without the client header never reaches the owner.
    refused = client.post(f"{BASE}/products/{item.group}/translations", json=body)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "CLIENT_HEADER_REQUIRED"
    refused = client.post(f"{BASE}/products/{item.group}/translations", json=body, headers=HEADERS)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "IMAGE_TRANSLATE_NOT_CONFIGURED"
    assert count(config, "jobs", "job_type = ?", TRANSLATE_JOB) == 0
    # The panel's read works and finds nothing read: no engine, no observation.
    state = _state(client, item)
    assert state["observation_id"] is None and state["regions"] == []
    assert state["selection"]["decision"] == "USE_SOURCE"
    images = client.get(f"{BASE}/items/{item.item_id}/images").json()
    assert [(i["role"], i["observed"]) for i in images["images"]] == [
        ("REPRESENTATIVE", False),
        ("DETAIL", False),
    ]
    assert all(count(config, table) == 0 for table in STUDIO_TABLES)


# ---------------------------------------------------------------- the whole round trip


def test_run_review_preview_save_apply_qa_select_and_revert(
    client: TestClient,
    container: Container,
    config: AppConfig,
    item: StudioProduct,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    ocr, sidecar = make_ready(container, tmp_path)
    assert client.get(f"{BASE}/capability").json() == {
        "key": "image_translate",
        "status": "READY",
        "missing": [],
        "languages": ["zh", "ja"],
        "max_images_per_run": 30,
    }
    _run(client, container, item)
    # One text request: the contact detail is kept by default and never sent; no image travels.
    (sent,) = sidecar.texts
    assert "shop8888" not in sent and item.detail not in sent
    state = _state(client, item)
    regions = {r["region_id"]: r for r in state["regions"]}
    assert state["candidate"]["status"] == "FRESH"
    assert {rid: r["flags"] for rid, r in regions.items()} == {
        "r0": [],
        "r1": [],
        "r2": ["CONTACT_DETAIL"],
        "r3": ["LOW_CONFIDENCE"],
        "r4": ["SCRIPT_MISMATCH", "COMPLEX_BACKGROUND"],
    }
    # A flagged region, the contact detail and the complex background are kept by default.
    assert [rid for rid, r in regions.items() if r["default_include"]] == ["r0", "r1"]
    assert regions["r2"]["suggestion"] is None and regions["r0"]["suggestion"] == "순수 천연 참기름"
    assert (regions["r0"]["default_erase"], regions["r4"]["default_erase"]) == ("ERASE", "OVERLAY")
    assert state["edit"] is None and state["expected_seq"] == 0
    listed = client.get(f"{BASE}/items/{item.item_id}/images").json()["images"]
    assert [i["observed"] for i in listed] == [False, True]

    # A preview renders and stores nothing; it names the draft's own flags.
    before = {t: count(config, t) for t in (*STUDIO_TABLES, *IMAGE_TABLES, "audit_events")}
    preview = client.post(
        _image(item, "/preview"),
        json={
            "observation_id": state["observation_id"],
            "source_language": "zh",
            "items": _draft(state, r0=None, r1="용량 대용량"),
        },
        headers=HEADERS,
    )
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    review = json.loads(preview.headers["x-image-studio-review"])
    assert review["complete"] is True and review["failures"] == []
    # The edited text dropped 350ml: that region is flagged now.
    assert review["flags"]["r1"] == ["NUMBER_UNIT_DROPPED"] and review["flags"]["r0"] == []
    assert {t: count(config, t) for t in before} == before

    # Save: the suggestion unchanged is the AI's; an edited text is the operator's.
    saved = _save(client, item, state, r0=None, r1="용량 350ml 대용량")
    assert saved.status_code == 200, saved.json()
    revision = saved.json()
    assert revision["seq"] == 1 and revision["candidate_id"] == state["candidate"]["candidate_id"]
    origins = {i["region_id"]: (i["include"], i["origin"]) for i in revision["items"]}
    assert origins["r0"] == (True, "AI_SUGGESTION") and origins["r1"] == (
        True,
        "OPERATOR_CONFIRMED",
    )
    assert origins["r2"] == (False, None)
    # A save against a moved lineage is refused.
    moved = _save(client, item, state, r0=None)
    assert moved.status_code == 409 and moved.json()["error"]["code"] == "IMAGE_EDIT_MOVED"

    # A flagged region needs its own confirmation: r3 included without it is refused.
    state = _state(client, item)
    assert state["edit"]["seq"] == 1 and state["expected_seq"] == 1
    second = _save(client, item, state, r0=None, r1="용량 350ml 대용량", r3=None).json()
    refused = _apply(client, item, second["revision_id"], [])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "IMAGE_STUDIO_FLAGGED_UNCONFIRMED"
    assert refused.json()["error"]["details"] == {
        "regions": ["r3"],
        "flags": {"r3": ["LOW_CONFIDENCE"]},
    }
    assert count(config, "derived_image_derivations") == 0
    # An older revision is never applied.
    stale = _apply(client, item, revision["revision_id"], ["r3"])
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "IMAGE_STUDIO_REVISION_NOT_CURRENT"

    applied = _apply(client, item, second["revision_id"], ["r3"])
    assert applied.status_code == 200, applied.json()
    artifact = applied.json()
    assert artifact["seq"] == 2 and artifact["qa_verdict"] is None
    derived = client.get(_image(item, f"/derivations/{artifact['derivation_id']}/image"))
    assert hashlib.sha256(derived.content).hexdigest() == artifact["artifact_sha256"]
    # The M4 owner recorded it: image-translate/v1 over the source, its spec naming only the
    # revision id and fingerprint.
    record = container.images.read_derivation(artifact["derivation_id"])
    assert record.transformation_version == "image-translate/v1"
    assert record.validated_source_revision_id == item.revision_id
    assert [(i.kind, i.sha256) for i in record.inputs] == [
        (ImageAssetKind.SOURCE_ASSET, item.detail)
    ]
    with contextlib.closing(raw(config)) as connection:
        (spec,) = connection.execute(
            "SELECT transformation_spec_json FROM derived_image_derivations"
        ).fetchone()
    assert json.loads(spec) == {
        "content_fingerprint": second["content_fingerprint"],
        "edit_revision_id": second["revision_id"],
    }
    # Applying the same revision again is the same derivation.
    again = _apply(client, item, second["revision_id"], ["r3"]).json()
    assert again["derivation_id"] == artifact["derivation_id"]
    assert count(config, "derived_image_derivations") == 1

    # Selectable only after the operator's PASS on that exact artifact (IT-09).
    select = {"derivation_id": artifact["derivation_id"], "actor": OPERATOR}
    early = client.post(_image(item, "/select"), json=select, headers=HEADERS)
    assert early.status_code == 409 and early.json()["error"]["code"] == "IMAGE_STUDIO_QA_REQUIRED"
    qa = client.post(
        _image(item, "/qa"),
        json={"derivation_id": artifact["derivation_id"], "verdict": "PASS", "actor": OPERATOR},
        headers=HEADERS,
    )
    assert qa.status_code == 200 and qa.json()["sha256"] == artifact["artifact_sha256"]
    chosen = client.post(_image(item, "/select"), json=select, headers=HEADERS)
    assert chosen.status_code == 200, chosen.json()
    selection = container.images.current_selection(item.item_id)
    assert selection is not None and selection.decided_by == OPERATOR
    assert selection.decision_origin.value == "OPERATOR"
    decisions = {d.sha256: (d.decision, d.derivation_id) for d in selection.decisions}
    assert decisions[item.detail] == (SourceDecisionKind.USE_DERIVED, artifact["derivation_id"])
    assert decisions[item.representative] == (SourceDecisionKind.USE_SOURCE, None)
    # Every output is kept: the detail output is now the translated artifact.
    assert [(o.role.value, o.asset_kind, o.sha256) for o in selection.outputs] == [
        ("REPRESENTATIVE", ImageAssetKind.SOURCE_ASSET, item.representative),
        ("DETAIL", ImageAssetKind.DERIVED_ARTIFACT, artifact["artifact_sha256"]),
    ]
    assert _state(client, item)["selection"] == {
        "decision": "USE_DERIVED",
        "output_role": "DETAIL",
        "derivation_id": artifact["derivation_id"],
        "derived_sha256": artifact["artifact_sha256"],
    }
    # Selecting it again changes nothing.
    revisions = count(config, "image_selection_revisions")
    client.post(_image(item, "/select"), json=select, headers=HEADERS)
    assert count(config, "image_selection_revisions") == revisions

    # 되돌리기: the source again; nothing is deleted.
    reverted = client.post(_image(item, "/revert"), json={"actor": OPERATOR}, headers=HEADERS)
    assert reverted.status_code == 200
    selection = container.images.current_selection(item.item_id)
    assert selection is not None
    assert {d.sha256: d.decision for d in selection.decisions}[item.detail] == (
        SourceDecisionKind.USE_SOURCE
    )
    assert count(config, "derived_image_derivations") == 1
    nothing = client.post(_image(item, "/revert"), json={"actor": OPERATOR}, headers=HEADERS)
    assert nothing.status_code == 409
    assert nothing.json()["error"]["code"] == "IMAGE_STUDIO_NOTHING_TO_REVERT"

    # The source was read, never written (IT-01); one OCR reading of it (D4: one request). Every
    # other reading is the leftover-text QA's of a rendering (I6).
    _source_untouched(config, item.detail)
    assert ocr.reads_of(item.detail) == 1
    # No OCR text and no Korean text in an audit row or a log record (§8).
    dump = _dump(config)
    assert not [t for t in TEXTS if t in dump]
    assert "IMAGE_TRANSLATION_APPLIED" in dump
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert not [t for t in TEXTS if t in logged]


def test_an_incomplete_rendering_records_nothing_and_leaves_the_source_selected(
    client: TestClient,
    container: Container,
    config: AppConfig,
    item: StudioProduct,
    tmp_path: Path,
) -> None:
    make_ready(container, tmp_path)
    _run(client, container, item)
    state = _state(client, item)
    # r4 is on the noisy patch: a labelled box over it, and far too much text for its box.
    saved = _save(client, item, state, r4="가" * 400).json()
    refused = _apply(client, item, saved["revision_id"], ["r4"])
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "IMAGE_STUDIO_RENDER_INCOMPLETE"
    assert error["details"] == {"failures": [["r4", "LAYOUT_OVERFLOW"]]}
    assert "가가" not in refused.text
    assert count(config, "derived_image_derivations") == 0
    selection = container.images.current_selection(item.item_id)
    assert selection is not None
    assert all(d.decision is SourceDecisionKind.USE_SOURCE for d in selection.decisions)
    # Nothing at all included is refused too.
    state = _state(client, item)
    empty = _save(client, item, state).json()
    nothing = _apply(client, item, empty["revision_id"], [])
    assert nothing.json()["error"]["code"] == "IMAGE_STUDIO_NOTHING_INCLUDED"


def test_the_callers_language_never_narrows_the_checks_on_apply(
    client: TestClient,
    container: Container,
    config: AppConfig,
    item: StudioProduct,
    tmp_path: Path,
) -> None:
    """IT-05: a run in Japanese makes every Chinese line SCRIPT_MISMATCH. Claiming Chinese at
    「적용」 does not lift it: the recorded language is checked as well."""
    make_ready(container, tmp_path)
    response = client.post(
        f"{BASE}/products/{item.group}/translations",
        json={"images": [item.detail], "source_language": "ja", "actor": OPERATOR},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    job_id = str(response.json()["job_id"])
    wait_until(lambda: container.jobs.get(job_id).state in ("SUCCEEDED", "DEAD"))
    assert container.jobs.get(job_id).state == "SUCCEEDED"
    state = client.get(_image(item), params={"source_language": "ja"}).json()
    regions = {r["region_id"]: r for r in state["regions"]}
    assert state["candidate"]["status"] == "FRESH"
    assert regions["r0"]["flags"] == ["SCRIPT_MISMATCH"]
    saved = client.post(
        _image(item, "/revisions"),
        json={
            "observation_id": state["observation_id"],
            "source_language": "ja",
            "items": _draft(state, r0="순수 천연 참기름"),
            "expected_seq": state["expected_seq"],
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )
    assert saved.status_code == 200, saved.json()
    revision = saved.json()
    assert revision["candidate_id"] == state["candidate"]["candidate_id"]
    # Under a Chinese claim alone r0 would carry no flag; it is refused all the same.
    refused = _apply(client, item, revision["revision_id"], [])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "IMAGE_STUDIO_FLAGGED_UNCONFIRMED"
    assert refused.json()["error"]["details"]["regions"] == ["r0"]
    assert count(config, "derived_image_derivations") == 0
    applied = _apply(client, item, revision["revision_id"], ["r0"])
    assert applied.status_code == 200, applied.json()


def test_erase_is_refused_on_a_complex_background(
    client: TestClient,
    container: Container,
    config: AppConfig,
    item: StudioProduct,
    tmp_path: Path,
) -> None:
    """IT-04: a complex background is never repainted. ERASE on r4 (the noisy patch) is refused
    by preview and save, and nothing is written."""
    make_ready(container, tmp_path)
    _run(client, container, item)
    state = _state(client, item)
    draft = _draft(state, r0=None, r4=None)
    for entry in draft:
        if entry["region_id"] == "r4":
            entry["erase"] = "ERASE"
    before = {t: count(config, t) for t in (*STUDIO_TABLES, *IMAGE_TABLES, "audit_events")}
    common = {"observation_id": state["observation_id"], "source_language": "zh", "items": draft}
    preview = client.post(_image(item, "/preview"), json=common, headers=HEADERS)
    saved = client.post(
        _image(item, "/revisions"),
        json={**common, "expected_seq": state["expected_seq"], "actor": OPERATOR},
        headers=HEADERS,
    )
    for refused in (preview, saved):
        assert refused.status_code == 422
        error = refused.json()["error"]
        assert error["code"] == "IMAGE_STUDIO_DRAFT_INVALID"
        assert error["details"] == {"region_id": "r4", "code": "COMPLEX_BACKGROUND"}
    assert {t: count(config, t) for t in before} == before
    # ERASE where the background is clean is accepted.
    clean = _draft(state, r0=None)
    for entry in clean:
        if entry["region_id"] == "r0":
            assert entry["erase"] == "ERASE"
    ok = client.post(
        _image(item, "/revisions"),
        json={
            "observation_id": state["observation_id"],
            "source_language": "zh",
            "items": clean,
            "expected_seq": state["expected_seq"],
            "actor": OPERATOR,
        },
        headers=HEADERS,
    )
    assert ok.status_code == 200, ok.json()


def test_a_stale_candidate_is_never_offered_as_a_suggestion(
    client: TestClient, container: Container, item: StudioProduct, tmp_path: Path
) -> None:
    make_ready(container, tmp_path)
    _run(client, container, item)
    registry = container.prompt_registry
    current = registry._store.entry(TASK_KEY).current.revision_id
    registry.revise(
        TASK_KEY,
        "template",
        ReviseRequest(
            actor="operator",
            expected_current_revision=current,
            field="prompt",
            text="GOAL\n운영자가 고친 번역 지시",
        ),
        cid="c-revise",
    )
    state = _state(client, item)
    assert state["candidate"]["status"] == "STALE"
    assert state["candidate"]["stale_reasons"] == ["prompt"]
    assert all(r["suggestion"] is None and not r["default_include"] for r in state["regions"])
    # The old text typed back is the operator's own, against no candidate.
    revision = _save(client, item, state, r0="순수 천연 참기름").json()
    assert revision["candidate_id"] is None
    assert [i["origin"] for i in revision["items"] if i["include"]] == ["OPERATOR_CONFIRMED"]


def test_refusals_name_ids_and_codes_never_the_text(
    client: TestClient, container: Container, item: StudioProduct, tmp_path: Path
) -> None:
    make_ready(container, tmp_path)
    _run(client, container, item)
    state = _state(client, item)
    url = _image(item, "/preview")
    body = {"observation_id": state["observation_id"], "source_language": "zh"}
    blank = client.post(url, json={**body, "items": _draft(state, r0="  ")}, headers=HEADERS)
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "IMAGE_STUDIO_DRAFT_INVALID"
    partial = client.post(url, json={**body, "items": _draft(state)[:2]}, headers=HEADERS)
    assert partial.json()["error"]["code"] == "IMAGE_STUDIO_DRAFT_INVALID"
    secret = "순수 천연 참기름 " * 60
    long = client.post(url, json={**body, "items": _draft(state, r0=secret)}, headers=HEADERS)
    assert long.status_code == 422 and long.json()["error"]["code"] == "REQUEST_INVALID"
    assert "참기름" not in long.text
    # Another Item's image, an unknown derivation: refused by id.
    unknown = client.get(f"{BASE}/items/{item.item_id}/images/{'f' * 64}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "IMAGE_STUDIO_IMAGE_UNKNOWN"
    missing = client.post(
        _image(item, "/qa"),
        json={"derivation_id": "nope", "verdict": "PASS", "actor": OPERATOR},
        headers=HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "IMAGE_STUDIO_DERIVATION_UNKNOWN"
    for response in (blank, partial, unknown, missing):
        assert not [t for t in TEXTS if t in response.text]
