"""The plain-text content of one notice (ADR-0033 §1).

A notice is 1 to 3 blocks; each block has an optional heading and 1 to 6 lines. Every value is
plain text, trimmed; a control character or a character the bundled font cannot draw is refused,
never dropped (DG-09). The limits are ICBM's own layout limits, not provider limits (B-DETAIL D4).
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.platform.core.errors import InputValidationError

BLOCKS_MAX: Final = 3
LINES_MAX: Final = 6
HEADING_MAX_LENGTH: Final = 20
LINE_MAX_LENGTH: Final = 40
GUIDANCE_TEXT_INVALID: Final = "GUIDANCE_TEXT_INVALID"


@dataclass(frozen=True)
class GuidanceBlock:
    heading: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class GuidanceContent:
    blocks: tuple[GuidanceBlock, ...]

    def canonical(self) -> dict[str, Any]:
        return {"blocks": [{"heading": b.heading, "lines": list(b.lines)} for b in self.blocks]}


def _refuse(reason: str, **details: Any) -> InputValidationError:
    return InputValidationError(
        GUIDANCE_TEXT_INVALID, f"the notice text is refused: {reason}", details=details
    )


def _text(value: Any, *, where: str, limit: int, drawable: frozenset[int]) -> str:
    if not isinstance(value, str):
        raise _refuse("a value is not text", where=where)
    text = value.strip()
    if len(text) > limit:
        raise _refuse("a value is too long", where=where, length=len(text), limit=limit)
    for char in text:
        if unicodedata.category(char).startswith("C") or ord(char) not in drawable:
            raise _refuse(
                "a character cannot be drawn", where=where, codepoint=f"U+{ord(char):04X}"
            )
    return text


def guidance_content(raw: Mapping[str, Any], drawable: frozenset[int]) -> GuidanceContent:
    """Validate ``{"blocks": [{"heading", "lines"}]}`` into a ``GuidanceContent``.

    ``drawable`` is the code-point set of the bundled font (``renderer.drawable_codepoints``)."""
    blocks = raw.get("blocks") if isinstance(raw, Mapping) else None
    if (
        not isinstance(blocks, Sequence)
        or isinstance(blocks, str)
        or not 1 <= len(blocks) <= BLOCKS_MAX
    ):
        raise _refuse(f"a notice has 1 to {BLOCKS_MAX} blocks")
    parsed: list[GuidanceBlock] = []
    for b, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise _refuse("a block is not an object", where=f"blocks[{b}]")
        heading = _text(
            block.get("heading", ""),
            where=f"blocks[{b}].heading",
            limit=HEADING_MAX_LENGTH,
            drawable=drawable,
        )
        lines = block.get("lines")
        if not isinstance(lines, Sequence) or isinstance(lines, str):
            raise _refuse("lines are not a list", where=f"blocks[{b}].lines")
        texts = tuple(
            _text(line, where=f"blocks[{b}].lines[{i}]", limit=LINE_MAX_LENGTH, drawable=drawable)
            for i, line in enumerate(lines)
        )
        if not 1 <= len(texts) <= LINES_MAX or any(not t for t in texts):
            raise _refuse(
                f"a block has 1 to {LINES_MAX} non-empty lines", where=f"blocks[{b}].lines"
            )
        parsed.append(GuidanceBlock(heading=heading, lines=texts))
    return GuidanceContent(blocks=tuple(parsed))
