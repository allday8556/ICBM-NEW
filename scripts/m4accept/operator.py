"""The one scripted operator of the acceptance harnesses (M4 ruling 5739459941 §F).

No system path may select an image on an operator's behalf. An acceptance run still needs an Item
whose images an operator has decided on, so exactly one place — this one — records that decision,
for every harness, and it records it through the image owner's **own operator entry point**. The
selection models and tables stay forbidden here like everywhere else, and nothing here decides
which images are chosen: the caller states the decision it is scripting.
"""

from collections.abc import Sequence

from app.products.image_model import SelectedOutput, SourceDecision
from app.products.image_store import SelectionMove, SelectionRecord
from app.products.images import ProductImageService


def scripted_selection(
    images: ProductImageService,
    item_id: str,
    *,
    source_revision_id: str,
    decisions: Sequence[SourceDecision],
    outputs: Sequence[SelectedOutput],
    decided_by: str,
) -> tuple[SelectionRecord, SelectionMove]:
    """Record one scripted operator's full image decision for an Item, through the image owner."""
    return images.record_operator_selection(
        item_id,
        source_revision_id=source_revision_id,
        decisions=decisions,
        outputs=outputs,
        decided_by=decided_by,
    )
