"""Synthetic profile revisions for the invented supplier ``synthetic`` (and ``synhook``)."""

from typing import Any

SUPPLIER = "synthetic"
HOOKED_SUPPLIER = "synhook"
INFO = "div.product-detail table.info"
NOTICE = "div.product-detail table.notice"


def _row(container: str, vocabulary: str) -> dict[str, Any]:
    return {"kind": "LABEL_ROW", "container": container, "vocabulary": vocabulary}


def _fields() -> dict[str, Any]:
    return {
        "original_name": {
            "primary": {"kind": "TEXT", "selector": "div.product-detail h1.product-title"},
            "alternatives": [
                {"kind": "EMBEDDED", "block": "JSON_LD", "ld_type": "Product", "path": ["name"]}
            ],
        },
        "prices": {"primary": _row(INFO, "price_labels"), "cardinality": "MANY"},
        "shipping": {"primary": _row(INFO, "shipping_labels"), "absent_when_present": INFO},
        "minimum_sale_price": {
            "primary": _row(INFO, "minimum_labels"),
            "absent_when_present": INFO,
        },
        "brand": {"primary": _row(INFO, "brand_labels"), "absent_when_present": INFO},
        "origin": {"primary": _row(INFO, "origin_labels"), "absent_when_present": INFO},
        "manufacturer": {
            "primary": _row(NOTICE, "manufacturer_labels"),
            "absent_when_present": NOTICE,
        },
        "notice": {"primary": _row(NOTICE, "notice_labels"), "cardinality": "MANY"},
        "detail_description": {"primary": {"kind": "TEXT", "selector": "div.detail-body p"}},
        "quantity_tiers": {
            "primary": _row("div.product-detail table.tiers", "tier_labels"),
            "absent_when_present": "div.product-detail",
        },
    }


def ptr(template_key: str, *, optioned: bool, supplier: str = SUPPLIER) -> dict[str, Any]:
    required = ["div.product-detail", "h1.product-title", "table.info", "div.options"]
    return {
        "schema_version": "icbm-profile/v1",
        "kind": "PAGE_TEMPLATE",
        "supplier_key": supplier,
        "template_key": template_key,
        "signature": {
            "required": [*required, "div.options select"] if optioned else required,
            "forbidden": [] if optioned else ["div.options select"],
        },
        "fields": _fields(),
        "stock": {"scope": "div.product-detail div.actions"},
        "options": {"container": "div.product-detail div.options"},
        "image_regions": [
            {"name": "main", "selector": "div.gallery img.main"},
            {"name": "detail", "selector": "div.detail-body img"},
        ],
    }


def epr(templates: list[str], *, supplier: str = SUPPLIER, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "icbm-profile/v1",
        "kind": "EXTRACTION_PROFILE",
        "supplier_key": supplier,
        "identity": {
            "sources": [
                {"kind": "ATTRIBUTE", "selector": "meta[name=product-id]", "attribute": "content"},
                {"kind": "EMBEDDED", "block": "JSON_LD", "ld_type": "Product", "path": ["sku"]},
            ]
        },
        "vocabularies": {
            "price_labels": ["판매가", "소비자가"],
            "shipping_labels": ["배송비"],
            "minimum_labels": ["최저판매가", "최저지도가"],
            "brand_labels": ["브랜드"],
            "origin_labels": ["원산지"],
            "manufacturer_labels": ["제조사"],
            "notice_labels": ["제조사", "원산지"],
            "tier_labels": ["수량별 가격", "수량별 할인"],
        },
        "stock": {
            "purchase_controls": ["button.btn-buy", "button.btn-cart"],
            "sold_out_words": ["품절", "일시품절", "SOLD OUT"],
        },
        "image_roles": [
            {"region": "main", "role": "REPRESENTATIVE", "take": "FIRST"},
            {"region": "detail", "role": "DETAIL", "take": "ALL"},
        ],
        "templates": templates,
    }
    document.update(overrides)
    return document


def hooked_epr(templates: list[str], hook_revision: str) -> dict[str, Any]:
    """The ``synhook`` supplier: an identity only a hook decodes, and a conditional shipping text
    only a hook reads. Two (hook_point, target) bindings — at the G6 cap, not over it."""
    return epr(
        templates,
        supplier=HOOKED_SUPPLIER,
        identity={
            "sources": [
                {"kind": "ATTRIBUTE", "selector": "meta[name=product-id]", "attribute": "content"}
            ]
        },
        hooks=[
            {
                "hook_point": "identity_decode",
                "target": "identity",
                "format_class": "COMPOSITE_CODE",
                "hook_name": "decode_item_number",
                "hook_revision": hook_revision,
            },
            {
                "hook_point": "value_parse",
                "target": "shipping",
                "format_class": "CONDITIONAL_POLICY_TEXT",
                "hook_name": "parse_conditional_shipping",
                "hook_revision": hook_revision,
            },
        ],
    )
