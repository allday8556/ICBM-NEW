"""B-DETAIL: the detail composition plan and the trusted REGISTER renderer (ADR-0014 §19, §27.1
amendment notes; design ``documents/reviews/B-DETAIL-detail-composition.md``).

The 20 owner-required cases, provider-zero: no test here opens a socket or calls a provider.
"""

import copy
import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.stages.products.image_model import ImageAssetKind, OutputRole, QaVerdict
from app.stages.products.model import ReadinessStatus, Reason
from app.stages.register import sanitize
from app.stages.register.authoring import AuthoredInputs, encode_inputs
from app.stages.register.authoring_revisions import (
    AuthoringRevisionError,
    AuthoringRevisionKind,
    canonical_content,
    detail_composition_content,
    detail_profile_of,
)
from app.stages.register.detail import (
    DETAIL_RENDERER_VERSION,
    DetailPlan,
    DetailPlanError,
    DetailProfile,
    PlannedImage,
    UploadedProviderAsset,
    render,
)
from app.stages.register.model import ListingShape
from app.stages.register.payload import (
    PAYLOAD_BUILDER_VERSION,
    PAYLOAD_BUILDER_VERSION_V2,
    PayloadNotReadyError,
    build_payload,
)
from app.stages.register.preparation import (
    DetailComposition,
    ListingValues,
    PreflightRequest,
    PreflightStage,
    PublicationImage,
    ReadinessInput,
    ResolvedUnit,
    UnitRequest,
    detail_plan,
    evaluate,
)
from integrations.marketplaces.smartstore import product
from tests.unit.register.test_m5_preflight_rules import (
    ITEMS,
    LISTING,
    candidate,
    codes,
    final,
    prepared_for,
    request,
    resolved,
)

ROOT = Path(__file__).resolve().parents[3]
V2 = DetailProfile(
    revision_id="detail-1",
    content_version="registration-detail-composition/v2",
    sections=("DETAIL_IMAGES", "BODY"),
    body_format="PLAIN_TEXT",
    renderer=DETAIL_RENDERER_VERSION,
)
V1 = DetailProfile("detail-1", "registration-detail-composition/v1", ("BODY",))
SECTIONS = ("DETAIL_IMAGES", "BODY")


@pytest.fixture(autouse=True)
def _provider_zero(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Case 20: provider-zero. Any socket opened while a case runs fails it."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a B-DETAIL test opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _image(
    role: OutputRole,
    position: int,
    digit: str,
    *,
    kind: ImageAssetKind = ImageAssetKind.SOURCE_ASSET,
    derivation: str | None = None,
    qa: QaVerdict = QaVerdict.PASS,
) -> PublicationImage:
    return PublicationImage(role, position, kind, digit * 64, derivation, f"qa-{digit}", qa)


REPRESENTATIVE = _image(OutputRole.REPRESENTATIVE, 0, "1")
DETAILS = (_image(OutputRole.DETAIL, 1, "3"), _image(OutputRole.DETAIL, 2, "4"))
SINGLE_LISTING = ListingValues(
    name=LISTING.name,
    tags=LISTING.tags,
    attributes=LISTING.attributes,
    notices=LISTING.notices,
    options={},
)


def unit_of(
    *images: PublicationImage, profile: DetailProfile | None = V2, **overrides: Any
) -> ResolvedUnit:
    """A one-Item separate listing whose Item carries ``images``."""
    first = replace(ITEMS[0], images=images)
    values: dict[str, Any] = {
        "items": (first,),
        "listing_shape": ListingShape.SEPARATE_LISTINGS,
        "detail_profile": profile,
    }
    values.update(overrides)
    unit = resolved(**values)
    return replace(
        unit,
        open_items=unit.open_items[:1],
        unit_item_ids=("i1",),
    )


def req(
    body: str = "", *, sections: tuple[str, ...] = SECTIONS, **overrides: Any
) -> PreflightRequest:
    values: dict[str, Any] = {
        "unit": UnitRequest("draft-1", 3, ("i1",)),
        "listing": SINGLE_LISTING,
        "detail": DetailComposition("detail-1", body, sections),
    }
    values.update(overrides)
    return request(**values)


def projected(result: Any) -> product.WireProjection:
    return product.project(build_payload(result).payload)


def detail_content(projection: product.WireProjection) -> str:
    return projection.document.mapping()["originProduct"]["detailContent"]


def uploaded(plan: DetailPlan) -> dict[tuple[str, str, str], UploadedProviderAsset]:
    return {
        image.key: UploadedProviderAsset(f"https://shop-phinf.example/d/{image.sha256[:6]}.jpg")
        for image in plan.images
    }


def ready_final(request_: PreflightRequest, unit: ResolvedUnit) -> Any:
    prepared = tuple(
        replace(asset, provider_asset_ref=f"https://shop-phinf.example/p/{asset.sha256[:6]}.jpg")
        for asset in prepared_for(evaluate(request_, unit, PreflightStage.CANDIDATE))
    )
    result = final(request_, unit, prepared)
    assert result.status is ReadinessStatus.READY, result.codes
    return result


# ---------------------------------------------------------------- 1, 2: a valid plan


def test_01_detail_images_only_is_a_valid_plan() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    result = candidate(req(""), unit)
    assert result.status is ReadinessStatus.READY, result.codes
    plan = detail_plan(req(""), unit)
    assert plan is not None
    assert plan.body == ""
    assert [image.sha256 for image in plan.images] == ["3" * 64, "4" * 64]
    assert "DETAIL_BODY_EMPTY" not in codes(result)


def test_02_detail_images_and_body_is_a_valid_plan() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    result = ready_final(req("first paragraph"), unit)
    content = detail_content(projected(result))
    assert content == (
        '<img src="https://shop-phinf.example/p/333333.jpg" alt="">\n'
        '<img src="https://shop-phinf.example/p/444444.jpg" alt="">\n'
        "<p>first paragraph</p>"
    )


# ---------------------------------------------------------------- 3, 4, 5: the operator body


def test_03_the_body_is_escaped_deterministically() -> None:
    plan = DetailPlan(
        "detail-1",
        SECTIONS,
        "PLAIN_TEXT",
        DETAIL_RENDERER_VERSION,
        "a & b < c > d \"e\" 'f'\nnext line\r\n\r\n\n second",
        (),
    )
    assert render(plan, {}) == (
        "<p>a &amp; b &lt; c &gt; d &quot;e&quot; &#x27;f&#x27;<br>next line</p>\n<p> second</p>"
    )


def test_04_operator_raw_html_cannot_bypass_the_policy() -> None:
    body = "<img src=x onerror=alert(1)><script>alert(2)</script><a href=x>link</a><p>p</p>"
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    content = detail_content(projected(ready_final(req(body), unit)))
    assert "<script" not in content
    assert "<a " not in content
    assert "<img src=x" not in content
    # Only the planned images are image elements; the operator's markup is text.
    assert content.count("<img ") == len(DETAILS)
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in content


@pytest.mark.parametrize(
    "body",
    ["see https://evil.example/x", 'look <img src="//evil.example/x">', "visit www.evil.com now"],
)
def test_05_an_operator_url_cannot_bypass_the_policy(body: str) -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    result = candidate(req(body), unit)
    assert result.status is ReadinessStatus.BLOCKED
    assert sanitize.EXTERNAL_URL in codes(result)
    with pytest.raises(sanitize.PayloadSanitationError):
        encode_inputs(AuthoredInputs(None, SINGLE_LISTING, DetailComposition("detail-1", body)))
    with pytest.raises(PayloadNotReadyError):
        build_payload(final(req(body), unit))


# ---------------------------------------------------------------- 6: the COLLECT description


def test_06_the_truncated_collect_description_is_never_used() -> None:
    sources = [
        *sorted((ROOT / "app" / "stages" / "register").glob("*.py")),
        ROOT / "integrations" / "marketplaces" / "smartstore" / "product.py",
        ROOT / "ui" / "web" / "js" / "pages" / "register.js",
    ]
    assert sources
    for path in sources:
        assert "detail_description" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- 7, 8: determinism


def test_07_the_image_order_is_the_selection_order_and_deterministic() -> None:
    shuffled = unit_of(DETAILS[1], REPRESENTATIVE, DETAILS[0])
    plan = detail_plan(req(""), shuffled)
    assert plan is not None
    assert [(i.item_id, i.position) for i in plan.images] == [("i1", 1), ("i1", 2)]
    second = ITEMS[1]
    two = resolved(
        items=(
            replace(ITEMS[0], images=(REPRESENTATIVE, DETAILS[1])),
            replace(
                second,
                images=(
                    _image(OutputRole.REPRESENTATIVE, 0, "5"),
                    DETAILS[1],
                    _image(OutputRole.DETAIL, 3, "6"),
                ),
            ),
        ),
        detail_profile=V2,
    )
    plan = detail_plan(request(detail=DetailComposition("detail-1", "", SECTIONS)), two)
    assert plan is not None
    # Item order, then position; an asset shared by two Items is placed once.
    assert [i.sha256[0] for i in plan.images] == ["4", "6"]


def test_08_the_same_frozen_inputs_and_renderer_give_the_same_detail_content() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    result = ready_final(req("body"), unit)
    frozen = build_payload(result).payload
    again = json.loads(json.dumps(frozen))
    assert detail_content(product.project(frozen)) == detail_content(product.project(again))
    assert build_payload(result).payload_digest == build_payload(result).payload_digest
    assert frozen["detail"]["renderer"] == DETAIL_RENDERER_VERSION


# ---------------------------------------------------------------- 9, 10: immutability and staleness


def test_09_an_authoring_change_after_the_snapshot_leaves_it_unchanged() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    frozen = build_payload(ready_final(req("first"), unit)).payload
    kept = copy.deepcopy(frozen)
    before = detail_content(product.project(frozen))
    # A later revision of the body, and a later profile revision, are other candidates…
    later = ready_final(req("second"), unit)
    assert later.candidate_fingerprint != candidate(req("first"), unit).candidate_fingerprint
    moved = replace(unit, target=replace(unit.target, detail_composition_revision="detail-2"))
    assert "AUTHORING_REVISIONS_UNOWNED" in codes(candidate(req("first"), moved))
    # …and the frozen Snapshot and its rendering are exactly what they were.
    assert frozen == kept
    assert detail_content(product.project(frozen)) == before


def test_10_an_image_selection_advance_is_a_stale_dependency() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    first = candidate(req("body"), unit)
    advanced = replace(
        unit, items=(replace(unit.items[0], selection_revision_id="selection-i1-next"),)
    )
    moved = candidate(req("body"), advanced)
    assert moved.candidate_fingerprint != first.candidate_fingerprint
    stale = final(req("body"), advanced, prepared_for(first))
    assert stale.status is ReadinessStatus.STALE
    assert "PREPARED_ASSET_CANDIDATE_MISMATCH" in codes(stale)
    # Another detail image order is another plan, so another candidate.
    reordered = unit_of(REPRESENTATIVE, replace(DETAILS[0], position=3), DETAILS[1])
    assert candidate(req("body"), reordered).candidate_fingerprint != first.candidate_fingerprint


# ---------------------------------------------------------------- 11, 12, 13: the trusted path


def test_11_a_missing_provider_asset_identity_refuses_the_send() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    first = candidate(req("body"), unit)
    without_detail = tuple(a for a in prepared_for(first) if a.sha256 != DETAILS[0].sha256)
    result = final(req("body"), unit, without_detail)
    assert result.status is not ReadinessStatus.READY
    assert "PROVIDER_ASSET_IDENTITY_MISSING" in codes(result)
    frozen = build_payload(ready_final(req("body"), unit)).payload
    for asset in frozen["items"][0]["publication_assets"]:
        if asset["sha256"] == DETAILS[0].sha256:
            asset["provider_asset_ref"] = None
    with pytest.raises(product.WireContractError) as refused:
        product.project(frozen)
    assert refused.value.code == "WIRE_IMAGE_NOT_PREPARED"


@pytest.mark.parametrize(
    "reference",
    [
        "http://supplier.example/detail.jpg",
        "https://supplier.example/detail.jpg?sig=1",
        "//supplier.example/detail.jpg",
        "https://user@supplier.example/detail.jpg",
    ],
)
def test_12_a_supplier_or_source_hotlink_never_appears(reference: str) -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    frozen = build_payload(ready_final(req("body"), unit)).payload
    assert "://" not in json.dumps(frozen["detail"])
    hotlinked = copy.deepcopy(frozen)
    for asset in hotlinked["items"][0]["publication_assets"]:
        if asset["role"] == "DETAIL":
            asset["provider_asset_ref"] = reference
    with pytest.raises(product.WireContractError) as refused:
        product.project(hotlinked)
    assert refused.value.code == "WIRE_IMAGE_REFERENCE_UNSAFE"
    planted = copy.deepcopy(frozen)
    planted["detail"]["images"][0]["url"] = reference
    with pytest.raises(product.WireContractError) as malformed:
        product.project(planted)
    assert malformed.value.code == "WIRE_DETAIL_PLAN_MALFORMED"


def test_13_an_uploaded_provider_identity_only_enters_through_the_trusted_path() -> None:
    plan = DetailPlan(
        "detail-1",
        SECTIONS,
        "PLAIN_TEXT",
        DETAIL_RENDERER_VERSION,
        "",
        (PlannedImage("i1", 1, "SOURCE_ASSET", "3" * 64, None),),
    )
    # A plain string is not an uploaded provider identity.
    raw: Any = {plan.images[0].key: "https://shop-phinf.example/d/x.jpg"}
    with pytest.raises(DetailPlanError) as refused:
        render(plan, raw)
    assert refused.value.code == "DETAIL_IMAGE_NOT_UPLOADED"
    for unsafe in ("javascript:alert(1)", "https://x.example/a.jpg#f", "bearer abcdefghijkl"):
        with pytest.raises(DetailPlanError):
            UploadedProviderAsset(unsafe)
    assert render(plan, uploaded(plan)).startswith('<img src="https://shop-phinf.example/d/')
    # The projection: detail images are placed in the body and never in the gallery, and the
    # plan's images must be exactly the Snapshot's detail images.
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    frozen = build_payload(ready_final(req("body"), unit)).payload
    projection = product.project(frozen)
    assert projection.image_references == ("https://shop-phinf.example/p/111111.jpg",)
    extra = copy.deepcopy(frozen)
    extra["detail"]["images"].pop()
    with pytest.raises(product.WireContractError) as mismatch:
        product.project(extra)
    assert mismatch.value.code == "WIRE_DETAIL_PLAN_MISMATCH"


# ---------------------------------------------------------------- 14, 15, 16: the blocker


@pytest.mark.parametrize(
    ("profile", "composition", "sections"),
    [
        (None, "detail-1", ("BODY",)),
        (V1, "detail-1", ("BODY",)),
        (V2, "detail-other", SECTIONS),
        (V2, "detail-1", ("BODY",)),
    ],
    ids=["no-profile", "v1-body-only", "other-revision", "other-sections"],
)
def test_14_unplaced_remains_while_no_valid_plan_exists(
    profile: DetailProfile | None, composition: str, sections: tuple[str, ...]
) -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS, profile=profile)
    request_ = req("body", detail=DetailComposition(composition, "body", sections))
    assert detail_plan(request_, unit) is None or not detail_plan(request_, unit).images  # type: ignore[union-attr]
    result = candidate(request_, unit)
    assert "PUBLICATION_DETAIL_IMAGES_UNPLACED" in codes(result)
    assert result.status is ReadinessStatus.BLOCKED
    assert not result.upload_permitted


def test_15_a_valid_plan_clears_only_the_detail_composition_blocker() -> None:
    images = (REPRESENTATIVE, *DETAILS)
    for change in ({}, {"category": None}, {"duplicate_evidence": None}):
        v1_request = req("body", sections=("BODY",), **change)
        blocked = candidate(v1_request, unit_of(*images, profile=V1))
        placed = candidate(req("body", **change), unit_of(*images))
        assert codes(blocked) - codes(placed) == {"PUBLICATION_DETAIL_IMAGES_UNPLACED"}
        assert codes(placed) - codes(blocked) == set()


def test_16_unrelated_blocked_and_review_reasons_are_unchanged() -> None:
    sold_out = ReadinessInput(
        ReadinessStatus.BLOCKED,
        (Reason("SOURCE_STOCK_SOLD_OUT", ReadinessStatus.BLOCKED),),
        "m4-rule/v",
        "f" * 64,
    )
    failing = (REPRESENTATIVE, _image(OutputRole.DETAIL, 1, "3", qa=QaVerdict.FAIL), DETAILS[1])
    unit = unit_of(*failing)
    unit = replace(unit, items=(replace(unit.items[0], base=sold_out),))
    result = candidate(req("", category=None), unit)
    assert result.status is ReadinessStatus.BLOCKED
    found = {(r.code, r.status) for r in result.reasons}
    assert ("M4_BASE.SOURCE_STOCK_SOLD_OUT", ReadinessStatus.BLOCKED) in found
    assert ("PUBLICATION_ASSET_QA_NOT_PASSED", ReadinessStatus.BLOCKED) in found
    assert ("CATEGORY_NOT_SELECTED", ReadinessStatus.REVIEW_REQUIRED) in found
    assert "PUBLICATION_DETAIL_IMAGES_UNPLACED" not in codes(result)


# ---------------------------------------------------------------- 17, 18, 19: regressions


def test_17_the_no_detail_no_image_legacy_path_is_unchanged() -> None:
    for profile in (None, V1):
        unit = unit_of(REPRESENTATIVE, profile=profile)
        legacy = req("legacy <b>body</b>", sections=("BODY",))
        result = ready_final(legacy, unit)
        built = build_payload(result).payload
        assert built["builder_version"] == PAYLOAD_BUILDER_VERSION
        assert built["detail"] == {
            "composition_revision": "detail-1",
            "sections": ["BODY"],
            "body": "legacy <b>body</b>",
        }
        # A BODY-only Snapshot projects its frozen body exactly as before B-DETAIL.
        assert detail_content(product.project(built)) == "legacy <b>body</b>"
        assert "plan" not in result.dependencies["candidate"]["detail"]
    # Under v2 a unit without detail images still needs its body, and renders it escaped.
    unit = unit_of(REPRESENTATIVE)
    assert "DETAIL_BODY_EMPTY" in codes(candidate(req(""), unit))
    built = build_payload(ready_final(req("text <b>"), unit)).payload
    assert built["builder_version"] == PAYLOAD_BUILDER_VERSION_V2
    assert detail_content(product.project(built)) == "<p>text &lt;b&gt;</p>"
    # A BODY-only Snapshot with a detail image is still never projected.
    legacy = copy.deepcopy(built)
    legacy["detail"] = {"composition_revision": "detail-1", "sections": ["BODY"], "body": "x"}
    legacy["items"][0]["publication_assets"].append(
        {**legacy["items"][0]["publication_assets"][0], "role": "DETAIL", "sha256": "3" * 64}
    )
    with pytest.raises(product.WireContractError) as refused:
        product.project(legacy)
    assert refused.value.code == "WIRE_DETAIL_IMAGE_NOT_PLACEABLE"


def test_18_the_m4_image_lineage_is_carried_exactly() -> None:
    derived = _image(
        OutputRole.DETAIL, 1, "7", kind=ImageAssetKind.DERIVED_ARTIFACT, derivation="deriv-1"
    )
    unit = unit_of(REPRESENTATIVE, derived, DETAILS[1])
    result = ready_final(req(""), unit)
    built = build_payload(result).payload
    assert built["detail"]["images"] == [
        {
            "item_id": "i1",
            "position": 1,
            "asset_kind": "DERIVED_ARTIFACT",
            "sha256": "7" * 64,
            "derivation_id": "deriv-1",
        },
        {
            "item_id": "i1",
            "position": 2,
            "asset_kind": "SOURCE_ASSET",
            "sha256": "4" * 64,
            "derivation_id": None,
        },
    ]
    # Every selected image stays a publication asset with its own role, unchanged.
    assets = built["items"][0]["publication_assets"]
    assert [(a["role"], a["sha256"][0], a["derivation_id"]) for a in assets] == [
        ("REPRESENTATIVE", "1", None),
        ("DETAIL", "7", "deriv-1"),
        ("DETAIL", "4", None),
    ]


def test_19_the_snapshot_payload_stays_sanitized_and_url_free() -> None:
    unit = unit_of(REPRESENTATIVE, *DETAILS)
    built = build_payload(ready_final(req("plain text"), unit))
    payload = built.payload
    assert payload["builder_version"] == PAYLOAD_BUILDER_VERSION_V2
    assert set(payload["detail"]) == {
        "composition_revision",
        "sections",
        "body_format",
        "renderer",
        "body",
        "images",
    }
    sanitize.require_clean(payload["detail"])
    assert not list(sanitize.problems(payload["detail"], path="detail"))
    assert (
        built.payload_digest == build_payload(ready_final(req("plain text"), unit)).payload_digest
    )


def test_20_this_suite_is_provider_zero() -> None:
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    source = (ROOT / "app" / "stages" / "register" / "detail.py").read_text(encoding="utf-8")
    for transport in ("httpx", "requests", "urllib", "socket", "aiohttp"):
        assert f"import {transport}" not in source


# ---------------------------------------------------------------- the profile owner


def test_the_profile_v2_holds_no_product_content_and_v1_still_reads() -> None:
    content = detail_composition_content("smartstore")
    document = canonical_content(
        AuthoringRevisionKind.DETAIL_COMPOSITION, "smartstore", None, content
    )
    assert set(document) == {
        "content_version",
        "kind",
        "marketplace_key",
        "sections",
        "body_format",
        "renderer",
        "guidance",
    }
    profile = detail_profile_of("rev-2", document)
    assert profile.sections == SECTIONS and profile.places_images
    v1 = {
        "content_version": "registration-detail-composition/v1",
        "kind": "DETAIL_COMPOSITION",
        "marketplace_key": "smartstore",
        "sections": ["BODY"],
        "guidance": False,
    }
    old = detail_profile_of("rev-1", v1)
    assert old.sections == ("BODY",) and not old.renders


@pytest.mark.parametrize(
    "change",
    [
        {"sections": ["BODY", "DETAIL_IMAGES"]},
        {"sections": ["TOP_GUIDANCE", "DETAIL_IMAGES", "BODY"]},
        {"sections": ["DETAIL_IMAGES", "BODY", "VIDEO"]},
        {"body_format": "HTML"},
        {"renderer": "detail-renderer/v0"},
        {"guidance": True},
        {"images": ["3" * 64]},
        {"body": "product text"},
    ],
)
def test_a_profile_is_never_widened(change: dict[str, Any]) -> None:
    content = {**detail_composition_content("smartstore"), **change}
    with pytest.raises(AuthoringRevisionError):
        canonical_content(AuthoringRevisionKind.DETAIL_COMPOSITION, "smartstore", None, content)


def test_no_count_limit_truncates_a_plan() -> None:
    many = tuple(
        _image(
            OutputRole.DETAIL,
            n,
            f"{n:064x}"[-1],
            kind=ImageAssetKind.DERIVED_ARTIFACT,
            derivation=f"d-{n}",
        )
        for n in range(1, 51)
    )
    plan = detail_plan(req(""), unit_of(REPRESENTATIVE, *many))
    assert plan is not None and len(plan.images) == 50
    assert render(plan, uploaded(plan)).count("<img ") == 50
