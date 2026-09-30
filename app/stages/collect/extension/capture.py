"""What an extension capture is, and what the server re-checks of it (ADR-0019 §3, §5, §6).

The browser cuts the product scope and sanitizes it under the reviewed policy. The server trusts
none of that: it measures and checks exactly what arrived.

- :func:`measure` counts what the capture holds — its bytes, its elements and its image
  references — for the ceilings that refuse a whole ingest before any run exists. It needs no
  policy: an image reference is any attribute of an ``<img>`` that is not its own ``id``,
  ``class`` or ``style``, and each ``srcset`` entry is one.
- :func:`policy_violations` reads the capture as structure and names everything the policy does
  not allow: an excluded tag, an attribute outside the allowlist, anything in ``<head>`` beyond the
  head allowance, or a frame that is not the one the extension builds.
- :class:`TransportEvidence` is what the browser observed of the navigation. Each value becomes a
  ``DocumentView`` field only if it is present and valid; nothing is defaulted (ADR-0019 §2).

Every finding is a kind and a tag or attribute name. Captured text and attribute values are never
part of a finding, a log or an error.
"""

from dataclasses import dataclass
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app.stages.collect.extension.policy import BrowserCapturePolicy

CAPTURED_CONTENT_TYPE = "text/html"
CAPTURED_STATUS = 200
# The frame the extension builds around a capture: exactly these, in this order.
_FRAME = ("html", "head", "body")
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


class PolicyReference(BaseModel):
    """The capture policy a capture says it was cut with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: StrictStr
    digest: StrictStr


class TransportEvidence(BaseModel):
    """What the browser observed of the captured tab's own navigation.

    ``response_status`` and ``redirect_count`` come from the navigation timing entry, ``url`` from
    the document's location, ``navigation_name`` from that entry's own name, and ``content_type``
    and ``character_set`` from the document. A value the browser could not observe is absent, and
    an absent value refuses the capture: it is never filled in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: StrictStr
    navigation_name: StrictStr
    response_status: StrictInt
    redirect_count: StrictInt
    content_type: StrictStr
    character_set: StrictStr


class CaptureEnvelope(BaseModel):
    """The whole body of one ingest. Any other field refuses the request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    supplier_key: StrictStr
    policy: PolicyReference
    transport: TransportEvidence
    html: StrictStr = Field(repr=False)


@dataclass(frozen=True)
class CaptureMeasure:
    html_bytes: int
    nodes: int
    image_refs: int


# The attributes of an ``<img>`` that never point at an image.
_NOT_A_REFERENCE = frozenset({"id", "class", "style"})


class _Structure(HTMLParser):
    """One pass over a capture: what it holds, and what the policy does not allow."""

    def __init__(self, policy: BrowserCapturePolicy | None) -> None:
        super().__init__(convert_charrefs=True)
        self._policy = policy
        self._open: list[str] = []
        self._frame: list[str] = []
        self.nodes = 0
        self.image_refs = 0
        self.violations: list[str] = []

    def _in_head(self) -> bool:
        return "head" in self._open and "body" not in self._open

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        values = {name.lower(): value or "" for name, value in attrs}
        depth = len(self._open)
        if tag == "img":
            self._images(values)
        if tag in _FRAME and depth == (0 if tag == "html" else 1):
            self._frame.append(tag)
            if values:
                self.violations.append(f"FRAME_ATTRIBUTE:{tag}")
        elif depth < 2:
            # Nothing may sit outside <head> and <body>, or beside <html>.
            self.violations.append(f"OUTSIDE_FRAME:{tag}")
        elif self._policy is not None:
            if self._in_head():
                if not self._policy.keeps_head(tag, values):
                    self.violations.append(f"HEAD_NOT_ALLOWED:{tag}")
            elif tag in self._policy.excluded_tags:
                self.violations.append(f"TAG_EXCLUDED:{tag}")
            for name in values:
                if not self._policy.keeps_attribute(tag, name):
                    self.violations.append(f"ATTRIBUTE_NOT_ALLOWED:{tag}[{name}]")
        if tag not in _VOID:
            self._open.append(tag)

    def _images(self, values: dict[str, str]) -> None:
        for name, value in values.items():
            if name in _NOT_A_REFERENCE:
                continue
            if name == "srcset":
                self.image_refs += sum(1 for entry in value.split(",") if entry.strip())
            elif value.strip():
                self.image_refs += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth] == tag:
                del self._open[depth:]
                return

    def handle_comment(self, data: str) -> None:
        self.violations.append("COMMENT")

    def handle_pi(self, data: str) -> None:
        self.violations.append("PROCESSING_INSTRUCTION")

    def frame_violations(self) -> list[str]:
        return [] if tuple(self._frame) == _FRAME else ["FRAME_NOT_EXACT"]


def _read(html: str, policy: BrowserCapturePolicy | None) -> _Structure:
    reader = _Structure(policy)
    reader.feed(html)
    reader.close()
    return reader


def measure(html: str) -> CaptureMeasure:
    """What exactly arrived: its UTF-8 size, its elements and its image references."""
    reader = _read(html, None)
    return CaptureMeasure(
        html_bytes=len(html.encode("utf-8")), nodes=reader.nodes, image_refs=reader.image_refs
    )


def policy_violations(html: str, policy: BrowserCapturePolicy) -> tuple[str, ...]:
    """Everything in the capture the policy does not allow, as distinct kinds in a stable order."""
    reader = _read(html, policy)
    return tuple(sorted({*reader.violations, *reader.frame_violations()}))
