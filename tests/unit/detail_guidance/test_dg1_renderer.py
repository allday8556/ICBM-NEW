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
    Template.CLEAN: "fee09a799966cb547916734dfa2493bdc72ea6678b688f2488a81970ac4fdd03",
    Template.MODERN: "fdfbe591fad4c4384ae9626e696cd167cd935a1ec650e671e6e031ca5872bb9d",
    Template.WARM: "544a9fdada7d788687754f19ff6181d2772bc841557d0adb991a7973ed71a1ad",
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


def test_the_preview_is_the_three_templates_of_the_one_renderer() -> None:
    content = _content(SAMPLE)
    shown = preview(content)
    assert [g.template for g in shown] == [Template.CLEAN, Template.MODERN, Template.WARM]
    assert [g.png for g in shown] == [render(content, t).png for t in Template]
    assert len({g.sha256 for g in shown}) == 3


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
        {"blocks": [{"lines": ["당일발송 😀"]}]},
        {"blocks": [{"lines": [3]}]},
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
