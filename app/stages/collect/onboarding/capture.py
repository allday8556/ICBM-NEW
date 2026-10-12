"""The local sample captures of 「사이트 분석」 (ADR-0036 §5 step 3, ADR-0017 §7.3).

A trial keeps the one product page it read as a **local sample capture**, so a words change can be
re-analysed offline (§5 step 6). Captures are content-addressed files under the owned data root
(``<data>/onboarding/captures/sha256/<xx>/<sha256>``), written once, verified on every read, and
never deleted while a trial references them (no trial row is ever deleted, so none is).

**Sanitization: an allowlist, then redaction.** ADR-0017's capture sanitizer
(``adaptive/engine/capture.py``) cuts a structured sample a template cannot read back. A trial must
be re-read by the template itself, so this module keeps the page as HTML, but only what the
template reads:

- **Text is kept only inside the template's product regions** (``PlatformAnalysis.
  capture_regions``: its product module, order area, description and image containers, with the
  site's relocated regions) and in the document's ``title``. Every other text node of the page —
  the header that greets the signed-in member, menus, side panels, the footer — is dropped,
  whatever its markup, so member text in a generic element never reaches the capture.
- **The tag structure stays whole**, so nesting reads exactly as the template's parser saw it (an
  unclosed description block included). Outside the regions an element keeps only ``id``,
  ``class`` and ``style``: what a ``within``/``marks`` rule and a hidden-element check read. A
  ``style`` keeps only its ``display``/``visibility`` keywords, everywhere: every other
  declaration, a CSS ``url(…)`` among them, is dropped.
- **Attributes are an allowlist everywhere**: those, the image sources a template reads (``src``,
  ``srcset``, ``data-src``, ``data-original``, ``ec-data-src``) inside the regions, the
  form-state flags (``disabled``, ``selected``, ``checked``), ``name``, a ``meta`` element's
  ``property``/``content``/``charset``, and a ``link``'s ``href`` when it is the canonical link.
- **Every kept locator follows ADR-0010's secret-bearing URL rule**: an image source, a
  ``srcset`` candidate, a ``meta`` URL and the canonical link keep only scheme, host, path and
  the query keys the site profile declares safe for that host; fragments, credentials, ports,
  path parameters and every other query value are dropped (no profile declares a tokenized path
  segment). Every other reference — script, stylesheet and frame sources, login form actions and
  links — keeps only its host (``//host/``, or ``//host/login`` for a login target); a link
  ``href``, form ``action`` or ``data-*`` value is never kept.

Inside what is kept, member-identifying material is then removed by rule:

- comments, and the text of every inline ``script``, ``style`` and ``textarea``;
- every element whose id or a class token names a member, account, login or my-page region
  (``member``, ``mypage``, ``myshop``, ``login``, ``logon``, ``account``, ``userinfo``,
  ``customer``), with everything inside it. A token that also names product content (``price``,
  ``product``, ``prd``, ``goods``, ``item``, ``option``, ``detail``) is the product's and stays;
- the ``value`` of every ``input`` (it is not in the allowlist);
- e-mail addresses, Korean mobile numbers, bearer and JWT tokens, secret-named query values
  (``token=``, ``session=``, ``sid=``, …) and every value of the session's own cookies.

What stays is the product's own regions: its information rows, its image references and its
identity declarations. The trial report is computed on the capture itself, so a re-analysis of
the same capture under the same words reproduces it; tests prove that on the repository's real
storefront fixtures the capture reads exactly as the whole page does. A region a later words
revision relocates outside the kept ones reads as not found in an old capture: a new sample
captures it. A capture is never shown, logged, audited or sent anywhere; only its SHA-256 leaves
this module.
"""

import hashlib
import html
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Final
from urllib.parse import unquote_plus, urljoin, urlsplit

from app.platform.core.errors import AppError, ErrorClass
from app.stages.collect.onboarding.analysis import LOGIN_PATH

CAPTURE_REVISION: Final = "onboarding-capture-2"
ONBOARDING_CAPTURE_MISSING: Final = "ONBOARDING_CAPTURE_MISSING"
ONBOARDING_CAPTURE_CORRUPT: Final = "ONBOARDING_CAPTURE_CORRUPT"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VOID: Final = frozenset(
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
_RAW_TEXT: Final = frozenset({"script", "style", "textarea"})
# What a template reads of an element anywhere: its marks, and whether its style hides it.
_STRUCTURE: Final = frozenset({"id", "class", "style"})
# Of a style, only what can hide an element: a ``display``/``visibility`` keyword (the templates'
# HIDDEN_STYLES). Every other declaration, a ``url(…)`` among them, is dropped.
_STYLE_KEPT: Final = frozenset({"display", "visibility"})
_STYLE_KEYWORD = re.compile(r"^[a-z-]+(!important)?$")
# The kept attributes that locate a resource: each keeps only what ADR-0010 lets a locator keep.
_LOCATORS: Final = frozenset({"src", "srcset", "data-src", "data-original", "ec-data-src"})
# What a template reads inside a product region besides: image sources, form-state flags, names.
_INSIDE: Final = frozenset(
    {
        "src",
        "srcset",
        "data-src",
        "data-original",
        "ec-data-src",
        "disabled",
        "selected",
        "checked",
        "name",
    }
)
# A meta element's declaration (og:title, og:url, og:image, the charset).
_META: Final = frozenset({"property", "name", "content", "charset", "http-equiv"})
_PRIVATE: Final = re.compile(
    r"(?i)(member|mypage|myshop|login|logon|account|userinfo|user-info|customer)"
)
# A token that also names the product's own content (a member price, say) is the product's.
_PRODUCT: Final = re.compile(r"(?i)(price|product|prd|goods|item|option|detail)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MOBILE = re.compile(r"\b01[016789][-. ]?\d{3,4}[-. ]?\d{4}\b")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){0,2}")
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_SECRET_QUERY = re.compile(
    r"(?i)\b(token|session|sid|sessid|jsessionid|phpsessid|auth|apikey|api_key|csrf|sig|signature"
    r"|access_token|refresh_token)=([^&\s\"'<>]+)"
)


class CaptureIntegrityError(AppError):
    """A stored capture is missing or no longer matches its checksum."""

    error_class = ErrorClass.FATAL


def _redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[SESSION]")
    text = _EMAIL.sub("[EMAIL]", text)
    text = _MOBILE.sub("[PHONE]", text)
    text = _JWT.sub("[TOKEN]", text)
    text = _BEARER.sub("[TOKEN]", text)
    return _SECRET_QUERY.sub(lambda m: f"{m.group(1)}=REDACTED", text)


def _private(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name.lower() not in ("id", "class") or not value:
            continue
        if any(_PRIVATE.search(t) and not _PRODUCT.search(t) for t in value.split()):
            return True
    return False


def _marks(attrs: list[tuple[str, str | None]], token: str) -> bool:
    """The templates' own ``marks`` rule: ``#name`` is exactly the id, ``.name`` exactly one of the
    classes, and a bare token the id or a class that starts with it."""
    element_id = next((v or "" for n, v in attrs if n.lower() == "id"), "").strip().lower()
    classes = [c.lower() for n, v in attrs if n.lower() == "class" and v for c in v.split()]
    if token.startswith("#"):
        return token[1:].lower() == element_id
    if token.startswith("."):
        return token[1:].lower() in classes
    bare = token.lower()
    return bare == element_id or any(c == bare or c.startswith(bare) for c in classes)


def _style(value: str) -> str:
    """A style reduced to its ``display``/``visibility`` keywords, squashed as the templates read
    it; empty when it states neither."""
    kept = []
    for declaration in value.split(";"):
        name, _, keyword = "".join(declaration.lower().split()).partition(":")
        if name in _STYLE_KEPT and _STYLE_KEYWORD.fullmatch(keyword):
            kept.append(f"{name}:{keyword}")
    return ";".join(kept)


def _host_only(reference: str, *, login: bool = False) -> str | None:
    """An absolute reference reduced to its host (``//host/``), with the fixed path ``/login`` when
    it targets a login page: what the report's host observation reads, and no path segment, query
    or fragment that could carry a secret. A relative one is the storefront's own: dropped."""
    try:
        parts = urlsplit(reference.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host or parts.scheme not in ("", "http", "https"):
        return None
    if login and not LOGIN_PATH.search(parts.path):
        return None
    return f"//{host}/login" if login else f"//{host}/"


def _attribute(
    tag: str, name: str, value: str | None, attrs: list[tuple[str, str | None]], inside: bool
) -> tuple[bool, str | None]:
    """Whether the attribute is kept, and the value kept (see the module docstring)."""
    if name == "style":
        kept = _style(value or "")
        return bool(kept), kept
    if name in _STRUCTURE:
        return True, value
    if tag == "meta":
        return name in _META, value
    if tag == "link":
        rel = next((v or "" for n, v in attrs if n.lower() == "rel"), "").lower().split()
        if name == "rel":
            return True, value
        if name != "href" or value is None:
            return False, None
        if "canonical" in rel:
            return True, value
        reduced = _host_only(value)
        return reduced is not None, reduced
    if (tag in ("script", "iframe") and name == "src") or (
        (tag, name) in (("form", "action"), ("a", "href"))
    ):
        reduced = None if value is None else _host_only(value, login=tag in ("form", "a"))
        return reduced is not None, reduced
    return inside and name in _INSIDE, value


def _locates(tag: str, name: str, value: str, attrs: list[tuple[str, str | None]]) -> bool:
    """Whether a kept attribute value is a resource locator the URL rule applies to."""
    if tag in ("script", "iframe", "a", "form"):
        return False  # already reduced to its host
    if tag == "link":
        rel = next((v or "" for n, v in attrs if n.lower() == "rel"), "").lower().split()
        return name == "href" and "canonical" in rel
    if tag == "meta":
        return name == "content" and value.strip().lower().startswith(("http:", "https:", "/"))
    return name in _LOCATORS


class _Sanitizer(HTMLParser):
    def __init__(
        self,
        secrets: tuple[str, ...],
        regions: tuple[str, ...],
        base_url: str,
        safe_query_keys: Mapping[str, frozenset[str]],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._secrets = secrets
        self._regions = regions
        self._base = base_url
        self._safe = safe_query_keys
        self.out: list[str] = []
        # The tag of the private region being skipped and how deep its own tag is nested in it.
        self._skip: tuple[str, int] | None = None
        # The tag of the product region being kept and how deep its own tag is nested in it.
        self._region: tuple[str, int] | None = None
        self._raw: str | None = None
        self._title = False

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]], inside: bool) -> str:
        kept: list[str] = []
        for name, value in attrs:
            lowered = name.lower()
            keep, value = _attribute(tag, lowered, value, attrs, inside)
            if not keep:
                continue
            if value is not None and _locates(tag, lowered, value, attrs):
                value = self._srcset(value) if lowered == "srcset" else self._locator(value)
                if not value:
                    continue
            if value is None:
                kept.append(f" {name}")
                continue
            clean = _redact(value, self._secrets)
            kept.append(f' {name}="{html.escape(clean, quote=True)}"')
        return "".join(kept)

    def _locator(self, reference: str) -> str | None:
        """ADR-0010's secret-bearing URL rule on a kept locator: resolved against the sample, then
        only scheme, host, path and the query keys the site's profile declares safe for that host.
        A fragment, credentials, a port, path parameters and every other query value are dropped;
        what is not an http(s) locator (a ``data:`` placeholder) is dropped whole."""
        try:
            parts = urlsplit(urljoin(self._base, reference.strip()))
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not host or not reference.strip():
            return None
        path = parts.path.split(";", 1)[0] or "/"
        safe = self._safe.get(host, frozenset())
        kept = [
            pair
            for pair in parts.query.split("&")
            if pair and unquote_plus(pair.split("=", 1)[0]) in safe
        ]
        return f"{parts.scheme}://{host}{path}" + (f"?{'&'.join(kept)}" if kept else "")

    def _srcset(self, value: str) -> str:
        """Each ``srcset`` candidate's locator under the same rule. A candidate's URL runs to the
        next whitespace (HTML's own parsing), so a comma inside a ``data:`` URL splits nothing."""
        candidates, at = [], 0
        while at < len(value):
            while at < len(value) and (value[at].isspace() or value[at] == ","):
                at += 1
            start = at
            while at < len(value) and not value[at].isspace():
                at += 1
            url, descriptor = value[start:at], ""
            if url.endswith(","):
                url = url.rstrip(",")
            else:
                start = at
                while at < len(value) and value[at] != ",":
                    at += 1
                descriptor = value[start:at].strip()
            if url and (located := self._locator(url)) is not None:
                candidates.append(f"{located} {descriptor}".strip())
        return ", ".join(candidates)

    def _enter(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        if self._region is not None:
            region, depth = self._region
            if tag == region:
                self._region = (region, depth + 1)
        elif any(_marks(attrs, token) for token in self._regions):
            self._region = (tag, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip is not None:
            skipped, depth = self._skip
            if tag == skipped and tag not in _VOID:
                self._skip = (skipped, depth + 1)
            return
        if _private(attrs):
            if tag not in _VOID:
                self._skip = (tag, 1)
            return
        self._enter(tag, attrs)
        self.out.append(f"<{tag}{self._attrs(tag, attrs, self._region is not None)}>")
        if tag in _RAW_TEXT:
            self._raw = tag
        if tag == "title":
            self._title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip is not None or _private(attrs):
            return
        inside = self._region is not None or any(_marks(attrs, t) for t in self._regions)
        self.out.append(f"<{tag}{self._attrs(tag, attrs, inside)}/>")

    def handle_endtag(self, tag: str) -> None:
        if self._skip is not None:
            skipped, depth = self._skip
            if tag == skipped:
                self._skip = None if depth == 1 else (skipped, depth - 1)
            return
        if self._raw == tag:
            self._raw = None
        if tag == "title":
            self._title = False
        if self._region is not None:
            region, depth = self._region
            if tag == region:
                self._region = None if depth == 1 else (region, depth - 1)
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip is not None or self._raw is not None:
            return
        if self._region is None and not self._title:
            return
        self.out.append(html.escape(_redact(data, self._secrets), quote=False))

    def handle_decl(self, decl: str) -> None:
        if self._skip is None and decl.lower().startswith("doctype"):
            self.out.append(f"<!{decl}>")


def sanitize(
    body: str,
    *,
    regions: Iterable[str],
    base_url: str,
    safe_query_keys: Mapping[str, frozenset[str]],
    secrets: Iterable[str] = (),
) -> str:
    """The page reduced to what the template reads, member material removed (module docstring).

    ``regions`` are the template's product regions for the site (``capture_regions``): text and
    content attributes are kept only inside them. ``base_url`` is the sample's URL and
    ``safe_query_keys`` the site profile's safe query keys per host: every kept locator is
    resolved against the one and keeps only the other's keys (ADR-0010)."""
    values = tuple(sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len))
    parser = _Sanitizer(
        tuple(reversed(values)),
        tuple(t for t in regions if t.strip("#.")),
        base_url,
        safe_query_keys,
    )
    parser.feed(body)
    parser.close()
    return "".join(parser.out)


class TrialCaptureStore:
    """Content-addressed sample captures under the owned data root (ADR-0006, ADR-0017)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, sha256: str) -> Path:
        if not _HEX64.fullmatch(sha256):
            raise ValueError("a capture is named by its SHA-256")
        return self._directory / "sha256" / sha256[:2] / sha256

    def put(self, text: str) -> str:
        data = text.encode("utf-8")
        sha256 = hashlib.sha256(data).hexdigest()
        path = self.path(sha256)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
            return sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{sha256}.{uuid.uuid4().hex}.tmp")
        try:
            with staging.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        return sha256

    def read(self, sha256: str) -> str:
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            raise CaptureIntegrityError(
                ONBOARDING_CAPTURE_MISSING, "a trial's sample capture is missing"
            ) from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise CaptureIntegrityError(
                ONBOARDING_CAPTURE_CORRUPT,
                "a trial's sample capture no longer matches its checksum",
            )
        return data.decode("utf-8")
