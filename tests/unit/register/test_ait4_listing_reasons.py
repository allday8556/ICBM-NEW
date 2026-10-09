"""ADR-0028 T4: the tag provenance joins the listing reasons without moving any other rule.

GPT audit of #278: the AI-tag check must sit beside the name rules, never above them, so a name
over the category's limit is refused whatever the tags hold.
"""

from dataclasses import replace

from app.stages.register.policy import Provenance
from app.stages.register.preparation import FieldValue
from tests.unit.register.test_m5_preflight_rules import LISTING, candidate, codes, request


def test_a_name_over_the_limit_is_refused_whatever_the_tags_hold() -> None:
    long_name = FieldValue("이" * 101)
    for tags, provenance in (
        (frozenset(), None),
        (frozenset({"operator-tag"}), None),
        (frozenset({"ai-tag"}), Provenance.AI_SUGGESTION),
    ):
        listing = replace(LISTING, name=long_name, tags=tags, tags_provenance=provenance)
        assert "LISTING_NAME_TOO_LONG" in codes(candidate(request(listing=listing)))


def test_an_ai_tag_set_is_unconfirmed_and_an_operator_set_is_not() -> None:
    ai = replace(LISTING, tags=frozenset({"ai-tag"}), tags_provenance=Provenance.AI_SUGGESTION)
    reasons = candidate(request(listing=ai)).reasons
    assert any(r.code == "FIELD_AI_SUGGESTION_UNCONFIRMED" and r.subject == "tags" for r in reasons)
    operator = replace(LISTING, tags=frozenset({"operator-tag"}), tags_provenance=None)
    reasons = candidate(request(listing=operator)).reasons
    assert not any(r.subject == "tags" for r in reasons)
