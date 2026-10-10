"""ADR-0033 G5: composition v3, the Snapshot pin and ``detail-renderer/v2`` (§6, §7).

Pure and provider-zero: every input is invented and fully resolved. The database, the grant, the
upload byte source and the end-to-end DRY_RUN acceptance are in
``tests/integration/detail_guidance/test_dg5_placement.py``.
"""

import copy
import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from app.stages.products.image_model import OutputRole
from app.stages.products.model import ReadinessStatus
from app.stages.register.detail import (
    DETAIL_RENDERER_VERSION,
    DETAIL_RENDERER_VERSION_V2,
    GUIDANCE_ASSET_KIND,
    DetailPlan,
    DetailPlanError,
    DetailProfile,
    PlannedGuidance,
    UploadedProviderAsset,
    plan_of,
    render,
)
from app.stages.register.guidance import GuidanceEntry, ResolvedGuidance
from app.stages.register.model import GuidanceAssetKind
from app.stages.register.payload import (
    PAYLOAD_BUILDER_VERSION_V2,
    PAYLOAD_BUILDER_VERSION_V3,
    build_payload,
)
from app.stages.register.preparation import (
    DETAIL_BODY_EMPTY,
    PROVIDER_ASSET_IDENTITY_MISSING,
    PUBLICATION_GUIDANCE_UNPLACED,
    PreflightRequest,
    PreflightStage,
    PreparedAsset,
    ResolvedUnit,
    detail_plan,
    evaluate,
    guidance_artifacts,
    selected_artifacts,
)
from integrations.marketplaces.smartstore import product
from tests.unit.register.test_b_detail_composition import (
    DETAILS,
    REPRESENTATIVE,
    V2,
    req,
    unit_of,
)
from tests.unit.register.test_m5_preflight_rules import codes, final, prepared_for

V3 = DetailProfile(
    revision_id="detail-1",
    content_version="registration-detail-composition/v3",
    sections=("TOP_GUIDANCE", "DETAIL_IMAGES", "BODY", "BOTTOM_GUIDANCE"),
    body_format="PLAIN_TEXT",
    renderer=DETAIL_RENDERER_VERSION_V2,
)
V3_SECTIONS = V3.sections
TOP = GuidanceEntry("STORE", "CLEAN", "a" * 64, guidance_revision_id="g-top-1")
HOLIDAY = GuidanceEntry("STORE", "WARM", "b" * 64, guidance_revision_id="g-period-1", kind="PERIOD")
OWN = GuidanceEntry("PRODUCT", "DOMESTIC", "c" * 64, preparation_revision_id="prep-rev-1")
# The same store-wide image at both placements is one guidance asset.
GUIDED = ResolvedGuidance(top=(TOP,), bottom=(HOLIDAY, OWN, TOP))


@pytest.fixture(autouse=True)
def _provider_zero(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a G5 composition test opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def ref(sha256: str) -> str:
    return f"https://shop-phinf.example/p/{sha256[:6]}.png"


def v3_unit(guidance: ResolvedGuidance = GUIDED, *images: Any) -> ResolvedUnit:
    return replace(unit_of(*(images or (REPRESENTATIVE, *DETAILS)), profile=V3), guidance=guidance)


def v3_req(body: str = "body text") -> PreflightRequest:
    return req(body, sections=V3_SECTIONS)


def prepared_all(result: Any) -> tuple[PreparedAsset, ...]:
    """Provider assets for every selected artifact — M4 images and guidance images alike."""
    images = tuple(
        replace(asset, provider_asset_ref=ref(asset.sha256)) for asset in prepared_for(result)
    )
    guidance = tuple(
        PreparedAsset(
            asset_kind=GuidanceAssetKind.GUIDANCE_ARTIFACT,
            sha256=key[1],
            derivation_id=None,
            asset_profile="asset-profile-1",
            candidate_fingerprint=result.candidate_fingerprint,
            provider_asset_ref=ref(key[1]),
        )
        for key in guidance_artifacts(result.resolved)
    )
    return images + guidance


def ready(request_: PreflightRequest, unit: ResolvedUnit) -> Any:
    first = evaluate(request_, unit, PreflightStage.CANDIDATE)
    result = final(request_, unit, prepared_all(first))
    assert result.status is ReadinessStatus.READY, result.codes
    return result


def detail_content(payload: dict[str, Any]) -> str:
    content = product.project(payload).document.mapping()["originProduct"]["detailContent"]
    assert isinstance(content, str)
    return content


# ---------------------------------------------------------------- the plan and the preflight


def test_a_v3_plan_freezes_the_resolved_notices_and_only_v3_places_them() -> None:
    plan = detail_plan(v3_req(), v3_unit())
    assert plan is not None and plan.guides and plan.renderer == DETAIL_RENDERER_VERSION_V2
    assert plan.canonical()["guidance"] == {
        "top": [
            {
                "source": "STORE",
                "guidance_revision_id": "g-top-1",
                "template": "CLEAN",
                "sha256": "a" * 64,
            }
        ],
        "bottom": [
            {
                "source": "STORE",
                "guidance_revision_id": "g-period-1",
                "template": "WARM",
                "sha256": "b" * 64,
            },
            {
                "source": "PRODUCT",
                "preparation_revision_id": "prep-rev-1",
                "template": "DOMESTIC",
                "sha256": "c" * 64,
            },
            {
                "source": "STORE",
                "guidance_revision_id": "g-top-1",
                "template": "CLEAN",
                "sha256": "a" * 64,
            },
        ],
    }
    # Under v3 the notices are placed: no PUBLICATION_GUIDANCE_UNPLACED, and the unit is READY.
    result = evaluate(v3_req(), v3_unit(), PreflightStage.CANDIDATE)
    assert PUBLICATION_GUIDANCE_UNPLACED not in result.codes
    assert result.status is ReadinessStatus.READY and result.upload_permitted
    # Under v2 the same resolved notices still block, at each placement that has one.
    v2 = replace(unit_of(REPRESENTATIVE, *DETAILS, profile=V2), guidance=GUIDED)
    blocked = evaluate(req("body text"), v2, PreflightStage.CANDIDATE)
    assert {r.subject for r in blocked.reasons if r.code == PUBLICATION_GUIDANCE_UNPLACED} == {
        "guidance:top",
        "guidance:bottom",
    }
    assert blocked.status is ReadinessStatus.BLOCKED
    # And with no profile at all.
    none = replace(unit_of(REPRESENTATIVE, *DETAILS, profile=None), guidance=GUIDED)
    assert PUBLICATION_GUIDANCE_UNPLACED in codes(evaluate(req(), none, PreflightStage.CANDIDATE))


def test_a_guidance_image_is_not_a_body() -> None:
    """DETAIL_BODY_EMPTY is unchanged: notices alone never stand in for the body."""
    unit = v3_unit(GUIDED, REPRESENTATIVE)
    result = evaluate(v3_req(""), unit, PreflightStage.CANDIDATE)
    assert DETAIL_BODY_EMPTY in result.codes
    assert DETAIL_BODY_EMPTY not in codes(evaluate(v3_req(""), v3_unit(), PreflightStage.CANDIDATE))


def test_the_artifact_set_is_the_item_images_plus_the_distinct_guidance_images() -> None:
    unit = v3_unit()
    assert guidance_artifacts(unit) == (
        (GUIDANCE_ASSET_KIND, "a" * 64, ""),
        (GUIDANCE_ASSET_KIND, "b" * 64, ""),
        (GUIDANCE_ASSET_KIND, "c" * 64, ""),
    )
    items = tuple(image.key for image in (REPRESENTATIVE, *DETAILS))
    assert selected_artifacts(unit) == items + guidance_artifacts(unit)
    assert guidance_artifacts(v3_unit(ResolvedGuidance())) == ()


def test_the_final_preflight_requires_every_guidance_image_uploaded() -> None:
    first = evaluate(v3_req(), v3_unit(), PreflightStage.CANDIDATE)
    everything = prepared_all(first)
    assert final(v3_req(), v3_unit(), everything).status is ReadinessStatus.READY
    missing = tuple(a for a in everything if a.sha256 != "b" * 64)
    result = final(v3_req(), v3_unit(), missing)
    assert result.status is not ReadinessStatus.READY
    assert [r.subject for r in result.reasons if r.code == PROVIDER_ASSET_IDENTITY_MISSING] == [
        f"asset:{'b' * 64}"
    ]


# ---------------------------------------------------------------- the Snapshot pin


def test_the_payload_is_v3_and_pins_one_url_free_guidance_asset_per_image() -> None:
    outbound = build_payload(ready(v3_req(), v3_unit()))
    payload = outbound.payload
    assert payload["builder_version"] == PAYLOAD_BUILDER_VERSION_V3
    assert payload["guidance_assets"] == [
        {
            "asset_kind": "GUIDANCE_ARTIFACT",
            "sha256": sha * 64,
            "derivation_id": None,
            "asset_profile": "asset-profile-1",
            "provider_asset_ref": ref(sha * 64),
        }
        for sha in "abc"
    ]
    assert outbound.guidance_assets == tuple(payload["guidance_assets"])
    # URL-free but for the uploaded provider references (B-DETAIL §5.2).
    assert "://" not in json.dumps(payload["detail"])
    # A unit without a notice on v3 is still v3, with empty lists and no guidance asset.
    plain = build_payload(ready(v3_req(), v3_unit(ResolvedGuidance()))).payload
    assert plain["builder_version"] == PAYLOAD_BUILDER_VERSION_V3
    assert plain["detail"]["guidance"] == {"top": [], "bottom": []}
    assert plain["guidance_assets"] == []
    # A v2 payload keeps its exact shape: no guidance anywhere.
    v2 = build_payload(ready(req("body text"), unit_of(REPRESENTATIVE, *DETAILS))).payload
    assert v2["builder_version"] == PAYLOAD_BUILDER_VERSION_V2
    assert "guidance" not in json.dumps(v2)


# ---------------------------------------------------------------- detail-renderer/v2


def test_the_renderer_places_top_guidance_detail_images_body_bottom_guidance() -> None:
    payload = build_payload(ready(v3_req("first line\n\n<b>second</b>"), v3_unit())).payload
    img = '<img src="{}" alt="">'.format
    assert detail_content(payload) == "\n".join(
        [
            img(ref("a" * 64)),
            img(ref("3" * 64)),
            img(ref("4" * 64)),
            "<p>first line</p>",
            "<p>&lt;b&gt;second&lt;/b&gt;</p>",
            img(ref("b" * 64)),
            img(ref("c" * 64)),
            img(ref("a" * 64)),
        ]
    )
    # Neither a notice nor a detail image is ever a gallery image.
    assert product.project(payload).image_references == (ref("1" * 64),)


def test_a_unit_without_guidance_on_v3_renders_exactly_as_on_v2() -> None:
    for body, images in (
        ("body text", (REPRESENTATIVE, *DETAILS)),
        ("", (REPRESENTATIVE, *DETAILS)),
        ("only a body", (REPRESENTATIVE,)),
    ):
        v2 = build_payload(ready(req(body), unit_of(*images))).payload
        v3 = build_payload(
            ready(v3_req(body), replace(unit_of(*images, profile=V3), guidance=ResolvedGuidance()))
        ).payload
        assert detail_content(v3) == detail_content(v2)
        assert detail_content(v3).encode("utf-8") == detail_content(v2).encode("utf-8")


def test_an_empty_guidance_section_renders_nothing() -> None:
    top_only = ResolvedGuidance(top=(TOP,))
    payload = build_payload(ready(v3_req(), v3_unit(top_only))).payload
    content = detail_content(payload)
    assert content.startswith(f'<img src="{ref("a" * 64)}" alt="">\n')
    assert content.endswith("<p>body text</p>")


def test_renderer_v1_never_renders_a_guidance_section_and_v2_refuses_misplaced_notices() -> None:
    entry = PlannedGuidance("STORE", "g-1", "CLEAN", "a" * 64)
    uploaded = {entry.key: UploadedProviderAsset(ref("a" * 64))}
    v1 = DetailPlan(
        "detail-1",
        ("TOP_GUIDANCE", "BODY"),
        "PLAIN_TEXT",
        DETAIL_RENDERER_VERSION,
        "body",
        (),
        (entry,),
    )
    with pytest.raises(DetailPlanError) as refused:
        render(v1, uploaded)
    assert refused.value.code == "DETAIL_PLAN_MALFORMED"
    without_section = DetailPlan(
        "detail-1", ("DETAIL_IMAGES", "BODY"), "PLAIN_TEXT", DETAIL_RENDERER_VERSION_V2, "b", ()
    )
    with pytest.raises(DetailPlanError) as unplaced:
        render(replace(without_section, guidance_bottom=(entry,)), uploaded)
    assert unplaced.value.code == "DETAIL_PLAN_MALFORMED"
    for section in ("VIDEO", "OPTION_TABLE"):
        widened = replace(without_section, sections=("DETAIL_IMAGES", "BODY", section))
        with pytest.raises(DetailPlanError):
            render(widened, uploaded)
    # A notice with no uploaded identity, or a plain string for one, refuses the rendering.
    placed = replace(without_section, sections=V3_SECTIONS, guidance_top=(entry,))
    for bad in ({}, {entry.key: ref("a" * 64)}):
        with pytest.raises(DetailPlanError) as missing:
            render(placed, bad)  # type: ignore[arg-type]
        assert missing.value.code == "DETAIL_IMAGE_NOT_UPLOADED"


def test_a_v2_plan_reads_exactly_as_before_and_a_v3_plan_is_read_strictly() -> None:
    v2 = build_payload(ready(req("body text"), unit_of(REPRESENTATIVE, *DETAILS))).payload
    assert plan_of(v2["detail"]).guidance == ()
    with pytest.raises(DetailPlanError):
        plan_of({**v2["detail"], "guidance": {"top": [], "bottom": []}})
    v3 = build_payload(ready(v3_req(), v3_unit())).payload
    assert plan_of(v3["detail"]).canonical() == v3["detail"]
    missing = {k: v for k, v in v3["detail"].items() if k != "guidance"}
    planted = copy.deepcopy(v3["detail"])
    planted["guidance"]["top"][0]["url"] = "https://supplier.example/notice.png"
    swapped = copy.deepcopy(v3["detail"])
    swapped["guidance"]["top"][0]["source"] = "PRODUCT"
    for broken in (missing, planted, swapped):
        with pytest.raises(DetailPlanError):
            plan_of(broken)


# ---------------------------------------------------------------- the projection


def test_the_projection_refuses_a_guidance_plan_mismatch() -> None:
    frozen = build_payload(ready(v3_req(), v3_unit())).payload
    dropped = copy.deepcopy(frozen)
    dropped["guidance_assets"].pop()
    reordered = copy.deepcopy(frozen)
    reordered["guidance_assets"].reverse()
    extra = copy.deepcopy(frozen)
    extra["guidance_assets"].append({**extra["guidance_assets"][0], "sha256": "d" * 64})
    duplicated = copy.deepcopy(frozen)
    duplicated["guidance_assets"].append(duplicated["guidance_assets"][0])
    unpinned = {k: v for k, v in frozen.items() if k != "guidance_assets"}
    derived = copy.deepcopy(frozen)
    derived["guidance_assets"][0]["derivation_id"] = "deriv-1"
    other_kind = copy.deepcopy(frozen)
    other_kind["guidance_assets"][0]["asset_kind"] = "SOURCE_ASSET"
    no_plan = copy.deepcopy(frozen)
    no_plan["detail"]["guidance"]["bottom"] = []
    for broken in (dropped, reordered, extra, duplicated, unpinned, derived, other_kind, no_plan):
        with pytest.raises(product.WireContractError) as refused:
            product.project(broken)
        assert refused.value.code == "WIRE_GUIDANCE_PLAN_MISMATCH"
    # A Snapshot that places no notice pins none.
    v2 = build_payload(ready(req("body text"), unit_of(REPRESENTATIVE, *DETAILS))).payload
    with pytest.raises(product.WireContractError) as planted:
        product.project({**v2, "guidance_assets": []})
    assert planted.value.code == "WIRE_GUIDANCE_PLAN_MISMATCH"


def test_a_guidance_image_without_a_safe_provider_reference_is_not_encodable() -> None:
    frozen = build_payload(ready(v3_req(), v3_unit())).payload
    unprepared = copy.deepcopy(frozen)
    unprepared["guidance_assets"][1]["provider_asset_ref"] = None
    with pytest.raises(product.WireContractError) as missing:
        product.project(unprepared)
    assert missing.value.code == "WIRE_IMAGE_NOT_PREPARED"
    for unsafe in ("http://supplier.example/n.png", "https://x.example/n.png?sig=1"):
        hotlinked = copy.deepcopy(frozen)
        hotlinked["guidance_assets"][0]["provider_asset_ref"] = unsafe
        with pytest.raises(product.WireContractError) as refused:
            product.project(hotlinked)
        assert refused.value.code == "WIRE_IMAGE_REFERENCE_UNSAFE"


def test_the_wire_encoding_is_v10() -> None:
    frozen = build_payload(ready(v3_req(), v3_unit())).payload
    assert product.project(frozen).encoding_version == "smartstore-register-wire/v10"
    assert product.WIRE_ENCODING_VERSION == "smartstore-register-wire/v10"
    assert OutputRole.DETAIL in {image.role for image in DETAILS}
