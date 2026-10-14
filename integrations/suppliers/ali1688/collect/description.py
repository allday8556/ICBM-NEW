"""1688's public description document, read as data (ADR-0040 §3.1).

A captured page names its description document in the embedded-data member,
``description.fields.detailUrl``. The server reads it (never the browser) and hands its text here.
The document is exactly one statement::

    var offer_details={"content":"<html of the description>"};

It is parsed strictly and never run: the trimmed text must start with ``var offer_details=`` and
end with ``;``, and what lies between must be one JSON object holding a ``content`` string. That
string is read as HTML for its ``<img src>`` references, in document order. A reference on one of
the declared image hosts is a ``DETAIL`` candidate; any other is ``UNKNOWN`` and never fetched.

Any mismatch states no image at all and says why. Nothing is repaired or guessed from a part of it.
"""

import json
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from integrations.suppliers.ali1688.collect.embedded import DETAIL_URL, at, member
from integrations.suppliers.ali1688.collect.images import DETAIL_RULE, IMAGE_HOSTS, UNKNOWN_RULE
from integrations.suppliers.collection import (
    DescriptionImages,
    DocumentView,
    ImageCandidate,
    ImageRole,
)

PREFIX = "var offer_details="
TERMINATOR = ";"
DESCRIPTION_NOT_OFFER_DETAILS = "DESCRIPTION_NOT_OFFER_DETAILS"
DESCRIPTION_NOT_JSON = "DESCRIPTION_NOT_JSON"
DESCRIPTION_NO_CONTENT = "DESCRIPTION_NO_CONTENT"
DESCRIPTION_URL_NOT_HTTPS = "DESCRIPTION_URL_NOT_HTTPS"


def locate(document: DocumentView) -> str | None:
    """The description document's URL the captured page names, or None when it names none."""
    data, _ = member(document)
    if data is None:
        return None
    url = at(data, *DETAIL_URL)
    return url if isinstance(url, str) and url else None


def _refuse_constant(name: str) -> object:
    raise ValueError(f"not JSON: {name}")


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        for name, value in attrs:
            if name == "src" and value is not None:
                self.sources.append(value)
                return


def classify(body: str, url: str) -> DescriptionImages:
    """The description's image references, or why the document states none."""
    if urlsplit(url).scheme != "https":
        return DescriptionImages(refusal=DESCRIPTION_URL_NOT_HTTPS)
    text = body.strip()
    if not text.startswith(PREFIX) or not text.endswith(TERMINATOR):
        return DescriptionImages(refusal=DESCRIPTION_NOT_OFFER_DETAILS)
    literal = text[len(PREFIX) : -len(TERMINATOR)]
    try:
        document = json.loads(literal, parse_constant=_refuse_constant)
    except ValueError:
        return DescriptionImages(refusal=DESCRIPTION_NOT_JSON)
    content = document.get("content") if isinstance(document, Mapping) else None
    if not isinstance(content, str):
        return DescriptionImages(refusal=DESCRIPTION_NO_CONTENT)
    reader = _Images()
    reader.feed(content)
    reader.close()
    candidates: list[ImageCandidate] = []
    for written in reader.sources:
        raw = written.strip()
        if not raw or raw.lower().startswith("data:"):
            continue
        resolved = urljoin(url, raw)
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https"):
            continue
        on_host = (parts.hostname or "") in IMAGE_HOSTS
        candidates.append(
            ImageCandidate(
                url=resolved,
                role=ImageRole.DETAIL if on_host else ImageRole.UNKNOWN,
                order=len(candidates),
                rule=DETAIL_RULE if on_host else UNKNOWN_RULE,
                source=written,
            )
        )
    return DescriptionImages(candidates=tuple(candidates))
