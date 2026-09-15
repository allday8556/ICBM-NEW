"""Sanitized structural inventory of reconnaissance documents (ADR-0010 §5).

What reconnaissance must record can be told from the page's *structure*: the product path form,
where identity and each fact live, the image hosts, and whether static HTML is enough. This
module reads bodies in memory and emits only structure:
* tag/id/class selectors;
* attribute, meta and JSON-LD key names;
* counts, booleans and hosts;
* paths with every digit run masked.

It never emits text content, attribute values, prices, names or member data.
``assert_sanitized`` refuses any output that contains, in any encoding, a string the caller
names as secret.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from app.collect.urls import secret_looking
from app.system.secret_scan import variants

# ASCII fragments looked for in id/class names, per fact the parser will need.
SELECTOR_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "name": ("name", "title", "subject"),
    "price": ("price", "cost", "sale"),
    "options": ("option", "opt", "select", "variant"),
    "stock": ("sold", "stock", "qty", "quantity"),
    "purchase": ("buy", "cart", "basket", "order", "purchase"),
    "shipping": ("delivery", "ship", "deliv"),
    "images": ("thumb", "image", "img", "photo", "gallery", "zoom"),
    "detail": ("detail", "desc", "content", "info"),
    "notice": ("notice", "gosi", "spec"),
    "brand": ("brand", "maker", "manufact", "origin", "supply"),
    "identity": ("product", "prd", "goods", "item", "sku", "code"),
}
# Public UI labels whose presence in visible text is recorded as a boolean only.
TEXT_SIGNALS: Mapping[str, tuple[str, ...]] = {
    "sold_out_label": ("품절", "sold out", "soldout"),
    "cart_label": ("장바구니",),
    "buy_label": ("구매하기", "바로구매"),
    "shipping_label": ("배송비",),
    "origin_label": ("원산지",),
    "manufacturer_label": ("제조사", "제조원"),
    "brand_label": ("브랜드",),
    "notice_label": ("상품정보제공고시", "상품정보고시", "정보제공고시"),
    "minimum_price_label": ("최소판매가", "최저판매가", "최소 판매가", "최저 판매가"),
    "tier_label": ("수량별", "구간별", "묶음"),
    "option_label": ("옵션",),
}
TERMS_SIGNALS = ("크롤링", "스크래핑", "자동화", "매크로", "로봇", "robot", "수집", "무단", "복제")
_POLICY_TEXT = {"terms": ("이용약관", "terms"), "privacy": ("개인정보", "privacy")}
_MAX_SELECTORS = 12
_DIGITS = re.compile(r"\d+")


def mask(path: str) -> str:
    return _DIGITS.sub("{n}", path)


def _selector(tag: str, attrs: Mapping[str, str]) -> str:
    element_id = attrs.get("id", "").strip()
    classes = [c for c in attrs.get("class", "").split() if c]
    text = tag + (f"#{element_id}" if element_id else "") + "".join(f".{c}" for c in classes[:3])
    return mask(text)[:80]


class _Scanner(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.selectors: dict[str, set[str]] = {key: set() for key in SELECTOR_KEYWORDS}
        self.meta_keys: set[str] = set()
        self.forms: list[dict[str, Any]] = []
        self.inputs: list[tuple[str, str]] = []  # (name, value), in memory only
        self.data_attributes: list[tuple[str, str]] = []
        self.images: list[str] = []
        self.script_hosts: Counter[str] = Counter()
        self.inline_scripts = 0
        self.jsonld: list[str] = []
        self.anchors: list[tuple[str, str]] = []  # (href, text), in memory only
        self.text_parts: list[str] = []
        self.script_text_parts: list[str] = []
        self._in: list[str] = []
        self._anchor: tuple[str, list[str]] | None = None
        self._jsonld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self._in.append(tag)
        haystack = f"{values.get('id', '')} {values.get('class', '')}".lower()
        for fact, keywords in SELECTOR_KEYWORDS.items():
            if any(keyword in haystack for keyword in keywords):
                self.selectors[fact].add(_selector(tag, values))
        for name, value in values.items():
            if name.startswith("data-") and value:
                self.data_attributes.append((name, value))
        if tag == "meta":
            key = values.get("property") or values.get("name") or values.get("itemprop")
            if key:
                self.meta_keys.add(key)
                if values.get("content"):
                    self.data_attributes.append((f"meta:{key}", values["content"]))
        elif tag == "form":
            action = urlsplit(urljoin(self.base_url, values.get("action", ""))).path
            self.forms.append(
                {"action": mask(action), "method": values.get("method", "get").lower()}
            )
        elif tag in ("input", "select", "textarea", "button"):
            field_name = values.get("name")
            if field_name:
                self.inputs.append((field_name, values.get("value", "")))
        elif tag == "img":
            for attribute in ("src", "data-src", "ec-data-src", "data-original"):
                if values.get(attribute):
                    self.images.append(urljoin(self.base_url, values[attribute]))
            for candidate in values.get("srcset", "").split(","):
                if candidate.strip():
                    self.images.append(urljoin(self.base_url, candidate.split()[0]))
        elif tag == "script":
            if values.get("src"):
                host = urlsplit(urljoin(self.base_url, values["src"])).hostname
                self.script_hosts[host or "?"] += 1
            else:
                self.inline_scripts += 1
            self._jsonld = values.get("type", "").lower() == "application/ld+json"
        elif tag == "a":
            self._anchor = (values.get("href", ""), [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            href, text = self._anchor
            self.anchors.append((href, " ".join(text)))
            self._anchor = None
        if tag == "script":
            self._jsonld = False
        if self._in:
            self._in.pop()

    def handle_data(self, data: str) -> None:
        if self._in and self._in[-1] == "script":
            if self._jsonld:
                self.jsonld.append(data)
            self.script_text_parts.append(data)
            return
        if self._in and self._in[-1] == "style":
            return
        self.text_parts.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data.strip())


def _url_parts(url: str) -> dict[str, str]:
    """Where each digit run of the product URL sits: {value: location}. In memory only."""
    parts = urlsplit(url)
    found: dict[str, str] = {}
    for index, segment in enumerate(parts.path.split("/")):
        for run in _DIGITS.findall(segment):
            found.setdefault(run, f"path[{index}]")
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        for run in _DIGITS.findall(value):
            found.setdefault(run, f"query:{key}")
    return found


def _jsonld_shape(blocks: Iterable[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    shapes: list[dict[str, Any]] = []
    scalars: list[tuple[str, str]] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            shapes.append({"parse": "invalid"})
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items if isinstance(items, list) else [items]:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            shapes.append(
                {
                    "type": item.get("@type"),
                    "keys": sorted(k for k in item if isinstance(k, str)),
                    "offer_keys": sorted(offers) if isinstance(offers, dict) else [],
                }
            )
            scalars.extend(
                (f"jsonld:{k}", str(v)) for k, v in item.items() if isinstance(v, str | int)
            )
    return shapes, scalars


def document_inventory(body: str, url: str) -> dict[str, Any]:
    """The sanitized structure of one product document; ``url`` is its canonical product URL."""
    scanner = _Scanner(url)
    scanner.feed(body)
    scanner.close()
    visible = " ".join(scanner.text_parts)
    scripted = " ".join(scanner.script_text_parts)
    jsonld, jsonld_scalars = _jsonld_shape(scanner.jsonld)
    digit_runs = _url_parts(url)
    identity = sorted(
        {
            (f"{'input' if (name, value) in scanner.inputs else 'attr'}:{name}", digit_runs[value])
            for name, value in [*scanner.inputs, *scanner.data_attributes, *jsonld_scalars]
            if value in digit_runs
        }
    )
    hosts: Counter[str] = Counter()
    query_keys: dict[str, set[str]] = {}
    secret_keys: Counter[str] = Counter()
    with_query: Counter[str] = Counter()
    for image in scanner.images:
        parts = urlsplit(image)
        host = parts.hostname or "?"
        hosts[host] += 1
        if parts.query:
            with_query[host] += 1
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if secret_looking(key):
                secret_keys[host] += 1
            else:
                query_keys.setdefault(host, set()).add(key)
    return {
        "path_form": mask(urlsplit(url).path),
        "bytes": len(body.encode("utf-8")),
        "selectors": {
            fact: sorted(found)[:_MAX_SELECTORS] for fact, found in scanner.selectors.items()
        },
        "meta_keys": sorted(scanner.meta_keys),
        "jsonld": jsonld,
        "forms": scanner.forms,
        "input_names": sorted({name for name, _ in scanner.inputs}),
        "data_attribute_names": sorted({name for name, _ in scanner.data_attributes}),
        "identity_candidates": [{"source": s, "equals_url": where} for s, where in identity],
        "images": {
            "count": len(scanner.images),
            "hosts": dict(sorted(hosts.items())),
            "with_query": dict(sorted(with_query.items())),
            "plain_query_keys": {h: sorted(k) for h, k in sorted(query_keys.items())},
            "secret_looking_query_keys": dict(sorted(secret_keys.items())),
        },
        "scripts": {
            "inline": scanner.inline_scripts,
            "hosts": dict(sorted(scanner.script_hosts.items())),
        },
        "text_signals": {
            signal: any(word in visible for word in words) for signal, words in TEXT_SIGNALS.items()
        },
        "script_only_signals": sorted(
            signal
            for signal, words in TEXT_SIGNALS.items()
            if not any(w in visible for w in words) and any(w in scripted for w in words)
        ),
        "policy_links": {
            kind: sorted({mask(p) for p in paths})
            for kind, paths in policy_links(body, url).items()
        },
    }


def policy_links(body: str, url: str) -> dict[str, list[str]]:
    """Storefront paths of links whose text names a policy document (unmasked, in memory)."""
    scanner = _Scanner(url)
    scanner.feed(body)
    scanner.close()
    storefront = urlsplit(url).hostname
    found: dict[str, list[str]] = {kind: [] for kind in _POLICY_TEXT}
    for href, text in scanner.anchors:
        target = urlsplit(urljoin(url, href))
        if target.hostname != storefront or target.scheme != "https":
            continue
        lowered = text.lower()
        for kind, words in _POLICY_TEXT.items():
            if any(word in lowered for word in words) and target.path not in found[kind]:
                found[kind].append(target.path)
    return found


def image_urls(body: str, url: str) -> list[str]:
    """Absolute image URLs of a document, in source order (in memory only; may be signed)."""
    scanner = _Scanner(url)
    scanner.feed(body)
    scanner.close()
    return list(dict.fromkeys(scanner.images))


# ---------------------------------------------------------------- robots.txt and terms


def parse_robots(text: str) -> dict[str, list[tuple[str, str]]]:
    """Rules per user-agent: {agent: [(directive, path)]} (robots.txt is a public document)."""
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    collecting_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not collecting_agents:
                agents = []
            agents.append(value.lower())
            collecting_agents = True
            for agent in agents:
                groups.setdefault(agent, [])
        elif field in ("allow", "disallow"):
            collecting_agents = False
            for agent in agents:
                groups.setdefault(agent, []).append((field, value))
        else:
            collecting_agents = False
    return groups


def _rule_matches(rule: str, path: str) -> bool:
    pattern = "".join(".*" if c == "*" else re.escape(c) for c in rule.rstrip("$"))
    return re.match(pattern + ("$" if rule.endswith("$") else ""), path) is not None


def robots_disallows(groups: Mapping[str, list[tuple[str, str]]], path: str) -> bool:
    """Whether the ``*`` group disallows ``path`` (longest matching rule wins; allow wins ties)."""
    best: tuple[int, bool] | None = None
    for directive, rule in groups.get("*", []):
        if not rule:
            continue  # an empty Disallow allows everything
        if _rule_matches(rule, path):
            candidate = (len(rule), directive == "disallow")
            if (
                best is None
                or candidate[0] > best[0]
                or (candidate[0] == best[0] and not candidate[1])
            ):
                best = candidate
    return bool(best and best[1])


def terms_signals(text: str) -> dict[str, Any]:
    visible = " ".join(_text_of(text))
    lowered = visible.lower()
    return {
        "bytes": len(text.encode("utf-8")),
        "mentions": sorted(word for word in TERMS_SIGNALS if word.lower() in lowered),
    }


def _text_of(body: str) -> list[str]:
    scanner = _Scanner("https://terms.invalid/")
    scanner.feed(body)
    scanner.close()
    return scanner.text_parts


# ---------------------------------------------------------------- disclosure guard


def assert_sanitized(findings: object, secrets: Iterable[str]) -> None:
    """Refuse findings that would carry any named secret in any encoding (names no value)."""
    rendered = json.dumps(findings, ensure_ascii=False).encode("utf-8")
    for secret in secrets:
        if not secret:
            continue
        if any(form in rendered for family in variants(secret).values() for form in family):
            raise ValueError("findings would disclose a secret value; nothing was written")
