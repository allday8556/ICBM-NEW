"""The Coupang catalog/products family stays complete and provider-zero."""

from pathlib import Path

from integrations.marketplaces.coupang.products import ProductEndpoint

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "documents/contracts/platforms/coupang/CATALOG_PRODUCTS.md"
PACKAGE = ROOT / "integrations/marketplaces/coupang"


def test_contract_pins_all_nine_catalog_and_twenty_two_product_sources() -> None:
    text = CONTRACT.read_text(encoding="utf-8")

    assert text.count("https://developers.coupang.com/en/api/categories/") == 6
    assert text.count("https://developers.coupang.com/en/api/brands/") == 3
    assert text.count("https://developers.coupang.com/en/api/products/") == 22
    assert len(ProductEndpoint) == 22


def test_contract_keeps_recommendation_review_and_identity_separation() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    prose = " ".join(text.split())

    for statement in (
        "never adopts a category",
        "Existing REGISTER metadata review/adoption remains the sole owner",
        "SellerProductId",
        "ProductId",
        "VendorItemId",
        "unitCount` is not inventory",
    ):
        assert statement in prose


def test_family_has_no_network_stack_or_live_authority() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    prose = " ".join(text.split())
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PACKAGE / "catalog.py", PACKAGE / "products.py")
    )

    assert "FakeTransport" in source
    assert "import httpx" not in source
    assert "import requests" not in source
    assert "import socket" not in source
    assert "app.platform.core.egress" not in source
    assert "no HTTP client, socket, egress grant, real credential or provider call" in prose
