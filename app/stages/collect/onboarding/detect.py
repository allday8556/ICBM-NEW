"""Platform detection and the policy reads of 「+ 공급처 추가」 (ADR-0036 §4 steps 2–3).

**Detection.** One read of the storefront's home page and one of each template's login page, all
through the common transport's bootstrap reader (one egress grant, for the entered host only).
A template is detected when its home-page markers *and* its login-form markers are both present;
exactly one detected template is the answer, anything else is *unsupported*. Markers are the
template packages' own data (``PlatformDetection``): their names are the evidence, and nothing
else is read from a page (no fact, no text kept).

**Policy reads.** robots.txt and the template's terms path: at most ``POLICY_READ_CAP`` reads
(ADR-0010 Q1). They are shown and acknowledged; their content never blocks anything (the owner's
principle: the seller carries that risk, so ICBM shows the facts and warnings and the operator
decides). A robots rule that disallows the template's product path, or the whole site, is a
warning.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from integrations.suppliers.base import PublicRead, PublicReader
from integrations.suppliers.platforms import PlatformDetection, PlatformTemplate, markers_in

UNSUPPORTED: Final = "unsupported"
HOME_PATH: Final = "/"
# What the robots summary shows: enough to read the file, never a page of it.
ROBOTS_LINES: Final = 60
ROBOTS_LINE_CHARS: Final = 200


class UnsupportedReason(StrEnum):
    HOME_UNREAD = "HOME_UNREAD"
    HOME_REDIRECT = "HOME_REDIRECT"
    NO_MARKERS = "NO_MARKERS"
    AMBIGUOUS = "AMBIGUOUS"


class PolicyWarning(StrEnum):
    ROBOTS_DISALLOWS_ALL = "ROBOTS_DISALLOWS_ALL"
    ROBOTS_DISALLOWS_PRODUCTS = "ROBOTS_DISALLOWS_PRODUCTS"
    ROBOTS_ABSENT = "ROBOTS_ABSENT"
    ROBOTS_UNREAD = "ROBOTS_UNREAD"
    TERMS_UNREAD = "TERMS_UNREAD"


@dataclass(frozen=True)
class TemplateEvidence:
    """What one template's markers found: names only."""

    platform: str
    home_markers: tuple[str, ...]
    login_path: str
    login_status: int | None
    login_markers: tuple[str, ...]

    @property
    def matched(self) -> bool:
        return bool(self.home_markers) and bool(self.login_markers)


@dataclass(frozen=True)
class Detection:
    platform: str
    reason: UnsupportedReason | None
    home_status: int | None
    # A home page that answered with a redirect: where to, as an https origin (for the operator).
    home_redirect: str | None
    evidence: tuple[TemplateEvidence, ...]
    reads: int

    @property
    def supported(self) -> bool:
        return self.platform != UNSUPPORTED

    @property
    def marker_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for e in self.evidence
            if e.platform == self.platform
            for name in (*e.home_markers, *e.login_markers)
        )


@dataclass(frozen=True)
class RobotsSummary:
    lines: tuple[str, ...]
    disallows_all: bool
    disallows_products: bool
    crawl_delay: str | None


@dataclass(frozen=True)
class PolicyDocument:
    document: str  # ROBOTS / TERMS
    path: str
    status: int | None
    failure: str | None
    content_sha256: str | None
    truncated: bool
    robots: RobotsSummary | None = None
    warnings: tuple[PolicyWarning, ...] = field(default_factory=tuple)


def detections(templates: Mapping[str, PlatformTemplate]) -> dict[str, PlatformDetection]:
    return {
        name: template.detection
        for name, template in sorted(templates.items())
        if template.detection is not None
    }


def detect(reader: PublicReader, host: str, templates: Mapping[str, PlatformTemplate]) -> Detection:
    """One home read plus each template's login read, then the classification (§4 step 2)."""
    known = detections(templates)
    login_paths = tuple(dict.fromkeys(d.login_path for d in known.values()))
    reads = reader.read(host, (HOME_PATH, *login_paths))
    home, logins = reads[0], dict(zip(login_paths, reads[1:], strict=True))
    return classify(home, logins, known)


def classify(
    home: PublicRead, logins: Mapping[str, PublicRead], known: Mapping[str, PlatformDetection]
) -> Detection:
    home_text = home.text() if home.status == 200 else ""
    evidence = []
    for platform, rules in known.items():
        login = logins.get(rules.login_path)
        login_text = login.text() if login is not None and login.status == 200 else ""
        evidence.append(
            TemplateEvidence(
                platform=platform,
                home_markers=markers_in(home_text, rules.home_markers),
                login_path=rules.login_path,
                login_status=None if login is None else login.status,
                login_markers=markers_in(login_text, rules.login_markers),
            )
        )
    matched = [e.platform for e in evidence if e.matched]
    redirect = None
    reason: UnsupportedReason | None = None
    if home.status is not None and 300 <= home.status < 400 and home.location_host:
        redirect = f"https://{home.location_host}"
    if len(matched) == 1:
        platform = matched[0]
    else:
        platform = UNSUPPORTED
        if home.status is None:
            reason = UnsupportedReason.HOME_UNREAD
        elif redirect is not None:
            reason = UnsupportedReason.HOME_REDIRECT
        elif matched:
            reason = UnsupportedReason.AMBIGUOUS
        else:
            reason = UnsupportedReason.NO_MARKERS
    return Detection(
        platform=platform,
        reason=reason,
        home_status=home.status,
        home_redirect=redirect,
        evidence=tuple(evidence),
        reads=1 + len(logins),
    )


def read_policies(
    reader: PublicReader, host: str, rules: PlatformDetection
) -> tuple[PolicyDocument, ...]:
    """robots.txt and the template's terms page: two policy reads (§4 step 3, ADR-0010 Q1)."""
    robots, terms = reader.read(host, (rules.robots_path, rules.terms_path))
    return (_robots_document(robots, rules), _terms_document(terms))


def _sha256(read: PublicRead) -> str | None:
    return hashlib.sha256(read.content).hexdigest() if read.status is not None else None


def _robots_document(read: PublicRead, rules: PlatformDetection) -> PolicyDocument:
    warnings: list[PolicyWarning] = []
    summary = None
    if read.status == 200:
        summary = summarise_robots(read.text(), rules.product_path_prefix)
        if summary.disallows_all:
            warnings.append(PolicyWarning.ROBOTS_DISALLOWS_ALL)
        elif summary.disallows_products:
            warnings.append(PolicyWarning.ROBOTS_DISALLOWS_PRODUCTS)
    elif read.status == 404:
        warnings.append(PolicyWarning.ROBOTS_ABSENT)
    else:
        warnings.append(PolicyWarning.ROBOTS_UNREAD)
    return PolicyDocument(
        document="ROBOTS",
        path=read.path,
        status=read.status,
        failure=None if read.failure is None else read.failure.value,
        content_sha256=_sha256(read),
        truncated=read.truncated,
        robots=summary,
        warnings=tuple(warnings),
    )


def _terms_document(read: PublicRead) -> PolicyDocument:
    return PolicyDocument(
        document="TERMS",
        path=read.path,
        status=read.status,
        failure=None if read.failure is None else read.failure.value,
        content_sha256=_sha256(read),
        truncated=read.truncated,
        warnings=() if read.status == 200 else (PolicyWarning.TERMS_UNREAD,),
    )


# ---------------------------------------------------------------- robots.txt (RFC 9309)

_FIELD = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


def _pattern(value: str) -> re.Pattern[str]:
    anchored = value.endswith("$")
    body = re.escape(value[:-1] if anchored else value).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def _groups(text: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        found = _FIELD.match(line)
        if found is None:
            continue
        name, value = found.group(1).lower(), found.group(2)
        if name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif name in ("allow", "disallow", "crawl-delay") and agents:
            rules.append((name, value))
    if agents:
        groups.append((agents, rules))
    return groups


def _allowed(rules: Sequence[tuple[str, str]], path: str) -> bool:
    """RFC 9309 §2.2.2: the longest matching rule decides; an equal Allow wins."""
    best: tuple[int, bool] | None = None
    for name, value in rules:
        if name not in ("allow", "disallow") or not value:
            continue
        if _pattern(value).match(path):
            candidate = (len(value), name == "allow")
            if best is None or candidate > best:
                best = candidate
    return True if best is None else best[1]


def summarise_robots(text: str, product_path: str) -> RobotsSummary:
    """The ``*`` group's verdict on the site root and on the template's product path, and the
    file's first lines for the operator to read. A rule the site addresses to a named bot alone is
    shown but not judged: ICBM reads with an ordinary browser identity."""
    rules = [rule for agents, group in _groups(text) if "*" in agents for rule in group]
    delays = [value for name, value in rules if name == "crawl-delay" and value]
    lines = tuple(
        line[:ROBOTS_LINE_CHARS] for line in (x.strip() for x in text.splitlines()) if line
    )[:ROBOTS_LINES]
    disallows_all = not _allowed(rules, "/") and not _allowed(rules, product_path)
    return RobotsSummary(
        lines=lines,
        disallows_all=disallows_all,
        disallows_products=not _allowed(rules, product_path),
        crawl_delay=delays[0][:16] if delays else None,
    )
