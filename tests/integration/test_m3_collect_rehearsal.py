"""The offline rehearsal of the production COLLECT path, run by CI (M3 Stage-B2).

The operator can run ``python scripts/m3_collect.py`` and read the same report. This keeps the
rehearsal honest: it fails here the moment the production path stops behaving as it describes.
"""

import pytest

from app.collect.models import CollectionOutcome
from scripts.m3collect.runner import rehearse

pytestmark = pytest.mark.integration


def test_the_offline_rehearsal_walks_the_whole_production_path() -> None:
    steps = {step.name: step for step in rehearse()}
    assert list(steps) == [
        "first collection",
        "same product too soon",
        "revalidated reuse",
        "changed content",
        "unresolved identity",
    ]

    refused = steps["same product too soon"]
    assert refused.detail == "COLLECT_SAME_PRODUCT_TOO_SOON"
    assert refused.revision_id is None and refused.image_requests == 0

    first = steps["first collection"]
    assert first.outcome is CollectionOutcome.RECORDED
    assert first.facts_status == "CONFIRMED"
    assert first.image_requests == 2, "two product references, and never the layout asset"

    reused = steps["revalidated reuse"]
    assert reused.outcome is CollectionOutcome.RECORDED
    assert reused.revision_id != first.revision_id, "a reuse still appends its own revision"
    assert reused.assets[0] == first.assets[0], "the confirmed bytes are the ones already stored"

    changed = steps["changed content"]
    assert changed.assets[0] != first.assets[0], "other bytes are another asset"
    assert changed.assets[1] == first.assets[1], "the unchanged reference keeps its own"

    unresolved = steps["unresolved identity"]
    assert unresolved.outcome is CollectionOutcome.NO_REVISION
    assert unresolved.revision_id is None and unresolved.facts_status is None
    assert unresolved.image_requests == 0, "no identity, nothing fetched"
