"""Synthetic support for the Adaptive core tests (ADR-0017, P1).

An invented supplier ``synmart`` and its pages, profiles, operator expectations and scopes, plus a
second invented supplier ``synhook2`` whose pages only its synthetic hooks can read. Nothing here
is a real page, account, person or product, and nothing reaches a network.
"""

import json
import re
from pathlib import Path
from typing import Any

from app.collect.adaptive.capture import (
    OperatorScope,
    RegionClass,
    ValidationSample,
    capture_sample,
    detect_regions,
)
from app.collect.adaptive.hooks import CANNOT_PARSE, HookManifest
from app.collect.adaptive.profiles import (
    Bundle,
    ExtractionProfileRevision,
    PageTemplateRevision,
    profile_digest,
    profile_document,
    resolve_bundle,
)
from app.collect.adaptive.validation import NegativeClass

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adaptive"
SUPPLIER = "synmart"
HOOKED_SUPPLIER = "synhook2"
SAMPLES = ("on_sale", "sold_out", "optioned")
# Test-level knowledge only; an operator expectation never names a profile-owned template.
TEMPLATE_OF = {"on_sale": "plain", "sold_out": "plain", "optioned": "choice"}
BOUNDARY = "goods-view"
SPEC = "section.goods-view table.spec"
LEGAL = "section.goods-view table.legal"
HOOK_REVISION = "synhook2-hooks-1"
Negatives = dict[NegativeClass, str]


def page(name: str) -> str:
    return (FIXTURES / "pages" / f"{name}.html").read_text("utf-8")


def expected(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / "expected" / f"{name}.json").read_text("utf-8"))
    return loaded


def negative_pages() -> Negatives:
    return {NegativeClass.LOGIN: page("login"), NegativeClass.NON_PRODUCT: page("listing")}


# ---------------------------------------------------------------- profiles


def _row(container: str, vocabulary: str) -> dict[str, Any]:
    return {"kind": "LABELLED_ROW", "container": container, "vocabulary": vocabulary}


def template(key: str, *, choice: bool, supplier: str = SUPPLIER) -> dict[str, Any]:
    required = ["section.goods-view", "h2.goods-name", "table.spec", "div.choice"]
    return {
        "schema_version": "icbm-profile/v1",
        "kind": "PAGE_TEMPLATE",
        "supplier_key": supplier,
        "template_key": key,
        "signature": {
            "required": [*required, "div.choice select"] if choice else required,
            "forbidden": [] if choice else ["div.choice select"],
        },
        "fields": {
            "original_name": {
                "primary": {"kind": "TEXT", "locator": "section.goods-view h2.goods-name"},
                "alternatives": [
                    {
                        "kind": "EMBEDDED",
                        "source": "JSON_LD",
                        "json_ld_type": "Product",
                        "path": ["name"],
                    }
                ],
            },
            "prices": {"primary": _row(SPEC, "price_labels"), "cardinality": "MANY"},
            "shipping": {"primary": _row(SPEC, "shipping_labels"), "absent_when": SPEC},
            "minimum_sale_price": {"primary": _row(SPEC, "minimum_labels"), "absent_when": SPEC},
            "brand": {"primary": _row(SPEC, "brand_labels"), "absent_when": SPEC},
            "origin": {"primary": _row(SPEC, "origin_labels"), "absent_when": SPEC},
            "manufacturer": {"primary": _row(LEGAL, "maker_labels"), "absent_when": LEGAL},
            "notice": {"primary": _row(LEGAL, "notice_labels"), "cardinality": "MANY"},
            "detail_description": {"primary": {"kind": "TEXT", "locator": "div.description p"}},
            "quantity_tiers": {
                "primary": _row("section.goods-view table.bulk", "tier_labels"),
                "absent_when": "section.goods-view",
            },
        },
        "stock_scope": "section.goods-view div.buttons",
        "options_container": "section.goods-view div.choice",
        "image_regions": [
            {"name": "cover", "locator": "div.photos img.cover"},
            {"name": "detail", "locator": "div.description img"},
        ],
    }


def epr(templates: list[str], *, supplier: str = SUPPLIER, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "icbm-profile/v1",
        "kind": "EXTRACTION_PROFILE",
        "supplier_key": supplier,
        "identity": {
            "sources": [
                {"kind": "ATTRIBUTE", "locator": "meta[name=goods-code]", "attribute": "content"},
                {
                    "kind": "EMBEDDED",
                    "source": "JSON_LD",
                    "json_ld_type": "Product",
                    "path": ["sku"],
                },
            ]
        },
        "vocabularies": {
            "price_labels": ["판매가", "정가"],
            "shipping_labels": ["배송비"],
            "minimum_labels": ["최저판매가"],
            "brand_labels": ["브랜드"],
            "origin_labels": ["원산지"],
            "maker_labels": ["제조원", "제조사"],
            "notice_labels": ["제조원", "원산지"],
            "tier_labels": ["수량별 할인", "수량별 가격"],
        },
        "purchase_controls": ["a.btn-order", "button.btn-basket"],
        "sold_out_words": ["품절", "SOLD OUT"],
        "image_roles": [
            {"region": "cover", "role": "REPRESENTATIVE", "take": "FIRST"},
            {"region": "detail", "role": "DETAIL", "take": "ALL"},
        ],
        "templates": templates,
    }
    document.update(overrides)
    return document


def documents(*templates: dict[str, Any]) -> dict[str, str]:
    """Canonical template documents keyed by their own recomputed digest."""
    parsed = [PageTemplateRevision.model_validate_json(json.dumps(t)) for t in templates]
    return {profile_digest(model): profile_document(model) for model in parsed}


def bundle_of(
    templates: list[dict[str, Any]], *, supplier: str = SUPPLIER, **overrides: Any
) -> Bundle:
    texts = documents(*templates)
    root = ExtractionProfileRevision.model_validate_json(
        json.dumps(epr(list(texts), supplier=supplier, **overrides))
    )
    return resolve_bundle(profile_document(root), texts)


def synmart_bundle(**overrides: Any) -> Bundle:
    return bundle_of(
        [template("plain", choice=False), template("choice", choice=True)], **overrides
    )


# ---------------------------------------------------------------- samples


def scope_for(html: str, boundary: str = BOUNDARY) -> OperatorScope:
    """The operator approves one boundary and confirms the regions detected inside it."""
    confirmed = tuple(
        found
        for found, region in detect_regions(html, boundary)
        if region is not RegionClass.NAVIGATION
    )
    return OperatorScope("operator:synthetic", "2026-09-25T00:00:00Z", boundary, confirmed)


def sample(name: str) -> ValidationSample:
    html = page(name)
    return capture_sample(html, scope_for(html), expected(name))


def samples() -> list[ValidationSample]:
    return [sample(name) for name in SAMPLES]


# ---------------------------------------------------------------- synthetic hooks


_CODE = re.compile(r"^CODE\[([A-Z]{2})/(\d{4})\]$")
_CONDITIONAL = re.compile(r"^(\d+)만원 이상 무료 / 미만 (\d{1,3}(?:,\d{3})*)원$")


def decode_code(text: str) -> Any:
    match = _CODE.fullmatch(text)
    return f"{match.group(1)}-{match.group(2)}" if match else CANNOT_PARSE


def conditional_shipping(text: str) -> Any:
    match = _CONDITIONAL.fullmatch(text)
    if match is None:
        return CANNOT_PARSE
    return {
        "kind": "CONDITIONAL",
        "policy_text": text,
        "fee_krw": int(match.group(2).replace(",", "")),
        "free_over_krw": int(match.group(1)) * 10000,
    }


def hook_manifest(revision: str = HOOK_REVISION, fingerprint: str = "a" * 64) -> HookManifest:
    return HookManifest(
        HOOKED_SUPPLIER,
        revision,
        fingerprint,
        {"decode_code": decode_code, "conditional_shipping": conditional_shipping},
    )


def hooked_bundle(revision: str = HOOK_REVISION) -> Bundle:
    return bundle_of(
        [template("plain", choice=False, supplier=HOOKED_SUPPLIER)],
        supplier=HOOKED_SUPPLIER,
        identity={
            "sources": [
                {"kind": "ATTRIBUTE", "locator": "meta[name=goods-code]", "attribute": "content"}
            ]
        },
        hooks=[
            {
                "hook_point": "identity_decode",
                "target": "identity",
                "format_class": "COMPOSITE_CODE",
                "hook_name": "decode_code",
                "hook_revision": revision,
            },
            {
                "hook_point": "value_parse",
                "target": "shipping",
                "format_class": "CONDITIONAL_POLICY_TEXT",
                "hook_name": "conditional_shipping",
                "hook_revision": revision,
            },
        ],
    )
