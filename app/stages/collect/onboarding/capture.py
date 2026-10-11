"""The local sample captures of 「사이트 분석」 (ADR-0036 §5 step 3, ADR-0017 §7.3).

A trial keeps the one product page it read as a **local sample capture**, so a words change can be
re-analysed offline (§5 step 6). Captures are content-addressed files under the owned data root
(``<data>/onboarding/captures/sha256/<xx>/<sha256>``), written once, verified on every read, and
never deleted while a trial references them (no trial row is ever deleted, so none is).

**Sanitization.** ADR-0017's capture sanitizer (``adaptive/engine/capture.py``) cuts a structured
sample a template cannot read back, and the repository's reconnaissance fixtures were reduced by
hand. A trial must be re-read by the template itself, so this module keeps the page as HTML and
removes what identifies the member, by rules of its own, before anything is written:

- comments, and the text of every inline ``script`` and ``style`` (the elements stay, with a
  script's ``src``): the templates never read a script or a style (ADR-0010 §7);
- every element whose id or a class token names a member, account, login or my-page region
  (``member``, ``mypage``, ``myshop``, ``login``, ``logon``, ``account``, ``userinfo``,
  ``customer``), with everything inside it: a signed-in page greets its member there. A token
  that also names product content (``price``, ``product``, ``prd``, ``goods``, ``item``,
  ``option``, ``detail``) is the product's and stays;
- the ``value`` of every ``input`` and the text of every ``textarea``: a form carries tokens and
  what the member typed, and no template reads a value;
- every event-handler attribute (``on…``);
- in all remaining text and attributes: e-mail addresses, Korean mobile numbers, bearer and JWT
  tokens, secret-named query values (``token=``, ``session=``, ``sid=``, …) and every value of the
  session's own cookies.

What stays is the product's own page: its information rows, its regions, its image references and
its identity declarations. The gap against ADR-0017 §7.3 is stated, not hidden: this is not the
versioned ``ValidationSample`` sanitizer, there is no operator scope, and the capture is the whole
page minus the regions above. A capture is never shown, logged, audited or sent anywhere; only its
SHA-256 leaves this module.
"""

import hashlib
import html
import os
import re
import uuid
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

from app.platform.core.errors import AppError, ErrorClass

CAPTURE_REVISION: Final = "onboarding-capture-1"
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


class _Sanitizer(HTMLParser):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self._secrets = secrets
        self.out: list[str] = []
        # The tag of the private region being skipped and how deep its own tag is nested in it.
        self._skip: tuple[str, int] | None = None
        self._raw: str | None = None

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        kept: list[str] = []
        for name, value in attrs:
            lowered = name.lower()
            if lowered.startswith("on") or (tag == "input" and lowered == "value"):
                continue
            if value is None:
                kept.append(f" {name}")
                continue
            clean = _redact(value, self._secrets)
            kept.append(f' {name}="{html.escape(clean, quote=True)}"')
        return "".join(kept)

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
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        if tag in _RAW_TEXT:
            self._raw = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip is not None or _private(attrs):
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        if self._skip is not None:
            skipped, depth = self._skip
            if tag == skipped:
                self._skip = None if depth == 1 else (skipped, depth - 1)
            return
        if self._raw == tag:
            self._raw = None
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip is not None or self._raw is not None:
            return
        self.out.append(html.escape(_redact(data, self._secrets), quote=False))

    def handle_decl(self, decl: str) -> None:
        if self._skip is None and decl.lower().startswith("doctype"):
            self.out.append(f"<!{decl}>")


def sanitize(body: str, *, secrets: Iterable[str] = ()) -> str:
    """The page with member-identifying material removed (see the module docstring)."""
    values = tuple(sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len))
    parser = _Sanitizer(tuple(reversed(values)))
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
