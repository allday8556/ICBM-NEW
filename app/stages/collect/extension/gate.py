"""The server's security gate over an extension capture (ADR-0019 §6).

The user's decision of 2026-10-01 (ADR-0019 §6.1; EXTENSION-E1.md §5.1):

```text
broad product capture → security-only hard filter → canonical extraction
→ what ICBM does not need is dropped → only canonical facts and evidence are stored
```

The browser cuts the product scope under the reviewed ``BrowserCapturePolicy``, and the server
checks the structure the policy allows (``capture.policy_violations``). Inside that scope this gate
refuses **only** what must never be collected, fail-closed:

- a secret: an attribute named for a token, session, cookie, credential or signature; a value
  shaped like a JWT, a bearer token, a ``key=value`` secret parameter or a long hex secret; a URL
  carrying credentials. Every value and text is read as it arrived and percent-decoded, an image
  reference's whole value included (its query, fragment and descriptors);
- the signed-in member's own account and identity: a Cafe24 member variable
  (``xans-member-var-*``), a Cafe24 my-shop module (``xans-myshop-*``), or an account, my-page,
  login or user-info region, written with or without "-" or "_".

User input values never arrive: the policy keeps no ``value`` or ``name`` attribute and no
``textarea``, and one that arrives is a policy violation before this gate runs.

Everything else in the product scope is product data and goes on **as it arrived**: a business
contact (a supplier's or maker's phone number or e-mail), an element whose class merely begins with
``member`` (a member price), an image reference with an ordinary query, fragment or odd shape. Each
is recorded as a note — a kind and a boundary — and never refuses. The canonical extractor takes
only what ICBM needs, and only canonical facts and evidence are ever stored.

Every finding and note is a kind and a boundary. A captured text or attribute value is never part
of one.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

BoundaryOf = Callable[[Mapping[str, Any]], str]

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

# The secrets the user named (ADR-0019 §6.1): credentials, tokens, cookies and sessions, with the
# names they commonly travel under. One list for attribute names, query keys and ``name=value``
# text.
_SECRET_WORDS = (
    r"token|session|cookie|secret|passw|credential|csrf|xsrf|signature"
    r"|(?:api|access|secret|private|client)[-_]?key"
)
# An attribute whose very name says it carries a secret.
_SECRET_NAME = re.compile(rf"(?i)({_SECRET_WORDS}|auth)")
# A query key that carries a secret or signs a URL.
_SECRET_KEY = re.compile(
    rf"(?i)^(?:.*(?:{_SECRET_WORDS}|auth).*|sid|sessid|jsessionid|phpsessid|sig|key|pw|pwd)$"
)
# A secret by its shape: a JWT, a bearer token, a ``name=value`` secret or URL credentials.
_SECRET_SHAPE = (
    r"eyJ[A-Za-z0-9_-]{10,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/-]{8,}"
    rf"|\b(?:[a-z_-]*(?:{_SECRET_WORDS})[a-z_-]*"
    r"|auth|authorization|sid|sessid|jsessionid|phpsessid|sig|pwd)\s*[=:]\s*[^\s&\"'<>]+"
    r"|[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"
)
# A value that is a secret, wherever it sits: a secret shape or a long hex run.
_SECRET_VALUE = re.compile(rf"(?i)({_SECRET_SHAPE}|\b[a-f0-9]{{32,}}\b)")
# The same in an image reference but for the hex run: suppliers name uploads with long hashes.
_SECRET_IN_REFERENCE = re.compile(rf"(?i)({_SECRET_SHAPE})")
# How many times a value is percent-decoded while it still changes, so an encoded secret is read.
_DECODE_ROUNDS = 4
# The signed-in member's own account and identity.
_IDENTITY_PREFIXES = ("xans-member-var", "xans-myshop")
# Matched with "-" and "_" folded away, so my-page, my_page and mypage are one region.
_ACCOUNT_PREFIXES = ("account", "mypage", "login", "userinfo")
# Product data that only looks private: recorded as notes, never refused.
_MEMBER_PREFIX = "member"
_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|\b0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}\b"
    r"|\b1[5-9]\d{2}-\d{4}\b"
)
_SECURITY_REFERENCE = frozenset({"CREDENTIALS", "SECRET_QUERY", "TOKEN_SHAPED"})
# In an image locator only a token shape is a secret: suppliers name uploads with long hashes, so a
# hex run in a file name is how an image is named, not a secret.
_TOKEN_IN_LOCATOR = re.compile(r"(?i)(eyJ[A-Za-z0-9_-]{10,}|bearer)")


@dataclass(frozen=True)
class GateResult:
    """What the gate found in one capture. ``blocking`` refuses the run; ``notes`` never do."""

    blocking: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _locator_problem(value: str) -> str | None:
    """Why one image locator is not a plain one, or ``None``. A shape, never the value."""
    if (unsafe := _UNSAFE_IN_LOCATOR.search(value)) is not None:
        return "NOT_A_LOCATOR_WHITESPACE" if unsafe.group().isspace() else "NOT_A_LOCATOR_CHARACTER"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "NOT_A_LOCATOR_UNPARSEABLE"
    if "@" in parts.netloc:
        return "CREDENTIALS"
    if _TOKEN_IN_LOCATOR.search(value):
        return "TOKEN_SHAPED"
    if any(_SECRET_KEY.match(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return "SECRET_QUERY"
    if parts.scheme not in {"", "http", "https"}:
        return "SCHEME"
    if parts.query or "?" in value:
        return "QUERY"
    if parts.fragment or "#" in value:
        return "FRAGMENT"
    if not parts.path and not parts.netloc:
        return "NOT_A_LOCATOR_NO_PATH"
    return None


def _reference_problems(name: str, value: str) -> list[str]:
    """Every reason one image reference attribute is not made of plain locators. An empty value or
    an empty ``srcset`` entry names no image, so it is no reference: the supplier's image owner
    (``integrations/suppliers/kmretail/collect/images.py``) skips both the same way, and a Cafe24
    lazy-load ``<img>`` leaves ``src`` empty and names its image in ``ec-data-src``."""
    if name != "srcset":
        problem = _locator_problem(value.strip()) if value.strip() else None
        return [] if problem is None else [problem]
    problems: list[str] = []
    for entry in filter(None, (entry.strip() for entry in value.split(","))):
        parts = entry.split()
        if len(parts) > 2 or (len(parts) == 2 and not _DESCRIPTOR.match(parts[1])):
            problems.append("NOT_A_LOCATOR_SRCSET")
        if (problem := _locator_problem(parts[0])) is not None:
            problems.append(problem)
    return problems


def _holds_secret(text: str, shape: re.Pattern[str]) -> bool:
    """Whether ``text``, or any percent-decoded form of it, holds a secret of ``shape``."""
    for _ in range(_DECODE_ROUNDS):
        if shape.search(text):
            return True
        decoded = unquote(text)
        if decoded == text:
            return False
        text = decoded
    return shape.search(text) is not None


def _folded(token: str) -> str:
    return token.replace("-", "").replace("_", "")


def _tokens(values: Mapping[str, str]) -> set[str]:
    return {*values.get("class", "").lower().split(), values.get("id", "").lower()} - {""}


# What a finding, a note or a logged violation may quote of the page's own names: a tag, an
# attribute name, an id or a class token. A plain identifier is quoted; anything else — a token that
# carries a value (``token=…``), a secret or contact shape, or an odd character — is masked, so a
# record never repeats a secret the gate found in a name.
_PLAIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
MASKED = "?"


def safe_token(token: str) -> str:
    """``token`` when it is a plain identifier with no secret or contact shape, else ``MASKED``."""
    plain = _PLAIN_NAME.match(token) is not None
    if not plain or _SECRET_VALUE.search(token) or _TOKEN_IN_LOCATOR.search(token):
        return MASKED
    return MASKED if _CONTACT.search(token) else token


def _safe_boundary(boundary_of: BoundaryOf, tag: str, values: Mapping[str, str]) -> str:
    """The boundary as the capture owner names it, built from the page's names made safe."""
    identifier = values.get("id", "").strip()
    named = {
        "id": safe_token(identifier) if identifier else "",
        "class": " ".join(safe_token(token) for token in values.get("class", "").split()),
    }
    return boundary_of({"tag": safe_token(tag), "attrs": named})


class _Scan(HTMLParser):
    """One pass over every element, attribute and text of the capture."""

    def __init__(self, boundary_of: BoundaryOf) -> None:
        super().__init__(convert_charrefs=True)
        self._boundary_of = boundary_of
        self._open: list[tuple[str, str]] = []
        self.blocking: dict[str, None] = {}
        self.notes: dict[str, None] = {}

    def _here(self) -> str:
        return self._open[-1][1] if self._open else "document"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        boundary = _safe_boundary(self._boundary_of, tag, values)
        tokens = _tokens(values)
        if any(token.startswith(_IDENTITY_PREFIXES) for token in tokens):
            self.blocking.setdefault(f"MEMBER_IDENTITY@{boundary}")
        if any(_folded(token).startswith(_ACCOUNT_PREFIXES) for token in tokens):
            self.blocking.setdefault(f"ACCOUNT_REGION@{boundary}")
        if any(token.startswith(_MEMBER_PREFIX) for token in tokens):
            self.notes.setdefault(f"MEMBER_NAMED@{boundary}")
        for name, value in values.items():
            quoted = safe_token(name)
            if _SECRET_NAME.search(name):
                self.blocking.setdefault(f"SECRET_ATTRIBUTE:{quoted}@{boundary}")
            reference = tag == "img" and name not in _NOT_A_REFERENCE
            refused = False
            if reference:
                for problem in _reference_problems(name, value):
                    finding = f"IMAGE_REFERENCE_{problem}:{quoted}@{boundary}"
                    if problem in _SECURITY_REFERENCE:
                        refused = True
                        self.blocking.setdefault(finding)
                    else:
                        self.notes.setdefault(finding)
            # Every value is read for a secret. An image reference is read whole too: a fragment, a
            # descriptor or a query value can carry one as well as a query key.
            shape = _SECRET_IN_REFERENCE if reference else _SECRET_VALUE
            secret = not refused and _holds_secret(value, shape)
            if secret or _SECRET_VALUE.search(name):
                self.blocking.setdefault(f"SECRET_VALUE:{quoted}@{boundary}")
            elif not reference and _CONTACT.search(value):
                self.notes.setdefault(f"CONTACT:{quoted}@{boundary}")
        if tag not in _VOID:
            self._open.append((tag, boundary))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth][0] == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        if _holds_secret(data, _SECRET_VALUE):
            self.blocking.setdefault(f"SECRET_TEXT@{self._here()}")
        elif _CONTACT.search(data):
            self.notes.setdefault(f"CONTACT_TEXT@{self._here()}")

    # The structure check refuses a comment, a declaration and a processing instruction before this
    # gate runs. The gate still reads each as text, so it stands on its own.
    def handle_comment(self, data: str) -> None:
        self.handle_data(data)

    def handle_decl(self, decl: str) -> None:
        self.handle_data(decl)

    def unknown_decl(self, data: str) -> None:
        self.handle_data(data)

    def handle_pi(self, data: str) -> None:
        self.handle_data(data)


def security_gate(html: str, *, boundary_of: BoundaryOf) -> GateResult:
    """What in the capture must never be collected (``blocking``), and what was only noted."""
    scan = _Scan(boundary_of)
    scan.feed(html)
    scan.close()
    return GateResult(blocking=tuple(scan.blocking), notes=tuple(scan.notes))
