"""Proof 5 — the closed hook binding model and `(hook_point, target)` counting (ADR-0017 §6)."""

import pytest
from pydantic import ValidationError

from app.collect.facts import FieldStatus
from prototypes.adaptive_collector.dom import parse_html
from prototypes.adaptive_collector.engine import ENGINE_REVISION, extract, value_json
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.hooks import (
    CANNOT_PARSE,
    HookManifest,
    HookRefused,
    g6_exceeded,
    promotion_group,
    promotion_key,
    promotion_review,
)
from prototypes.adaptive_collector.profile import (
    ExtractionProfileRevision,
    ProfileStore,
    semantic_tuple,
)
from prototypes.adaptive_collector.tests.conftest import hook_manifest, hooked_bundle, page


def _binding(point: str, target: str, fmt: str, name: str = "h") -> dict[str, str]:
    return {
        "hook_point": point,
        "target": target,
        "format_class": fmt,
        "hook_name": name,
        "hook_revision": "r1",
    }


def _epr(bindings: list[dict[str, str]], supplier: str = "synthetic") -> ExtractionProfileRevision:
    return ExtractionProfileRevision.model_validate_json(
        __import__("json").dumps(profiles.epr(["a" * 64], supplier=supplier, hooks=bindings))
    )


def test_hook_points_are_closed() -> None:
    with pytest.raises(ValidationError):
        _epr([_binding("document_preprocess", "identity", "PATH_CODE")])


def test_a_binding_targets_only_what_its_hook_point_allows(store: ProfileStore) -> None:
    for bad in (
        _binding("value_parse", "not_a_field", "MONEY_TEXT"),
        _binding("identity_decode", "prices", "PATH_CODE"),
        _binding("value_parse", "prices", "PATH_CODE"),  # a format class of another hook point
    ):
        template = store.put(profiles.ptr("simple", optioned=False))
        bundle = store.bundle(store.put(profiles.epr([template], hooks=[bad])))
        with pytest.raises(HookRefused):
            extract(bundle, parse_html(page("simple_on_sale")), None)


def test_hooks_propose_candidates_and_the_engine_decides(store: ProfileStore) -> None:
    got = extract(hooked_bundle(store), parse_html(page("hooked")), hook_manifest())
    assert got.identity == "SYN-3001"
    assert got.fields["shipping"].status is FieldStatus.CONFIRMED
    assert value_json(got.fields["shipping"]) == {
        "kind": "CONDITIONAL",
        "policy_text": "5만원 이상 무료 / 미만 3,000원",
        "fee_krw": 3000,
        "free_over_krw": 50000,
    }
    assert got.hook_calls == {"identity_decode:identity": 1, "value_parse:shipping": 1}


def test_cannot_parse_and_an_invalid_candidate_are_review_required(store: ProfileStore) -> None:
    manifest = hook_manifest()
    html = page("hooked").replace("5만원 이상 무료 / 미만 3,000원", "조건부 배송")
    got = extract(hooked_bundle(store), parse_html(html), manifest)
    assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED
    bad = HookManifest(
        manifest.supplier_key,
        manifest.hook_revision,
        manifest.hook_fingerprint,
        {**manifest.hooks, "parse_conditional_shipping": lambda text: {"kind": "FIXED"}},
    )
    got = extract(hooked_bundle(store), parse_html(page("hooked")), bad)
    assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED
    assert got.fields["shipping"].value is None


def test_a_hook_cannot_set_a_status_or_evidence(store: ProfileStore) -> None:
    manifest = hook_manifest()
    sneaky = HookManifest(
        manifest.supplier_key,
        manifest.hook_revision,
        manifest.hook_fingerprint,
        {
            **manifest.hooks,
            "parse_conditional_shipping": lambda text: {
                "kind": "UNKNOWN",
                "policy_text": text,
                "status": "CONFIRMED",
            },
        },
    )
    got = extract(hooked_bundle(store), parse_html(page("hooked")), sneaky)
    # The value model refuses the extra key: the engine, not the hook, decides the status.
    assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED


def test_the_bound_hook_revision_must_be_the_running_one(store: ProfileStore) -> None:
    with pytest.raises(HookRefused):
        extract(
            hooked_bundle(store, "synhook-hooks-0"), parse_html(page("hooked")), hook_manifest()
        )
    with pytest.raises(HookRefused):
        extract(hooked_bundle(store), parse_html(page("hooked")), None)


def test_a_fingerprint_only_change_keeps_semantics_and_a_revision_change_is_a_new_epr(
    store: ProfileStore,
) -> None:
    first = hooked_bundle(store)
    same = hooked_bundle(store)
    # Same HOOK_REVISION, whatever the implementation fingerprint: same EPR, same semantics.
    assert first.epr_digest == same.epr_digest
    assert semantic_tuple(ENGINE_REVISION, first.epr_digest) == semantic_tuple(
        ENGINE_REVISION, same.epr_digest
    )
    advanced = hooked_bundle(store, "synhook-hooks-2")
    assert advanced.epr_digest != first.epr_digest


def test_promotion_key_counts_bindings_not_hook_points() -> None:
    two_value_parse = _epr(
        [
            _binding("value_parse", "prices", "MONEY_TEXT", "a"),
            _binding("value_parse", "shipping", "CONDITIONAL_POLICY_TEXT", "b"),
        ]
    )
    assert {promotion_key(b) for b in two_value_parse.hooks} == {
        ("value_parse", "prices"),
        ("value_parse", "shipping"),
    }
    assert not g6_exceeded(two_value_parse)  # two bindings: at the cap
    three = _epr(
        [
            _binding("value_parse", "prices", "MONEY_TEXT", "a"),
            _binding("value_parse", "shipping", "CONDITIONAL_POLICY_TEXT", "b"),
            _binding("value_parse", "brand", "LABELLED_TEXT", "c"),
        ]
    )
    assert g6_exceeded(three)  # three bindings of ONE hook point exceed the cap


def test_g5_flags_a_key_shared_by_two_suppliers_whatever_the_format_class() -> None:
    one = _epr([_binding("value_parse", "shipping", "CONDITIONAL_POLICY_TEXT")], "synthetic")
    two = _epr([_binding("value_parse", "shipping", "LABELLED_TEXT")], "othershop")
    assert promotion_group(one.hooks[0]) != promotion_group(two.hooks[0])
    assert promotion_review([one, two]) == {("value_parse", "shipping"): {"synthetic", "othershop"}}


def test_cannot_parse_is_a_sentinel_not_a_value() -> None:
    assert repr(CANNOT_PARSE) == "CANNOT_PARSE"
