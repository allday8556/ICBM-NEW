"""Strict EPR/PTR value models and content-digest semantics (ADR-0017 §3)."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.collect.adaptive.profiles import (
    BundleRefused,
    ExtractionProfileRevision,
    PageTemplateRevision,
    profile_digest,
    profile_document,
    resolve_bundle,
)
from tests.adaptive_support import bundle_of, documents, epr, synmart_bundle, template

Mutation = Callable[[dict[str, Any]], None]


def _parse(document: dict[str, Any]) -> PageTemplateRevision:
    return PageTemplateRevision.model_validate_json(json.dumps(document))


MUTATIONS: dict[str, Mutation] = {
    "host": lambda t: t.__setitem__("hosts", ["cdn.example"]),
    "limit": lambda t: t.__setitem__("request_limit", 3),
    "new-field": lambda t: t["fields"].__setitem__("secret_price", t["fields"]["prices"]),
    "a-status": lambda t: t["fields"]["prices"].__setitem__("status", "CONFIRMED"),
    "a-value": lambda t: t["fields"]["prices"]["primary"].__setitem__("value", "19800"),
    "an-expression": lambda t: t["fields"]["prices"]["primary"].__setitem__("kind", "SCRIPT"),
    "pseudo-class": lambda t: t["signature"].__setitem__("required", ["div:has(.x)"]),
    "sibling": lambda t: t["signature"].__setitem__("required", ["a ~ b"]),
    "nine-steps": lambda t: t["signature"].__setitem__("required", [" ".join(["div"] * 9)]),
    "too-long": lambda t: t["signature"].__setitem__("required", ["x" * 201]),
    "quoted": lambda t: t.__setitem__("stock_scope", "div[onclick='x()']"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_template_never_states_facts_statuses_egress_or_expressions(name: str) -> None:
    document = template("plain", choice=False)
    MUTATIONS[name](document)
    with pytest.raises(ValidationError):
        _parse(document)


def test_the_models_are_frozen() -> None:
    model = _parse(template("plain", choice=False))
    with pytest.raises(ValidationError):
        model.template_key = "renamed"


def test_the_digest_is_content_only_and_order_independent() -> None:
    document = template("plain", choice=False)
    reordered = json.loads(json.dumps(document, sort_keys=True))
    assert profile_digest(_parse(document)) == profile_digest(_parse(reordered))
    edited = template("plain", choice=False)
    edited["fields"]["brand"]["primary"]["vocabulary"] = "origin_labels"
    assert profile_digest(_parse(edited)) != profile_digest(_parse(document))


def test_an_epr_pins_its_templates_and_its_digest_covers_them() -> None:
    first = synmart_bundle()
    assert [t.template_key for _, t in first.templates] == ["plain", "choice"]
    edited = template("plain", choice=False)
    edited["signature"]["forbidden"] = ["div.choice select", "div.banner"]
    assert bundle_of([edited, template("choice", choice=True)]).epr_digest != first.epr_digest


def test_a_bundle_recomputes_every_digest() -> None:
    texts = documents(template("plain", choice=False))
    pinned = next(iter(texts))
    root = profile_document(
        ExtractionProfileRevision.model_validate_json(json.dumps(epr([pinned])))
    )
    assert resolve_bundle(root, texts).templates[0][0] == pinned
    tampered = json.loads(texts[pinned])
    tampered["template_key"] = "tampered"
    with pytest.raises(BundleRefused, match="recompute"):
        resolve_bundle(root, {pinned: json.dumps(tampered)})
    with pytest.raises(BundleRefused, match="not supplied"):
        resolve_bundle(root, {})


def test_a_template_is_never_shared_across_suppliers() -> None:
    with pytest.raises(BundleRefused, match="shared"):
        bundle_of([template("plain", choice=False, supplier="othershop")])


def test_an_unknown_vocabulary_refuses_the_bundle() -> None:
    document = template("plain", choice=False)
    document["fields"]["brand"]["primary"]["vocabulary"] = "no_such_labels"
    with pytest.raises(BundleRefused, match="vocabulary"):
        bundle_of([document])


@pytest.mark.parametrize(
    "binding",
    [
        {"hook_point": "document_preprocess", "target": "identity", "format_class": "PATH_CODE"},
        {"hook_point": "value_parse", "target": "not_a_field", "format_class": "MONEY_TEXT"},
        {"hook_point": "identity_decode", "target": "prices", "format_class": "PATH_CODE"},
        {"hook_point": "value_parse", "target": "prices", "format_class": "PATH_CODE"},
    ],
    ids=["unknown-point", "unknown-target", "wrong-target", "foreign-format-class"],
)
def test_hook_bindings_are_closed_at_parse(binding: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        synmart_bundle(hooks=[{**binding, "hook_name": "h", "hook_revision": "r1"}])


def test_one_binding_per_hook_point_and_target() -> None:
    twice = {
        "hook_point": "value_parse",
        "target": "brand",
        "format_class": "LABELLED_TEXT",
        "hook_revision": "r1",
    }
    with pytest.raises(ValidationError):
        synmart_bundle(hooks=[{**twice, "hook_name": "a"}, {**twice, "hook_name": "b"}])
