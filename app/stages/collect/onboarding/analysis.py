"""「사이트 분석」: what one trial reads from one captured product page (ADR-0036 §5, slice O3).

Pure: a sanitized capture in, a report out. It reads only through the site's bound collection (the
template's own field parser, identity rule and image-role rules under the site's current words) and
the template's analysis hooks (its row scanner and its structure rules). It requests nothing,
stores nothing and decides nothing canonical: the report is never a ``ProductFactsRevision``
(OB-02), and nothing here can append one.

**Field statuses.** Every supplied field is the template's own status (``CONFIRMED`` read,
``ABSENT`` the page states none, ``REVIEW_REQUIRED`` not read). The ``images`` field, which a
collection derives from fetched bytes, is a trial's own reading because a trial fetches no image:
``CONFIRMED`` when the role rules name a representative image, ``REVIEW_REQUIRED`` when they name
product images but no representative one, ``ABSENT`` when they name none.

**Value summary and fingerprint.** The summary is what the operator verifies (§7): the name, the
prices, the options, the stock, the source product number, and the image count and addresses
(queries dropped). The facts fingerprint is the SHA-256 of the canonical JSON of the statuses and
that summary: the same capture under the same words and template always yields the same one, and an
operator verification names exactly it (§3 ``onboarding_verifications``).

**Unread rows** (§5 step 5, D4). A row of the template's information list whose label no row
slot's words name, and that the template does not read by its own words, is unread. So is a
phrase of a sales-channel row that no configured phrase reads. A candidate slot is proposed by rule,
never by AI, in this order:
1. ``contains_word``: a slot word of two or more characters inside the label, or the label inside
   one (the longest such word decides; a tie between slots proposes nothing);
2. ``similar_word``: the slot word most similar to the label (``difflib`` ratio at least 0.6;
   a tie proposes nothing);
3. ``money_row``: a label ending in 가 or 금액 whose value is exactly one amount proposes ``price``;
4. a channel phrase by its words: 폐쇄몰 → closed only; a ban (불가/금지) naming 쿠팡 → Coupang, or
   스마트스토어/네이버 → SmartStore; 가능 without a ban → all allowed.
The value is kept as at most 80 characters with URL material removed.

**Observed hosts** (§5 step 5, D1). Hosts the page references: an image host by the role rules
(``image`` for product evidence, ``asset`` for layout images), a form or link to a login page on
another host (``login``), and script, style and frame sources (``asset``). The storefront host is
listed only as an image host, the one role it may still need.

**Structural problems** (§5, ADR-0035), by rule:
- ``SCRIPT_RENDERED`` / ``PRODUCT_BLOCK_NOT_FOUND``: no information row, no name and no price: the
  product is not in the page's HTML (scripts present, or not);
- ``PRICE_BLOCK_NOT_FOUND``: no price is read, no price row matched and no unread row proposes a
  price slot;
- ``PRICE_BLOCK_UNREADABLE``: a matched price row is held for review and no unread row proposes a
  price slot (the cell itself is what the template cannot read);
- ``IDENTITY_UNRESOLVED``: the product block is there, but the page states no identity a
  collection could record;
- the template's own structure rules (``DUPLICATED_ORDER_LIST``).
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from app.stages.collect.facts import IMAGES_FIELD, SUPPLIED_FIELDS, FieldFact, FieldStatus
from integrations.suppliers.collection import (
    SAMPLED_ROLES,
    DocumentView,
    ImageCandidate,
    ImageRole,
    ReadKind,
    SupplierCollection,
    UnresolvedIdentity,
)
from integrations.suppliers.platforms import PlatformAnalysis

TRIAL_SCHEME: Final = "icbm-onboarding-trial/v1"
UNREAD_LIMIT: Final = 40
HOST_LIMIT: Final = 50
IMAGE_URL_LIMIT: Final = 10
LABEL_LIMIT: Final = 60
VALUE_LIMIT: Final = 80
SIMILARITY: Final = 0.6
PRICE_SLOTS: Final = frozenset({"price", "purchase_price", "list_price"})

SCRIPT_RENDERED: Final = "SCRIPT_RENDERED"
PRODUCT_BLOCK_NOT_FOUND: Final = "PRODUCT_BLOCK_NOT_FOUND"
PRICE_BLOCK_NOT_FOUND: Final = "PRICE_BLOCK_NOT_FOUND"
PRICE_BLOCK_UNREADABLE: Final = "PRICE_BLOCK_UNREADABLE"
IDENTITY_UNRESOLVED: Final = "IDENTITY_UNRESOLVED"

_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//\S*")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# One amount as a price cell states it: an amount, then only a discount rate and a tax note.
_ONE_AMOUNT = re.compile(
    r"^(\d{1,3}(?:,\d{3})+|\d+)원?(?:\d{1,3}%)?(?:\(?(?:부가세|VAT)(?:포함|별도)\)?)?$",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[/,·\n]")
_LOGIN_PATH = re.compile(r"(?i)login|logon|signin")
_ROLE_ORDER: Final = {"image": 0, "login": 1, "asset": 2}


@dataclass(frozen=True)
class UnreadRow:
    kind: str  # "row" or "phrase"
    label: str
    value: str
    candidate: str | None
    rule: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "value": self.value,
            "candidate": self.candidate,
            "rule": self.rule,
        }


@dataclass(frozen=True)
class ObservedHost:
    host: str
    role: str  # "image", "login" or "asset"
    count: int

    def as_json(self) -> dict[str, Any]:
        return {"host": self.host, "role": self.role, "count": self.count}


@dataclass(frozen=True)
class TrialReport:
    statuses: Mapping[str, str]
    summary: Mapping[str, Any]
    # Why a field that is not read was held, quoted as the template's own evidence (bounded).
    notes: Mapping[str, str]
    fingerprint: str
    unread: tuple[UnreadRow, ...]
    hosts: tuple[ObservedHost, ...]
    structural: tuple[str, ...]


def _squash(text: str) -> str:
    return "".join(text.split())


def _bounded(text: str, limit: int) -> str:
    cleaned = " ".join(_URL_MATERIAL.sub("[URL]", text).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def facts_fingerprint(statuses: Mapping[str, str], summary: Mapping[str, Any]) -> str:
    """The fingerprint of what a trial showed (ADR-0036 §3, §7): statuses and value summary."""
    text = json.dumps(
        {"scheme": TRIAL_SCHEME, "statuses": dict(statuses), "summary": dict(summary)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the page's references


class _References(HTMLParser):
    """Script, style, frame, form and link references, and how many scripts the page holds."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []  # (kind, url)
        self.scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if tag == "script":
            self.scripts += 1
            if values.get("src"):
                self.found.append(("asset", values["src"]))
        elif tag == "link" and values.get("href"):
            self.found.append(("asset", values["href"]))
        elif tag == "iframe" and values.get("src"):
            self.found.append(("asset", values["src"]))
        elif tag == "form" and values.get("action"):
            self.found.append(("form", values["action"]))
        elif tag == "a" and values.get("href"):
            self.found.append(("link", values["href"]))

    handle_startendtag = handle_starttag


def _host_of(reference: str, base: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(urljoin(base, reference.strip()))
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not _HOST.fullmatch(host):
        return None
    return host, parts.path


def _observed(
    body: str, sample_url: str, storefront: str, candidates: Sequence[ImageCandidate]
) -> tuple[tuple[ObservedHost, ...], int]:
    counts: dict[tuple[str, str], int] = {}

    def note(host: str, role: str) -> None:
        if host == storefront and role != "image":
            return
        counts[(host, role)] = counts.get((host, role), 0) + 1

    for candidate in candidates:
        found = _host_of(candidate.url, sample_url)
        if found is not None:
            note(found[0], "image" if candidate.role in SAMPLED_ROLES else "asset")
    parser = _References()
    parser.feed(body)
    parser.close()
    for kind, reference in parser.found:
        found = _host_of(reference, sample_url)
        if found is None:
            continue
        host, path = found
        if kind == "asset":
            note(host, "asset")
        elif _LOGIN_PATH.search(path):
            note(host, "login")
    hosts = sorted(
        (ObservedHost(host, role, count) for (host, role), count in counts.items()),
        key=lambda h: (_ROLE_ORDER[h.role], h.host),
    )
    return tuple(hosts[:HOST_LIMIT]), parser.scripts


# ---------------------------------------------------------------- unread rows


def _candidate(
    label: str, value: str, words: Mapping[str, tuple[str, ...]], slots: Sequence[str]
) -> tuple[str | None, str | None]:
    squashed = _squash(label)
    contains: dict[str, int] = {}
    similar: dict[str, float] = {}
    for slot in slots:
        for word in words.get(slot, ()):
            target = _squash(word)
            if not target:
                continue
            if len(target) >= 2 and (target in squashed or squashed in target):
                contains[slot] = max(contains.get(slot, 0), len(target))
            ratio = SequenceMatcher(None, squashed, target).ratio()
            similar[slot] = max(similar.get(slot, 0.0), ratio)
    if contains:
        best = max(contains.values())
        winners = [slot for slot, size in contains.items() if size == best]
        return (winners[0], "contains_word") if len(winners) == 1 else (None, None)
    if similar:
        best_ratio = max(similar.values())
        if best_ratio >= SIMILARITY:
            winners = [slot for slot, ratio in similar.items() if ratio == best_ratio]
            return (winners[0], "similar_word") if len(winners) == 1 else (None, None)
    if squashed.endswith(("가", "금액")) and _ONE_AMOUNT.fullmatch(_squash(value)):
        return "price", "money_row"
    return None, None


def _channel_candidate(phrase: str) -> str | None:
    squashed = _squash(phrase)
    banned = "불가" in squashed or "금지" in squashed
    if "폐쇄몰" in squashed:
        return "channel_closed_only"
    if banned and "쿠팡" in squashed:
        return "channel_forbid_coupang"
    if banned and ("스마트스토어" in squashed or "네이버" in squashed):
        return "channel_forbid_smartstore"
    if not banned and "가능" in squashed:
        return "channel_all_allowed"
    return None


def _leftover(text: str, phrases: Sequence[str]) -> str:
    left = _squash(text)
    for phrase in sorted({_squash(p) for p in phrases if _squash(p)}, key=len, reverse=True):
        left = left.replace(phrase, "")
    return _SEPARATORS.sub("", left)


def _unread(
    analysis: PlatformAnalysis, body: str, words: Mapping[str, tuple[str, ...]]
) -> tuple[list[UnreadRow], list[str]]:
    """The unread rows and phrases, and the labels of the rows read as a price."""
    row_words = {word for slot in analysis.row_slots for word in words.get(slot, ())}
    channel_rows = set(words.get("sales_channel_row", ()))
    phrases = [word for slot in analysis.channel_slots for word in words.get(slot, ())]
    allowed = [_squash(word) for word in words.get("channel_all_allowed", ())]
    price_words = {word for slot in PRICE_SLOTS for word in words.get(slot, ())}
    found: list[UnreadRow] = []
    priced: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for row in analysis.rows(body):
        if row.label in price_words:
            priced.append(row.label)
        if row.label in channel_rows:
            if _squash(row.value) in allowed or not _leftover(row.value, phrases):
                continue
            for part in _SEPARATORS.split(row.value):
                if not _squash(part) or not _leftover(part, phrases):
                    continue
                entry = UnreadRow(
                    "phrase",
                    _bounded(row.label, LABEL_LIMIT),
                    _bounded(part, VALUE_LIMIT),
                    _channel_candidate(part),
                    "channel_words" if _channel_candidate(part) else None,
                )
                if ("phrase", entry.label, entry.value) not in seen:
                    seen.add(("phrase", entry.label, entry.value))
                    found.append(entry)
            continue
        if row.label in row_words or analysis.reads_label(row.label):
            continue
        slot, rule = _candidate(row.label, row.value, words, analysis.row_slots)
        entry = UnreadRow(
            "row", _bounded(row.label, LABEL_LIMIT), _bounded(row.value, VALUE_LIMIT), slot, rule
        )
        if ("row", entry.label, entry.value) not in seen:
            seen.add(("row", entry.label, entry.value))
            found.append(entry)
    return found[:UNREAD_LIMIT], priced


# ---------------------------------------------------------------- the summary


def _image_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def _images(candidates: Sequence[ImageCandidate]) -> tuple[str, dict[str, Any]]:
    product = [c for c in candidates if c.role in SAMPLED_ROLES]
    ordered = sorted(product, key=lambda c: (c.role is not ImageRole.PRIMARY, c.order))
    urls: list[str] = []
    identities: set[str] = set()
    for candidate in ordered:
        if candidate.identity in identities:
            continue
        identities.add(candidate.identity)
        url = _image_url(candidate.url)
        if url is not None and url not in urls:
            urls.append(url)
    primary = any(c.role is ImageRole.PRIMARY for c in product)
    status = (
        FieldStatus.CONFIRMED
        if primary
        else FieldStatus.REVIEW_REQUIRED
        if product
        else FieldStatus.ABSENT
    )
    return status.value, {"count": len(identities), "urls": urls[:IMAGE_URL_LIMIT]}


def _value(fact: FieldFact | None) -> dict[str, Any] | None:
    if fact is None or fact.value is None or fact.status is not FieldStatus.CONFIRMED:
        return None
    dumped: dict[str, Any] = fact.value.model_dump(mode="json")
    return dumped


def _note(fact: FieldFact | None) -> str | None:
    if fact is None or fact.status is FieldStatus.CONFIRMED:
        return None
    quoted = [e.observed for e in fact.evidence if e.observed]
    return _bounded(" / ".join(quoted[:3]), VALUE_LIMIT) if quoted else None


def analyse(
    *,
    body: str,
    sample_url: str,
    storefront_host: str,
    collection: SupplierCollection,
    analysis: PlatformAnalysis,
    words: Mapping[str, tuple[str, ...]],
) -> TrialReport:
    """One trial's report over a sanitized capture (``body``) of ``sample_url``."""
    document = DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path=urlsplit(sample_url).path or "/",
        location=None,
        content_type="text/html; charset=utf-8",
        body=body,
    )
    fields = collection.fields(document)
    candidates = collection.roles.classify(body, sample_url)
    identity = collection.identity(document, sample_url)
    statuses = {key: fields[key].status.value for key in sorted(SUPPLIED_FIELDS) if key in fields}
    image_status, image_summary = _images(candidates)
    statuses[IMAGES_FIELD] = image_status
    options = fields.get("options")
    summary: dict[str, Any] = {
        "name": (_value(fields.get("original_name")) or {}).get("text"),
        "prices": (_value(fields.get("prices")) or {}).get("prices", []),
        "options": {
            "status": None if options is None else options.status.value,
            "observed": []
            if options is None
            else [_bounded(e.observed, VALUE_LIMIT) for e in options.evidence if e.observed][:3],
        },
        "stock": (_value(fields.get("stock")) or {}).get("availability"),
        "product_id": None
        if isinstance(identity, UnresolvedIdentity)
        else identity.source_product_id,
        "images": image_summary,
    }
    notes = {key: note for key in sorted(fields) if (note := _note(fields[key])) is not None}
    unread, priced = _unread(analysis, body, words)
    hosts, scripts = _observed(body, sample_url, storefront_host, candidates)
    structural: list[str] = []
    rows = analysis.rows(body)
    read_name = statuses.get("original_name") == FieldStatus.CONFIRMED.value
    read_price = statuses.get("prices") == FieldStatus.CONFIRMED.value
    if not rows and not read_name and not read_price:
        structural.append(SCRIPT_RENDERED if scripts else PRODUCT_BLOCK_NOT_FOUND)
    else:
        proposed = any(u.candidate in PRICE_SLOTS for u in unread)
        if not read_price and not priced and not proposed:
            structural.append(PRICE_BLOCK_NOT_FOUND)
        elif (
            statuses.get("prices") == FieldStatus.REVIEW_REQUIRED.value and priced and not proposed
        ):
            structural.append(PRICE_BLOCK_UNREADABLE)
        if isinstance(identity, UnresolvedIdentity):
            structural.append(IDENTITY_UNRESOLVED)
    structural.extend(code for code in analysis.structure(body) if code not in structural)
    return TrialReport(
        statuses=statuses,
        summary=summary,
        notes=notes,
        fingerprint=facts_fingerprint(statuses, summary),
        unread=tuple(unread),
        hosts=hosts,
        structural=tuple(structural),
    )
