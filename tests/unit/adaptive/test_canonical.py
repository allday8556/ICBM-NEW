"""Canonical JSON, digests and the semantic tuple (ADR-0017 §3, §5.2): non-finite is refused."""

import json
import math

import pytest

from app.collect.adaptive.canonical import (
    NonFiniteValue,
    canonical_json,
    digest,
    length_prefixed,
    parse_json,
)
from app.collect.adaptive.document import read_html
from app.collect.adaptive.extraction_identity import EXTRACTOR_REVISION
from app.collect.adaptive.profiles import (
    ExtractionProfileRevision,
    extraction_semantics_id,
    profile_document,
    resolve_bundle,
    semantic_tuple,
)
from tests.adaptive_support import documents, epr, template


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "[1e999]", '{"a": -1e400}'])
def test_non_finite_json_is_refused_when_read(text: str) -> None:
    with pytest.raises(NonFiniteValue):
        parse_json(text)
    json.loads(text)  # the standard library alone would have accepted it


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {"price": math.nan}, [math.inf]])
def test_non_finite_values_are_refused_when_written(value: object) -> None:
    with pytest.raises(NonFiniteValue):
        canonical_json(value)
    with pytest.raises(NonFiniteValue):
        digest("any-scheme", value)


def test_canonical_json_is_key_order_independent_and_compact() -> None:
    assert canonical_json({"b": 1, "a": "가"}) == canonical_json({"a": "가", "b": 1})
    assert canonical_json({"b": 1, "a": "가"}) == '{"a":"가","b":1}'


def test_a_digest_is_domain_separated_by_its_scheme() -> None:
    assert digest("scheme-a", {"x": 1}) != digest("scheme-b", {"x": 1})
    assert digest("scheme-a", {"x": 1}) == digest("scheme-a", {"x": 1})


def test_length_prefixing_keeps_part_boundaries() -> None:
    assert length_prefixed(["ab", "c"]) != length_prefixed(["a", "bc"])
    assert length_prefixed(["가"]) == b"\x00\x00\x00\x03" + "가".encode()


def test_the_semantic_tuple_has_semantic_parts_only() -> None:
    semantics = semantic_tuple(EXTRACTOR_REVISION, "e" * 64)
    assert semantics == ("ADAPTIVE", EXTRACTOR_REVISION, "icbm-profile/v1", "e" * 64)
    assert extraction_semantics_id(semantics) != extraction_semantics_id(
        semantic_tuple(EXTRACTOR_REVISION, "f" * 64)
    )
    assert extraction_semantics_id(("ADAPTIVE", "ab", "c", "d")) != extraction_semantics_id(
        ("ADAPTIVE", "a", "bc", "d")
    )


def test_a_non_finite_embedded_literal_is_never_admissible() -> None:
    root = read_html(
        '<script type="application/ld+json">{"price": NaN}</script>'
        '<script>var d = {"price": 1e999};</script>'
    )
    assert [e.data for e in root.walk() if e.tag == "script"] == [None, None]


def test_a_profile_document_with_a_non_finite_number_is_refused() -> None:
    texts = documents(template("plain", choice=False))
    root = profile_document(
        ExtractionProfileRevision.model_validate_json(json.dumps(epr(list(texts))))
    )
    with pytest.raises(NonFiniteValue):
        resolve_bundle(root[:-1] + ',"x":NaN}', texts)
    pinned = next(iter(texts))
    with pytest.raises(NonFiniteValue):
        resolve_bundle(root, {pinned: texts[pinned][:-1] + ',"y":Infinity}'})
