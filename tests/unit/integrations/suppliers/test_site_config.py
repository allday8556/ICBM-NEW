"""Site configurations and their binding to platform templates (ADR-0030 §3, §4, §7).

Every site here is written in the test; no supplier is contacted.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.stages.collect.extension.policy import CapturePolicySource, parse_policy
from integrations.suppliers.collection import DocumentView, ReadKind
from integrations.suppliers.site_config import SiteConfig, SiteConfigError, SiteStatus, parse_site
from integrations.suppliers.sites import (
    PLATFORM_DIRECTORY,
    SITE_DIRECTORY,
    TEMPLATES,
    bind_sites,
    capture_policy_bytes,
)


def raw(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": "icbm-supplier-site/v1",
        "supplier_key": "example",
        "display_name": "Example",
        "platform": "cafe24",
        "base_url": "https://example.co.kr",
        "storefront_host": "example.co.kr",
        "image_hosts": ["example.co.kr"],
        "product_path_form": "seo",
        "status": "RECON",
        "revision": "example-1",
    }
    for key, value in changes.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    return document


def encoded(**changes: Any) -> bytes:
    return json.dumps(raw(**changes), ensure_ascii=False).encode("utf-8")


def site(**changes: Any) -> SiteConfig:
    """A valid site; label and region overrides may be given as already-parsed values."""
    labels = changes.pop("label_overrides", None)
    regions = changes.pop("region_overrides", None)
    parsed = parse_site(encoded(**changes), "example")
    if labels is not None:
        parsed = replace(parsed, label_overrides={k: tuple(v) for k, v in labels.items()})
    if regions is not None:
        parsed = replace(parsed, region_overrides=regions)
    return parsed


def test_a_minimal_site_parses() -> None:
    parsed = parse_site(encoded(), "example")
    assert parsed.status is SiteStatus.RECON and not parsed.active
    assert parsed.image_hosts == frozenset({"example.co.kr"})
    assert len(parsed.digest) == 64


@pytest.mark.parametrize(
    ("changes", "stem"),
    [
        ({"unexpected": 1}, "example"),
        ({"revision": None}, "example"),
        ({}, "other"),
        ({"supplier_key": "Bad Key"}, "Bad Key"),
        ({"base_url": "http://example.co.kr"}, "example"),
        ({"base_url": "https://example.co.kr/shop"}, "example"),
        ({"base_url": "https://example.co.kr/"}, "example"),
        ({"base_url": "https://user:secret@example.co.kr"}, "example"),
        ({"base_url": "https://user@example.co.kr"}, "example"),
        ({"base_url": "https://example.co.kr:8443"}, "example"),
        ({"base_url": "https://example.co.kr#top"}, "example"),
        ({"storefront_host": "other.co.kr"}, "example"),
        ({"image_hosts": ["*.example.co.kr"]}, "example"),
        ({"image_hosts": []}, "example"),
        ({"image_hosts": ["example.co.kr", "example.co.kr"]}, "example"),
        ({"status": "LIVE"}, "example"),
        ({"status": "ACTIVE"}, "example"),
        ({"label_overrides": {"price": ["도.*가"]}}, "example"),
        ({"label_overrides": {"price": ["<b>가</b>"]}}, "example"),
        ({"label_overrides": {"price": []}}, "example"),
        ({"region_overrides": {"detail": {"by": "css", "token": "x"}}}, "example"),
        ({"region_overrides": {"detail": {"by": "id", "token": "a b"}}}, "example"),
        ({"limits": {"max_image_bytes": 1.5}}, "example"),
        ({"limits": {"max_image_bytes": 0}}, "example"),
        ({"limits": {"same_product_interval_s": float("inf")}}, "example"),
        ({"limits": {"min_queue_interval_s": float("nan")}}, "example"),
        ({"limits": {"unknown": 1}}, "example"),
        ({"seller_code_convention": "UP-{name}"}, "example"),
        ({"recon_record": "../secrets.md"}, "example"),
    ],
)
def test_an_invalid_site_is_refused(changes: dict[str, Any], stem: str) -> None:
    with pytest.raises(SiteConfigError):
        parse_site(encoded(**changes), stem)


def test_an_active_site_names_its_reconnaissance_record() -> None:
    parsed = parse_site(
        encoded(status="ACTIVE", recon_record="documents/acceptance/suppliers/example-recon.md"),
        "example",
    )
    assert parsed.active


def _write(directory: Path, name: str, data: bytes) -> None:
    (directory / f"{name}.json").write_bytes(data)


def test_a_valid_site_is_bound_to_its_template(tmp_path: Path) -> None:
    _write(tmp_path, "example", encoded(label_overrides={"price": ["도매가"]}))
    bound, problems = bind_sites(tmp_path)
    assert problems == ()
    (only,) = bound
    assert only.extractor_revision == "cafe24-1+example-1"
    assert only.collection.supplier_key == "example"
    assert only.collection.roles.identity == only.extractor_revision
    assert only.collection.profile.storefront_host == "example.co.kr"
    assert only.collection.profile.is_product_path("/product/abc/12/")
    assert not only.collection.profile.is_product_path("/product/detail.html")
    assert only.definition.profile.egress_hosts == {"example.co.kr", "login2.cafe24ssl.com"}
    view = DocumentView(ReadKind.PRODUCT_READ, 200, "/product/a/12/", None, "text/html", "")
    assert set(only.collection.fields(view)) >= {"original_name", "prices", "stock"}


def test_an_invalid_file_leaves_only_its_own_site_out(tmp_path: Path) -> None:
    _write(tmp_path, "example", encoded())
    _write(tmp_path, "broken", b"{not json")
    _write(tmp_path, "kmretail", encoded(supplier_key="kmretail"))
    _write(tmp_path, "unknown", encoded(supplier_key="unknown", platform="makeshop"))
    _write(tmp_path, "slots", encoded(supplier_key="slots", label_overrides={"colour": ["색"]}))
    _write(tmp_path, "form", encoded(supplier_key="form", product_path_form="detail"))
    _write(tmp_path, "raised", encoded(supplier_key="raised", limits={"max_image_refs": 99}))
    _write(
        tmp_path, "faster", encoded(supplier_key="faster", limits={"same_product_interval_s": 61})
    )
    bound, problems = bind_sites(tmp_path)
    assert [site.config.supplier_key for site in bound] == ["example", "faster"]
    assert {problem.split(":")[0] for problem in problems} == {
        "broken.json",
        "kmretail.json",
        "unknown.json",
        "slots.json",
        "form.json",
        "raised.json",
    }


def test_a_raised_limit_needs_the_owner_s_decision(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "example",
        encoded(limits={"max_image_refs": 40}, limit_decision="Issue 219 comment 1"),
    )
    (only,), problems = bind_sites(tmp_path)
    assert problems == ()
    assert only.collection.profile.limits.max_image_refs == 40


def test_any_change_to_the_site_file_changes_its_identity(tmp_path: Path) -> None:
    _write(tmp_path, "example", encoded())
    (first,), _ = bind_sites(tmp_path)
    _write(tmp_path, "example", encoded(image_hosts=["example.co.kr", "img.example.co.kr"]))
    (second,), _ = bind_sites(tmp_path)
    assert first.extractor_revision == second.extractor_revision
    assert first.extractor_fingerprint != second.extractor_fingerprint


def test_a_site_s_capture_policy_is_its_template_s_with_its_own_host(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "example",
        encoded(region_overrides={"detail": {"by": "id", "token": "goodsDetail"}}),
    )
    (only,), _ = bind_sites(tmp_path)
    policy = parse_policy(capture_policy_bytes(only), "example")
    assert policy.host == "example.co.kr"
    assert policy.revision == "cafe24-capture-1.example-1"
    assert [(r.by, r.token) for r in policy.allowed_regions] == [("id", "goodsDetail")]
    served = CapturePolicySource(PLATFORM_DIRECTORY, sites={"example": capture_policy_bytes(only)})
    assert served.load("example").digest == policy.digest


def test_this_build_s_sites_all_bind() -> None:
    _, problems = bind_sites(SITE_DIRECTORY)
    assert problems == ()
    assert set(TEMPLATES) == {"cafe24"}
