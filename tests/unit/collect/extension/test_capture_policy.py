"""The BrowserCapturePolicy owner (ADR-0019 §5; rulings 5906290729 B-4, B-5, B-10, B-11)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.stages.collect.extension.policy import (
    MAX_HTML_BYTES,
    MAX_IMAGE_REFS,
    MAX_NODES,
    POLICY_FILE,
    CapturePolicyRefused,
    CapturePolicySource,
    canonical_bytes,
    parse_policy,
    policy_digest,
)
from integrations.suppliers.kmretail.collect.identity import (
    CANONICAL_REL,
    CORROBORATING_META,
    IDENTITY_META,
)
from integrations.suppliers.kmretail.collection import COLLECTION
from tests.support.extension_support import KM_POLICY, REPO_ROOT, SUPPLIER

SUPPLIERS = REPO_ROOT / "integrations" / "suppliers"
# The reviewed KM policy. A change of the file changes the digest; a change without a new revision
# is what this pin refuses.
KM_REVISION = "kmretail-capture-2"
KM_DIGEST = "74670a991bf12686b837928436ebf6bae67cdb0516d5299c3eb22c1762707679"


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(KM_POLICY.read_text("utf-8"))
    return loaded


def _raw(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_the_km_policy_is_the_reviewed_one() -> None:
    policy = CapturePolicySource(SUPPLIERS).load(SUPPLIER)
    assert (policy.supplier_key, policy.revision) == (SUPPLIER, KM_REVISION)
    # B-11: exactly the canonical KM CollectionProfile host, and no other.
    assert policy.host == COLLECTION.profile.storefront_host == "kmretail.co.kr"
    # B-4: the E1 ceilings, stated by the policy and never above the server's own.
    assert (
        (policy.bounds.max_html_bytes, policy.bounds.max_nodes, policy.bounds.max_image_refs)
        == (
            MAX_HTML_BYTES,
            MAX_NODES,
            MAX_IMAGE_REFS,
        )
        == (512 * 1024, 20_000, 40)
    )
    # The digest is of the one canonical serialization, recomputed here from the file.
    assert policy.canonical == canonical_bytes(_document())
    assert policy.digest == policy_digest(policy.canonical) == KM_DIGEST


def test_the_km_head_allowance_is_exactly_the_identity_declarations() -> None:
    # B-10: from <head> only the three declarations the KM identity rule reads, and nothing else.
    policy = CapturePolicySource(SUPPLIERS).load(SUPPLIER)
    assert {(rule.tag, rule.attribute, rule.value) for rule in policy.head_allowance} == {
        ("meta", "property", IDENTITY_META),
        ("meta", "property", CORROBORATING_META),
        ("link", "rel", CANONICAL_REL),
    }
    assert policy.keeps_head("meta", {"property": "product:productId", "content": "9001"})
    assert policy.keeps_head("link", {"rel": "Canonical", "href": "https://kmretail.co.kr/x"})
    for tag, attributes in (
        ("meta", {"property": "og:title", "content": "x"}),
        ("meta", {"name": "description", "content": "x"}),
        ("link", {"rel": "stylesheet", "href": "/a.css"}),
        ("script", {}),
        ("title", {}),
    ):
        assert not policy.keeps_head(tag, attributes), tag


def test_the_policy_is_topology_only() -> None:
    # ADR-0019 §5: product root, regions, attributes, bounds and a revision. No host list beyond
    # its own host, no path, query, pacing or transport, and no source fact or extraction rule.
    assert set(_document()) == {
        "schema",
        "supplier_key",
        "revision",
        "host",
        "product_root",
        "allowed_regions",
        "excluded_regions",
        "excluded_tags",
        "allowed_attributes",
        "head_allowance",
        "bounds",
    }
    policy = CapturePolicySource(SUPPLIERS).load(SUPPLIER)
    # Nothing that could carry a secret, a form value or a link target is kept.
    kept = set().union(*policy.allowed_attributes.values())
    assert not kept & {"value", "onclick", "action", "name", "title", "alt", "data-member-token"}
    assert "href" not in policy.allowed_attributes["*"]
    assert {"script", "style", "iframe", "textarea"} <= policy.excluded_tags


def test_the_digest_is_independent_of_how_the_file_is_written() -> None:
    document = _document()
    pretty = json.dumps(document, ensure_ascii=False, indent=8).encode("utf-8")
    reordered = json.dumps(dict(reversed(list(document.items()))), ensure_ascii=False).encode()
    assert (
        parse_policy(pretty, SUPPLIER).digest
        == parse_policy(reordered, SUPPLIER).digest
        == parse_policy(KM_POLICY.read_bytes(), SUPPLIER).digest
    )
    changed = copy.deepcopy(document)
    changed["excluded_tags"].append("form")
    assert (
        parse_policy(_raw(changed), SUPPLIER).digest
        != parse_policy(_raw(document), SUPPLIER).digest
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.pop("bounds"), "CAPTURE_POLICY_INVALID"),
        (lambda d: d.update(extra=1), "CAPTURE_POLICY_INVALID"),
        (lambda d: d.update(schema="other/v1"), "CAPTURE_POLICY_INVALID"),
        (lambda d: d.update(supplier_key="othershop"), "CAPTURE_POLICY_INVALID"),
        (lambda d: d.update(host="*.kmretail.co.kr"), "CAPTURE_POLICY_INVALID"),
        (lambda d: d.update(product_root={"by": "css", "token": "x"}), "CAPTURE_POLICY_INVALID"),
        (
            lambda d: d.update(product_root={"by": "class", "token": "a b"}),
            "CAPTURE_POLICY_INVALID",
        ),
        (lambda d: d.update(allowed_attributes={}), "CAPTURE_POLICY_INVALID"),
        (lambda d: d["head_allowance"].append({"tag": "meta"}), "CAPTURE_POLICY_INVALID"),
        # A missing, zero, unbounded or non-integer cap refuses; nothing falls back to a default.
        (lambda d: d["bounds"].pop("max_nodes"), "CAPTURE_POLICY_BOUND_INVALID"),
        (lambda d: d["bounds"].update(max_nodes=0), "CAPTURE_POLICY_BOUND_INVALID"),
        (lambda d: d["bounds"].update(max_nodes=MAX_NODES + 1), "CAPTURE_POLICY_BOUND_INVALID"),
        (
            lambda d: d["bounds"].update(max_html_bytes=MAX_HTML_BYTES + 1),
            "CAPTURE_POLICY_BOUND_INVALID",
        ),
        (lambda d: d["bounds"].update(max_image_refs=True), "CAPTURE_POLICY_BOUND_INVALID"),
        (lambda d: d["bounds"].update(max_image_refs=None), "CAPTURE_POLICY_BOUND_INVALID"),
    ],
)
def test_an_invalid_policy_is_refused_never_repaired(mutate: Any, code: str) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(CapturePolicyRefused) as refused:
        parse_policy(_raw(document), SUPPLIER)
    assert refused.value.code == code


def test_a_policy_that_is_not_json_is_refused() -> None:
    for raw in (b"", b"{", b"[]", "￾".encode("utf-16")):
        with pytest.raises(CapturePolicyRefused):
            parse_policy(raw, SUPPLIER)


def test_a_supplier_without_a_reviewed_policy_has_no_extension_transport(tmp_path: Path) -> None:
    source = CapturePolicySource(tmp_path)
    for key in ("kmretail", "../kmretail", "KMRETAIL", "", "a/b", "kmretail/../x"):
        with pytest.raises(CapturePolicyRefused) as refused:
            source.load(key)
        assert refused.value.code == "CAPTURE_POLICY_ABSENT", key


def test_the_policy_is_read_again_on_every_load(tmp_path: Path) -> None:
    # Never cached: a policy revised after one load is what the next load answers.
    package = tmp_path / SUPPLIER
    package.mkdir()
    document = _document()
    (package / POLICY_FILE).write_bytes(_raw(document))
    source = CapturePolicySource(tmp_path)
    first = source.load(SUPPLIER)
    document["revision"] = "kmretail-capture-next"
    (package / POLICY_FILE).write_bytes(_raw(document))
    second = source.load(SUPPLIER)
    assert (first.revision, second.revision) == (KM_REVISION, "kmretail-capture-next")
    assert first.digest != second.digest


def test_the_extension_holds_no_copy_of_a_policy() -> None:
    # E1 specification §4 (H-2): one owner. The extension receives the policy for each capture and
    # carries no policy file, selector list or region list of its own.
    extension = REPO_ROOT / "ui" / "extension"
    document = _document()
    tokens = {document["product_root"]["token"]} | {
        region["token"] for region in (*document["allowed_regions"], *document["excluded_regions"])
    }
    files = [path for path in extension.rglob("*") if path.is_file()]
    assert files
    assert not [path for path in files if path.name == POLICY_FILE]
    for path in files:
        text = path.read_text("utf-8")
        assert "icbm-browser-capture-policy" not in text, path
        assert not [token for token in tokens if token in text], path
        for rule in document["head_allowance"]:
            if ":" in rule["value"]:  # the meta properties; "canonical" is an ordinary word
                assert rule["value"] not in text, path
