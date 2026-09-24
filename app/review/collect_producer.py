"""The COLLECT / M3 review producer (Gate 2 G2-B, ADR-0016 §6).

It derives the review conditions COLLECT already states, and only those, from each source
identity's **current** ProductFactsRevision: the newest one a RECORDED run names, the same rule the
M4 materializer follows. It reads through the COLLECT store and writes nothing.

**The reviewed mapping** (one owner condition, one kind; nothing indexed twice):

- the ``stock`` field → STOCK, ``field:stock``, ``SOURCE_STOCK_REVIEW_REQUIRED``;
- any other field except ``images`` → COLLECT_EVIDENCE, ``field:<key>``,
  ``SOURCE_FIELD_REVIEW_REQUIRED``;
- an image reference left UNRESOLVED → COLLECT_EVIDENCE, ``image:<ROLE>:<ordinal>``,
  ``SOURCE_IMAGE_<issue>``;
- the representative, detail or excluded-share guard → COLLECT_EVIDENCE, ``images:<guard>``,
  ``SOURCE_IMAGE_<GUARD>``;
- ``images`` under review for evidence none of the above names → COLLECT_EVIDENCE,
  ``field:images``, ``SOURCE_FIELD_REVIEW_REQUIRED`` (fail closed).

- The ``images`` field is itself a projection of its references and guards, so it is indexed
  through its own REVIEW_REQUIRED evidence and never a second time as a whole field. An EXCLUDED
  reference is a decided exclusion (its evidence is ABSENT), so it is not review work.
- M4's ``SOURCE_CORE_FIELD_REVIEW_REQUIRED`` projects these same field statuses and is not indexed
  again (§6).

**Source identity** (ADR-0016 §12, the G2-B design point): the exact ``revision_id``. A new
collection is a new revision, so an unchanged condition moves to a new identity and supersedes the
old item (§4); the old item keeps its revision and its resolution history.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.collect.facts import (
    EXCLUDED_SHARE_LOCATOR,
    IMAGES_FIELD,
    MISSING_DETAIL_LOCATOR,
    MISSING_REPRESENTATIVE_LOCATOR,
    EvidenceKind,
    FieldStatus,
)
from app.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.review.model import ReviewCondition, ReviewKind

COLLECT_PRODUCER: Final = "collect.facts"
STOCK_FIELD: Final = "stock"

SOURCE_STOCK_REVIEW_REQUIRED: Final = "SOURCE_STOCK_REVIEW_REQUIRED"
SOURCE_FIELD_REVIEW_REQUIRED: Final = "SOURCE_FIELD_REVIEW_REQUIRED"
SOURCE_IMAGE_UNRESOLVED: Final = "SOURCE_IMAGE_UNRESOLVED"
_GUARDS: Final = {
    MISSING_REPRESENTATIVE_LOCATOR: (
        "images:representative",
        "SOURCE_IMAGE_REPRESENTATIVE_MISSING",
    ),
    MISSING_DETAIL_LOCATOR: ("images:detail", "SOURCE_IMAGE_DETAIL_MISSING"),
    EXCLUDED_SHARE_LOCATOR: ("images:excluded", "SOURCE_IMAGE_EXCLUDED_SHARE"),
}


class CollectReviewProducer:
    """Reads COLLECT source truth; writes nothing."""

    def __init__(self, revisions: ProductFactsRevisionStore) -> None:
        self._revisions = revisions

    @property
    def name(self) -> str:
        return COLLECT_PRODUCER

    def scopes(self) -> Sequence[Mapping[str, str]]:
        return tuple(
            {"supplier_key": supplier, "source_product_id": product}
            for supplier, product in self._revisions.recorded_sources()
        )

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        conditions: list[ReviewCondition] = []
        for supplier, product in self._sources(scope):
            revision = self._revisions.current_recorded(supplier, product)
            if revision is not None:
                conditions.extend(conditions_of(revision))
        return tuple(conditions)

    def _sources(self, scope: Mapping[str, str]) -> Sequence[tuple[str, str]]:
        supplier, product = scope.get("supplier_key"), scope.get("source_product_id")
        if supplier is not None and product is not None:
            return ((supplier, product),)
        return tuple(
            (s, p)
            for s, p in self._revisions.recorded_sources()
            if (supplier is None or s == supplier) and (product is None or p == product)
        )


def conditions_of(revision: StoredRevision) -> tuple[ReviewCondition, ...]:
    """The review conditions one revision states, by the mapping above."""
    found: list[tuple[ReviewKind, str, str]] = []
    for key, stored in revision.fields.items():
        if stored.status is not FieldStatus.REVIEW_REQUIRED:
            continue
        if key == STOCK_FIELD:
            found.append((ReviewKind.STOCK, "field:stock", SOURCE_STOCK_REVIEW_REQUIRED))
        elif key == IMAGES_FIELD:
            found.extend(_image_conditions(revision))
        else:
            found.append(
                (ReviewKind.COLLECT_EVIDENCE, f"field:{key}", SOURCE_FIELD_REVIEW_REQUIRED)
            )
    scope = {"supplier_key": revision.supplier_key, "source_product_id": revision.source_product_id}
    return tuple(
        ReviewCondition(
            kind=kind,
            producer=COLLECT_PRODUCER,
            scope=scope,
            subject=subject,
            reason_code=reason,
            source_identity=revision.revision_id,
        )
        for kind, subject, reason in dict.fromkeys(found)
    )


def _image_conditions(revision: StoredRevision) -> list[tuple[ReviewKind, str, str]]:
    issues = {(ref.role.value, ref.ordinal): ref.issue for ref in revision.images}
    found: list[tuple[ReviewKind, str, str]] = []
    unnamed = False
    for stored in revision.fields[IMAGES_FIELD].evidence:
        evidence = stored.evidence
        if evidence.status is not FieldStatus.REVIEW_REQUIRED:
            continue
        guard = _GUARDS.get(evidence.locator)
        if guard is not None:
            found.append((ReviewKind.COLLECT_EVIDENCE, *guard))
            continue
        role, _, ordinal = (evidence.normalized or "").partition(":")
        if evidence.kind is EvidenceKind.IMAGE and ordinal.isdigit():
            issue = issues.get((role, int(ordinal)))
            reason = SOURCE_IMAGE_UNRESOLVED if issue is None else f"SOURCE_IMAGE_{issue.value}"
            found.append((ReviewKind.COLLECT_EVIDENCE, f"image:{role}:{ordinal}", reason))
            continue
        unnamed = True
    if unnamed or not found:
        # Fail closed: the field is under review for a reason no row above names, so the field
        # itself is indexed rather than nothing.
        found.append((ReviewKind.COLLECT_EVIDENCE, "field:images", SOURCE_FIELD_REVIEW_REQUIRED))
    return found
