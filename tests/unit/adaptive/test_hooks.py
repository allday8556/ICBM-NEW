"""The closed hook model, candidates decided by the engine, and promotion keys (ADR-0017 §6)."""

import json

import pytest

from app.collect.adaptive.document import read_html
from app.collect.adaptive.engine import extract, fact_value
from app.collect.adaptive.extraction_identity import EXTRACTOR_REVISION
from app.collect.adaptive.hooks import (
    BINDING_CAP,
    CANNOT_PARSE,
    HookManifest,
    exceeds_binding_cap,
    promotion_group,
    promotion_key,
    shared_promotion_keys,
)
from app.collect.adaptive.profiles import (
    BundleRefused,
    ExtractionProfileRevision,
    semantic_tuple,
)
from app.collect.facts import FieldStatus
from tests.adaptive_support import epr, hook_manifest, hooked_bundle, page


def _binding(point: str, target: str, fmt: str, name: str = "h") -> dict[str, str]:
    return {
        "hook_point": point,
        "target": target,
        "format_class": fmt,
        "hook_name": name,
        "hook_revision": "r1",
    }


def _epr(bindings: list[dict[str, str]], supplier: str = "synmart") -> ExtractionProfileRevision:
    return ExtractionProfileRevision.model_validate_json(
        json.dumps(epr(["a" * 64], supplier=supplier, hooks=bindings))
    )


def test_hooks_propose_and_the_engine_decides() -> None:
    got = extract(hooked_bundle(), read_html(page("hooked")), hook_manifest())
    assert got.source_product_id == "SH-0007"
    assert got.fields["shipping"].status is FieldStatus.CONFIRMED
    assert fact_value(got.fields["shipping"]) == {
        "kind": "CONDITIONAL",
        "policy_text": "2만원 이상 무료 / 미만 2,500원",
        "fee_krw": 2500,
        "free_over_krw": 20000,
    }
    assert got.hook_calls == {"identity_decode:identity": 1, "value_parse:shipping": 1}


def test_cannot_parse_an_invalid_candidate_and_a_smuggled_status_are_all_review() -> None:
    manifest = hook_manifest()
    unreadable = page("hooked").replace("2만원 이상 무료 / 미만 2,500원", "조건부")
    got = extract(hooked_bundle(), read_html(unreadable), manifest)
    assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED
    for candidate in (
        {"kind": "FIXED"},
        {"kind": "UNKNOWN", "policy_text": "x", "status": "CONFIRMED"},
        {"kind": "FIXED", "policy_text": "x", "fee_krw": float("nan")},
    ):
        rogue = HookManifest(
            manifest.supplier_key,
            manifest.hook_revision,
            manifest.hook_fingerprint,
            {**manifest.hooks, "conditional_shipping": lambda _text, c=candidate: c},
        )
        got = extract(hooked_bundle(), read_html(page("hooked")), rogue)
        assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED, candidate
        assert got.fields["shipping"].value is None


def test_an_identity_hook_that_cannot_parse_leaves_no_identity() -> None:
    manifest = hook_manifest()
    rogue = HookManifest(
        manifest.supplier_key,
        manifest.hook_revision,
        manifest.hook_fingerprint,
        {**manifest.hooks, "decode_code": lambda _text: CANNOT_PARSE},
    )
    got = extract(hooked_bundle(), read_html(page("hooked")), rogue)
    assert got.source_product_id is None and got.identity_reason == "IDENTITY_UNREADABLE"


def test_the_bound_hook_revision_must_be_the_running_one() -> None:
    with pytest.raises(BundleRefused, match="HOOK_REVISION"):
        extract(hooked_bundle("synhook2-hooks-0"), read_html(page("hooked")), hook_manifest())
    with pytest.raises(BundleRefused, match="manifest"):
        extract(hooked_bundle(), read_html(page("hooked")), None)


def test_a_fingerprint_only_change_keeps_semantics_a_revision_change_does_not() -> None:
    same = hooked_bundle()
    assert semantic_tuple(EXTRACTOR_REVISION, same.epr_digest) == semantic_tuple(
        EXTRACTOR_REVISION, hooked_bundle().epr_digest
    )
    # A new implementation fingerprint does not touch the EPR, so it cannot touch the tuple.
    extract(same, read_html(page("hooked")), hook_manifest(fingerprint="b" * 64))
    assert hooked_bundle("synhook2-hooks-2").epr_digest != same.epr_digest


def test_the_promotion_key_counts_bindings_not_hook_points() -> None:
    two = _epr(
        [
            _binding("value_parse", "prices", "MONEY_TEXT", "a"),
            _binding("value_parse", "shipping", "CONDITIONAL_POLICY_TEXT", "b"),
        ]
    )
    assert {promotion_key(b) for b in two.hooks} == {
        ("value_parse", "prices"),
        ("value_parse", "shipping"),
    }
    assert BINDING_CAP == 2 and not exceeds_binding_cap(two)
    three = _epr(
        [
            *[b.model_dump(mode="json") for b in two.hooks],
            _binding("value_parse", "brand", "LABELLED_TEXT", "c"),
        ]
    )
    assert exceeds_binding_cap(three)


def test_g5_flags_a_key_shared_across_suppliers_whatever_the_format_class() -> None:
    one = _epr([_binding("value_parse", "shipping", "CONDITIONAL_POLICY_TEXT")], "synmart")
    two = _epr([_binding("value_parse", "shipping", "LABELLED_TEXT")], "othershop")
    assert promotion_group(one.hooks[0]) != promotion_group(two.hooks[0])
    assert shared_promotion_keys([one, two]) == {
        ("value_parse", "shipping"): frozenset({"synmart", "othershop"})
    }
