"""ADR-0033 G1: the Detail Guidance renderer and the notice content (DG-02, DG-09)."""

import hashlib
import io
from typing import Any

import pytest
from PIL import Image

from app.capabilities.detail_guidance import renderer
from app.capabilities.detail_guidance.content import (
    GUIDANCE_TEXT_INVALID,
    LINE_MAX_LENGTH,
    guidance_content,
)
from app.capabilities.detail_guidance.presets import PLACEHOLDER, PRESETS, PRESETS_VERSION
from app.capabilities.detail_guidance.renderer import (
    CANVAS_WIDTH,
    FONT_FILE,
    FONT_SHA256,
    GUIDANCE_TEXT_TOO_WIDE,
    RENDERER_VERSION,
    Template,
    drawable_codepoints,
    preview,
    render,
)
from app.platform.core.errors import InputValidationError

SAMPLE: dict[str, Any] = {
    "blocks": [
        {"heading": "배송 안내", "lines": ["12시 이전 주문시 당일발송", "Weekend 0~9 ·※"]},
        {"heading": "", "lines": ["C/S 평일 10:00 ~ 17:00"]},
    ]
}
# The decoded pixels of SAMPLE under each template, pinned for guidance-renderer/v1 with the
# bundled font and the pinned Pillow (constraints.txt). A drawing change must bump the version.
PIXELS = {
    Template.CLEAN: "354ab2f0fc537b4b733e6f45a89dfab79977b707e471e3899ddab1260add076f",
    Template.MODERN: "508a412a67c664a95728e8de5fd9959d32a081e1659aff6e0f2030634c23ca53",
    Template.WARM: "8274623ce786d08cf1fb810e2920554971fa74799169bd2bb671fabf9f594e8e",
    Template.DOMESTIC: "370af15041a5670ac9228866cf0607b4eb68bd992b883eeef3295e82ef33c040",
    Template.OVERSEAS: "0d289d9de08f3d4917da5eb0de49db73db0919262f221de4bfaeb0d9f478e5ed",
}


def _content(raw: dict[str, Any]) -> Any:
    return guidance_content(raw, drawable_codepoints())


def _refused(raw: dict[str, Any]) -> InputValidationError:
    with pytest.raises(InputValidationError) as caught:
        _content(raw)
    assert caught.value.code == GUIDANCE_TEXT_INVALID
    return caught.value


def test_the_bundled_font_is_the_pinned_ofl_file() -> None:
    assert hashlib.sha256(FONT_FILE.read_bytes()).hexdigest() == FONT_SHA256
    licence = (FONT_FILE.parent / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License, Version 1.1" in licence
    drawable = drawable_codepoints()
    assert {ord("가"), ord("힣"), ord("A"), ord("0"), ord("·"), ord("※")} <= drawable
    assert ord("😀") not in drawable


@pytest.mark.parametrize("template", list(Template))
def test_each_template_draws_the_same_pixels_and_bytes(template: Template) -> None:
    first = render(_content(SAMPLE), template)
    again = render(_content(SAMPLE), template)
    assert first.png == again.png
    assert first.sha256 == hashlib.sha256(first.png).hexdigest()
    image = Image.open(io.BytesIO(first.png))
    assert image.format == "PNG" and image.size == (CANVAS_WIDTH, first.height)
    assert not image.info.get("dpi") and "tIME" not in image.info
    assert hashlib.sha256(image.tobytes()).hexdigest() == PIXELS[template]
    assert (first.template, first.renderer_version, first.font_sha256) == (
        template,
        RENDERER_VERSION,
        FONT_SHA256,
    )


def test_the_preview_is_the_five_templates_of_the_one_renderer() -> None:
    content = _content(SAMPLE)
    shown = preview(content)
    assert [g.template for g in shown] == list(Template)
    assert [t.value for t in Template] == ["CLEAN", "MODERN", "WARM", "DOMESTIC", "OVERSEAS"]
    assert [g.png for g in shown] == [render(content, t).png for t in Template]
    assert len({g.sha256 for g in shown}) == 5


def test_the_height_follows_the_text_and_a_heading_is_optional() -> None:
    one = render(_content({"blocks": [{"lines": ["당일발송"]}]}), Template.CLEAN)
    two = render(_content({"blocks": [{"lines": ["당일발송", "주말 제외"]}]}), Template.CLEAN)
    headed = render(
        _content({"blocks": [{"heading": "안내", "lines": ["당일발송"]}]}), Template.CLEAN
    )
    assert one.height < two.height and one.height < headed.height


def test_the_content_is_trimmed_plain_text() -> None:
    content = _content({"blocks": [{"heading": "  배송 안내 ", "lines": [" 당일발송  "]}]})
    assert content.canonical() == {"blocks": [{"heading": "배송 안내", "lines": ["당일발송"]}]}


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"blocks": []},
        {"blocks": [{"lines": ["a"]}] * 4},
        {"blocks": [{"lines": []}]},
        {"blocks": [{"lines": ["a"] * 7}]},
        {"blocks": [{"lines": ["  "]}]},
        {"blocks": [{"lines": "당일발송"}]},
        {"blocks": [{"heading": "가" * 21, "lines": ["a"]}]},
        {"blocks": [{"lines": ["가" * (LINE_MAX_LENGTH + 1)]}]},
        {"blocks": [{"lines": ["당일\t발송"]}]},
        {"blocks": [{"lines": ["당일\n발송"]}]},
        {"blocks": [{"lines": ["\t당일발송"]}]},
        {"blocks": [{"heading": "안내\n", "lines": ["당일발송"]}]},
        {"blocks": [{"lines": ["당일발송 😀"]}]},
        {"blocks": [{"lines": [3]}]},
        {"blocks": [{"lines": ["문의 https://example.kr"]}]},
        {"blocks": [{"lines": ["www.shop 에서 확인"]}]},
        {"blocks": [{"lines": ["naver.com 으로 문의"]}]},
        {"blocks": [{"heading": "<b>안내</b>", "lines": ["당일발송"]}]},
        {"blocks": [{"lines": ["당일 &amp; 익일"]}]},
        {"blocks": [{"lines": ["<script>당일"]}]},
    ],
)
def test_text_that_cannot_be_drawn_exactly_is_refused(raw: dict[str, Any]) -> None:
    _refused(raw)


def test_a_line_wider_than_the_text_box_is_refused_never_shrunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _content({"blocks": [{"lines": ["가" * LINE_MAX_LENGTH]}]})
    for template in Template:
        render(content, template)
    monkeypatch.setattr(renderer, "TEXT_BOX_WIDTH", 100)
    with pytest.raises(InputValidationError) as caught:
        render(content, Template.CLEAN)
    assert caught.value.code == GUIDANCE_TEXT_TOO_WIDE


def test_a_shipping_template_draws_its_mark_band_above_the_text() -> None:
    content = _content(SAMPLE)
    plain = render(content, Template.CLEAN)
    for template in (Template.DOMESTIC, Template.OVERSEAS):
        shipped = render(content, template)
        assert shipped.height == plain.height + renderer.BAND_HEIGHT


def test_the_shipping_presets_are_valid_editable_examples() -> None:
    assert PRESETS_VERSION == "guidance-presets/v1"
    assert [(p.key, p.label, p.template) for p in PRESETS] == [
        ("DOMESTIC", "국내배송", Template.DOMESTIC),
        ("OVERSEAS", "해외배송", Template.OVERSEAS),
    ]
    for preset in PRESETS:
        for raw in (preset.top, preset.bottom):
            content = _content(raw)
            for template in Template:
                render(content, template)
        text = str(preset.top) + str(preset.bottom)
        assert PLACEHOLDER in text
    overseas = str(PRESETS[1].top) + str(PRESETS[1].bottom)
    assert "개인통관고유부호" in overseas and "관부가세" in overseas
    # No fixed legal threshold or amount (ADR-0033 §11).
    assert not any(word in overseas for word in ("달러", "USD", "$", "원 ", "만원"))


def test_plain_punctuation_is_not_markup_or_a_url() -> None:
    content = _content(
        {
            "blocks": [
                {
                    "heading": "C/S 안내",
                    "lines": ["<주말 제외>", "10:00 ~ 17:00 (1.5일)", "A/S 1:1"],
                }
            ]
        }
    )
    assert content.blocks[0].lines[0] == "<주말 제외>"
