"""The server's final gate over an extension capture (ADR-0019 §6).

Owner amendment ``5909645067`` §1.

The pipeline goes on with the capture **as it arrived**, never with a sanitized copy. So a capture
passes only when the capture owner's sanitizer and final scan had nothing private or secret to take
out of **any part of it**. The capture owner is used as it is, through the sanitizer the container
hands in; this module never imports it.

Two things the capture owner's generic rules do not decide well for this input are decided here,
before it is asked:

- **Image references.** An ``<img>`` reference is judged as a URL: a plain ``http(s)`` or relative
  locator with no credentials, no query and no fragment. A generic secret pattern would refuse any
  image whose file name is a long hash, which is how suppliers commonly name uploads, so the
  reference is judged by what it is and then masked for the scan. A reference that is not such a
  locator is a finding. An empty value is no reference at all, as the supplier's image owner reads
  it (``integrations/suppliers/kmretail/collect/images.py``): a Cafe24 lazy-load ``<img>`` leaves
  ``src`` empty and names its image in ``ec-data-src``.
- **Regions the sanitizer sets aside.** The sanitizer drops a navigation or non-authoritative region
  whole and looks no further into it, but the extractor receives the whole capture. So every such
  region is opened and scanned as well: it is neutralized and the sanitizer is asked again, until
  it sets nothing more aside. A private region, wherever it sits, is a finding.

Every finding is a kind and a boundary. A captured text or attribute value is never part of one.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

PRIVATE_REGION = "PRIVATE"
NAVIGATION_REGION = "NAVIGATION"
# A page cannot nest regions deeper than the structural depth ceiling; this bounds the loop.
MAX_PASSES = 16
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_NOT_A_REFERENCE = frozenset({"id", "class", "style"})
_DESCRIPTOR = re.compile(r"^\d+(?:\.\d+)?[wx]$")
_UNSAFE_IN_LOCATOR = re.compile(r"[\s<>\"'\\^`{|}\x00-\x1f\x7f]")
_TOKEN_SHAPED = re.compile(r"(?i)(eyJ[A-Za-z0-9_-]{10,}|bearer)")
_MASK = "/image"


@dataclass(frozen=True)
class Sanitized:
    """What the capture owner's sanitizer and final scan said of one document.

    ``refusal`` is its own refusal text (kinds and boundaries), or ``None``. ``removals`` are
    ``(kind, boundary)`` and ``excluded`` are ``(boundary, region class)``.
    """

    refusal: str | None
    removals: tuple[tuple[str, str], ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()


Sanitizer = Callable[[str], Sanitized]
BoundaryOf = Callable[[Mapping[str, Any]], str]


def _locator_problem(value: str) -> str | None:
    """Why one image locator is not a plain one, or ``None``.

    A "not a locator" answer names its shape (whitespace, another unsafe character, unparseable,
    no path), never the value: a refused real capture is gone with its job, so the kind is all an
    operator can learn from (EXTENSION-E1.md §5.1).
    """
    if (unsafe := _UNSAFE_IN_LOCATOR.search(value)) is not None:
        return "NOT_A_LOCATOR_WHITESPACE" if unsafe.group().isspace() else "NOT_A_LOCATOR_CHARACTER"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "NOT_A_LOCATOR_UNPARSEABLE"
    if parts.scheme not in {"", "http", "https"}:
        return "SCHEME"
    if "@" in parts.netloc:
        return "CREDENTIALS"
    if parts.query or "?" in value:
        return "QUERY"
    if parts.fragment or "#" in value:
        return "FRAGMENT"
    if not parts.path and not parts.netloc:
        return "NOT_A_LOCATOR_NO_PATH"
    if _TOKEN_SHAPED.search(value):
        return "TOKEN_SHAPED"
    return None


def _reference_problem(name: str, value: str) -> str | None:
    """Why one image reference attribute is not made of plain locators, or ``None``. An empty
    value or an empty ``srcset`` entry names no image, so it is no reference: the image owner
    skips both the same way."""
    if name != "srcset":
        return _locator_problem(value.strip()) if value.strip() else None
    for entry in filter(None, (entry.strip() for entry in value.split(","))):
        parts = entry.split()
        if len(parts) > 2 or (len(parts) == 2 and not _DESCRIPTOR.match(parts[1])):
            return "NOT_A_LOCATOR_SRCSET"
        if (problem := _locator_problem(parts[0])) is not None:
            return problem
    return None


class _View(HTMLParser):
    """Re-serialize a capture for the scan: every text and attribute as it is, except that image
    references are masked and the regions already opened are neutralized."""

    def __init__(self, neutral: Mapping[str, str], boundary_of: BoundaryOf) -> None:
        super().__init__(convert_charrefs=True)
        self._neutral = neutral
        self._boundary_of = boundary_of
        self._out: list[str] = []
        # (the tag as it arrived, the tag as it is written)
        self._open: list[tuple[str, str]] = []
        self.image_findings: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self._out.append(f"<!{decl}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        boundary = self._boundary_of({"tag": tag, "attrs": values})
        written = tag
        if (region := self._neutral.get(boundary)) is not None:
            # Opened for the scan: it no longer names a region, and its content is scanned.
            values = {name: value for name, value in values.items() if name not in {"id", "class"}}
            if region == NAVIGATION_REGION:
                written = "div"
        if tag == "img":
            for name in [name for name in values if name not in _NOT_A_REFERENCE]:
                problem = _reference_problem(name, values[name])
                if problem is not None:
                    self.image_findings.append(f"IMAGE_REFERENCE_{problem}:{name}@{boundary}")
                values[name] = _MASK
        rendered = "".join(
            f' {name}="{escape(value, quote=True)}"' for name, value in values.items()
        )
        self._out.append(f"<{written}{rendered}>")
        if tag not in _VOID:
            self._open.append((tag, written))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth][0] == tag:
                for _, written in reversed(self._open[depth:]):
                    self._out.append(f"</{written}>")
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._out.append(escape(data, quote=False))

    def text(self) -> str:
        return "".join(self._out)


def _view(html: str, neutral: Mapping[str, str], boundary_of: BoundaryOf) -> _View:
    view = _View(neutral, boundary_of)
    view.feed(html)
    view.close()
    return view


def final_gate(html: str, *, sanitize: Sanitizer, boundary_of: BoundaryOf) -> tuple[str, ...]:
    """Every reason the capture may not go on as it arrived; empty means it is clean."""
    findings: dict[str, None] = {}
    neutral: dict[str, str] = {}
    for _ in range(MAX_PASSES):
        view = _view(html, neutral, boundary_of)
        for finding in view.image_findings:
            findings.setdefault(finding)
        report = sanitize(view.text())
        if report.refusal is not None:
            findings.setdefault(report.refusal)
            return tuple(findings)
        for kind, boundary in report.removals:
            findings.setdefault(f"SANITIZER_REMOVED:{kind}@{boundary}")
        set_aside: dict[str, str] = {}
        for boundary, region in report.excluded:
            if region == PRIVATE_REGION:
                findings.setdefault(f"SANITIZER_EXCLUDED:{region}@{boundary}")
            elif boundary not in neutral:
                set_aside[boundary] = region
        if not set_aside:
            return tuple(findings)
        neutral.update(set_aside)
    findings.setdefault("SANITIZER_REGION_DEPTH_EXCEEDED")
    return tuple(findings)
