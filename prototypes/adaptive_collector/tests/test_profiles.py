"""Proof 1 — strict EPR/PTR parsing and immutable content-digest semantics (ADR-0017 §3, §5.2)."""

import json

import pytest
from pydantic import ValidationError

from prototypes.adaptive_collector.engine import ENGINE_REVISION
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.profile import (
    BundleRefused,
    ProfileStore,
    canonical,
    extraction_semantics_id,
    profile_digest,
    semantic_tuple,
)


def test_an_unknown_key_is_refused_at_save(store: ProfileStore) -> None:
    template = profiles.ptr("simple", optioned=False)
    template["hosts"] = ["cdn.example"]
    with pytest.raises(ValidationError):
        store.put(template)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["fields"].__setitem__("secret_price", t["fields"]["prices"]),
        lambda t: t["fields"]["prices"].__setitem__("status", "CONFIRMED"),
        lambda t: t["fields"]["prices"]["primary"].__setitem__("value", "12000"),
        lambda t: t["fields"]["prices"]["primary"].__setitem__("kind", "SCRIPT"),
        lambda t: t["signature"].__setitem__("required", ["div:has(.x)"]),
        lambda t: t["signature"].__setitem__("required", [" ".join(["div"] * 9)]),
    ],
    ids=["new-field", "a-status", "a-source-value", "an-expression", "pseudo-class", "unbounded"],
)
def test_a_profile_never_states_facts_statuses_new_fields_or_expressions(
    store: ProfileStore, mutate: object
) -> None:
    template = profiles.ptr("simple", optioned=False)
    mutate(template)  # type: ignore[operator]
    with pytest.raises((ValidationError, ValueError)):
        store.put(template)


def test_the_digest_is_content_only_and_order_independent(store: ProfileStore) -> None:
    template = profiles.ptr("simple", optioned=False)
    reordered = json.loads(canonical(template))  # keys sorted differently from the source dict
    assert store.put(template) == store.put(reordered)
    edited = profiles.ptr("simple", optioned=False)
    edited["fields"]["brand"]["primary"]["vocabulary"] = "origin_labels"
    assert store.put(edited) != store.put(template)


def test_the_store_is_append_only_and_refuses_tampered_rows(store: ProfileStore) -> None:
    assert not any(hasattr(store, name) for name in ("update", "delete", "replace", "remove"))
    digest = store.put(profiles.ptr("simple", optioned=False))
    before = dict(store._rows)
    store.put(profiles.ptr("simple", optioned=False))  # the same content again changes nothing
    assert store._rows == before
    tampered = json.loads(store._rows[digest])
    tampered["template_key"] = "tampered"
    store._rows[digest] = canonical(tampered)
    with pytest.raises(BundleRefused):
        store.get(digest)


def test_an_epr_pins_its_templates_by_digest_and_the_bundle_digest_covers_them(
    store: ProfileStore,
) -> None:
    simple = store.put(profiles.ptr("simple", optioned=False))
    epr = store.put(profiles.epr([simple]))
    assert store.bundle(epr).templates.keys() == {simple}
    with pytest.raises(BundleRefused):
        store.bundle(store.put(profiles.epr(["0" * 64])))
    # A template edit is a new PTR digest, so the EPR that pins it is a new EPR digest.
    edited = profiles.ptr("simple", optioned=False)
    edited["signature"]["forbidden"] = ["div.options select", "div.banner"]
    assert store.put(profiles.epr([store.put(edited)])) != epr


def test_a_template_is_never_shared_across_suppliers(store: ProfileStore) -> None:
    foreign = store.put(profiles.ptr("simple", optioned=False, supplier="othershop"))
    with pytest.raises(BundleRefused):
        store.bundle(store.put(profiles.epr([foreign])))


def test_the_semantic_tuple_has_semantic_parts_only() -> None:
    semantics = semantic_tuple(ENGINE_REVISION, "a" * 64)
    assert semantics == ("ADAPTIVE", ENGINE_REVISION, "icbm-profile/v1", "a" * 64)
    assert extraction_semantics_id(semantics) == extraction_semantics_id(semantics)
    assert extraction_semantics_id(semantics) != extraction_semantics_id(
        semantic_tuple(ENGINE_REVISION, "b" * 64)
    )
    # Length prefixing: moving a boundary between parts changes the stored digest.
    assert extraction_semantics_id(("ADAPTIVE", "ab", "c", "d")) != extraction_semantics_id(
        ("ADAPTIVE", "a", "bc", "d")
    )


def test_profile_digest_matches_the_store(store: ProfileStore) -> None:
    digest = store.put(profiles.ptr("simple", optioned=False))
    assert profile_digest(store.get(digest)) == digest
