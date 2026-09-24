"""The M4 / PRODUCT DB review producer (Gate 2 G2-C, ADR-0016 §6).

It indexes, by reference, the ``REVIEW_REQUIRED`` reasons M4 **base** readiness already derives
for each Item of an ACTIVE canonical Product (``ProductReadinessService.base_readiness``). It
re-implements no readiness rule and writes nothing.

**The reviewed mapping** (one owner condition, one kind; nothing indexed twice):

- COLLECT_EVIDENCE, the evidence a Product's Item is built on:
  - ``SOURCE_CORE_FIELD_ABSENT`` → ``field:<key>``;
  - ``IMAGE_SELECTION_MISSING``, ``IMAGE_SELECTION_EMPTY`` → ``images``;
  - ``IMAGE_QA_MISSING``, ``IMAGE_QA_REVIEW_REQUIRED`` → ``image:<ROLE>:<pos>``.
- SOURCE_CHANGE, which source products stand behind the Item:
  - ``MEMBERSHIP_REVISION_MISSING``, ``MEMBERSHIP_REVISION_NOT_CURRENT`` → ``membership``;
  - ``GROUP_MEMBER_CANDIDATE_PENDING`` → ``membership:candidates``;
  - ``BINDING_MISSING``, ``BINDING_COMPOSITION_INVALID``, ``BINDING_OFFER_INVALID``,
    ``BINDING_MEMBER_NOT_CONFIRMED`` → ``binding``.

SOURCE_CHANGE is indexed here, but the kind stays ``NOT_WIRED`` while source drift
(``docs/ARCHITECTURE.md`` §11), which can also emit it, has no production owner
(``app.review.counts``).

Not indexed, by design:

- ``SOURCE_CORE_FIELD_REVIEW_REQUIRED`` projects a COLLECT field status that the COLLECT producer
  already indexes (§6).
- ``IMAGE_FINDING_*`` details the ``IMAGE_QA_REVIEW_REQUIRED`` verdict of the same image, which is
  indexed once.
- ``STALE`` and ``BLOCKED`` reasons are not review work: they are the owner's own verdicts, and a
  human resolution could not move them (§5). Pricing readiness is per context and is not indexed.
- The Items of a RETIRED group: it is history, not a product (ADR-0013 §4).

A ``REVIEW_REQUIRED`` reason that is neither mapped nor excluded above **fails the derivation**
(``REVIEW_CONDITION_UNMAPPED``), so a new M4 reason can never be silently left out of a count.

**Source identity**: the base readiness ``dependency_fingerprint`` the reason was derived under. A
changed fingerprint supersedes the old item and opens a new one (G2-07).
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.core.errors import AppError, ErrorClass
from app.products.images import (
    IMAGE_FINDING_PREFIX,
    IMAGE_QA_MISSING,
    IMAGE_QA_REVIEW_REQUIRED,
    IMAGE_SELECTION_EMPTY,
    IMAGE_SELECTION_MISSING,
)
from app.products.model import ReadinessStatus
from app.products.pricing_service import (
    BINDING_COMPOSITION_INVALID,
    BINDING_MEMBER_NOT_CONFIRMED,
    BINDING_MISSING,
    BINDING_OFFER_INVALID,
    MEMBERSHIP_REVISION_MISSING,
    MEMBERSHIP_REVISION_NOT_CURRENT,
)
from app.products.readiness import (
    GROUP_CANDIDATE_PENDING,
    SOURCE_CORE_FIELD_ABSENT,
    SOURCE_CORE_FIELD_REVIEW_REQUIRED,
    ProductReadinessService,
)
from app.review.model import ReviewCondition, ReviewKind

PRODUCTS_PRODUCER: Final = "products.readiness"
REVIEW_CONDITION_UNMAPPED: Final = "REVIEW_CONDITION_UNMAPPED"

# The reviewed table: an M4 REVIEW_REQUIRED reason code → its kind and how its subject is named.
_FIELD, _IMAGES, _IMAGE, _MEMBERSHIP, _CANDIDATES, _BINDING = (
    "field",
    "images",
    "image",
    "membership",
    "candidates",
    "binding",
)
MAPPING: Final[Mapping[str, tuple[ReviewKind, str]]] = {
    SOURCE_CORE_FIELD_ABSENT: (ReviewKind.COLLECT_EVIDENCE, _FIELD),
    IMAGE_SELECTION_MISSING: (ReviewKind.COLLECT_EVIDENCE, _IMAGES),
    IMAGE_SELECTION_EMPTY: (ReviewKind.COLLECT_EVIDENCE, _IMAGES),
    IMAGE_QA_MISSING: (ReviewKind.COLLECT_EVIDENCE, _IMAGE),
    IMAGE_QA_REVIEW_REQUIRED: (ReviewKind.COLLECT_EVIDENCE, _IMAGE),
    MEMBERSHIP_REVISION_MISSING: (ReviewKind.SOURCE_CHANGE, _MEMBERSHIP),
    MEMBERSHIP_REVISION_NOT_CURRENT: (ReviewKind.SOURCE_CHANGE, _MEMBERSHIP),
    GROUP_CANDIDATE_PENDING: (ReviewKind.SOURCE_CHANGE, _CANDIDATES),
    BINDING_MISSING: (ReviewKind.SOURCE_CHANGE, _BINDING),
    BINDING_COMPOSITION_INVALID: (ReviewKind.SOURCE_CHANGE, _BINDING),
    BINDING_OFFER_INVALID: (ReviewKind.SOURCE_CHANGE, _BINDING),
    BINDING_MEMBER_NOT_CONFIRMED: (ReviewKind.SOURCE_CHANGE, _BINDING),
}
# Excluded on purpose: a projection of COLLECT's own condition (§6).
EXCLUDED: Final = frozenset({SOURCE_CORE_FIELD_REVIEW_REQUIRED})


class ReviewConditionUnmappedError(AppError):
    """An owner reason with no reviewed kind: the derivation fails rather than drop it."""

    error_class = ErrorClass.FATAL


def _subject(style: str, code: str, subject: str | None) -> str:
    if style == _FIELD:
        return f"field:{subject or 'revision'}"
    if style == _IMAGE and subject:
        return f"image:{subject}"
    if style == _CANDIDATES:
        return "membership:candidates"
    if style in (_IMAGES, _MEMBERSHIP, _BINDING):
        return style
    raise ReviewConditionUnmappedError(
        REVIEW_CONDITION_UNMAPPED,
        "an M4 reason has no reviewed subject",
        details={"reason_code": code},
    )


class ProductsReviewProducer:
    """Reads M4 base readiness; writes nothing."""

    def __init__(self, readiness: ProductReadinessService) -> None:
        self._readiness = readiness

    @property
    def name(self) -> str:
        return PRODUCTS_PRODUCER

    def scopes(self) -> Sequence[Mapping[str, str]]:
        return tuple(
            {"product_group_id": group, "item_id": item}
            for group, item in self._readiness.base_items()
        )

    def truth_token(self) -> str:
        return self._readiness.base_truth_token()

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        group, item = scope.get("product_group_id"), scope.get("item_id")
        conditions: list[ReviewCondition] = []
        for group_id, item_id in self._readiness.base_items():
            if (group is None or group == group_id) and (item is None or item == item_id):
                conditions.extend(self._conditions(group_id, item_id))
        return tuple(conditions)

    def _conditions(self, group_id: str, item_id: str) -> list[ReviewCondition]:
        readiness = self._readiness.base_readiness(item_id)
        found: dict[tuple[ReviewKind, str, str], None] = {}
        for reason in readiness.reasons:
            if reason.status is not ReadinessStatus.REVIEW_REQUIRED:
                continue
            if reason.code in EXCLUDED or reason.code.startswith(IMAGE_FINDING_PREFIX):
                continue
            mapped = MAPPING.get(reason.code)
            if mapped is None:
                raise ReviewConditionUnmappedError(
                    REVIEW_CONDITION_UNMAPPED,
                    "an M4 REVIEW_REQUIRED reason has no reviewed kind",
                    details={"reason_code": reason.code},
                )
            kind, style = mapped
            found[(kind, _subject(style, reason.code, reason.subject), reason.code)] = None
        scope = {"product_group_id": group_id, "item_id": item_id}
        return [
            ReviewCondition(
                kind=kind,
                producer=PRODUCTS_PRODUCER,
                scope=scope,
                subject=subject,
                reason_code=code,
                source_identity=readiness.dependency_fingerprint,
            )
            for kind, subject, code in found
        ]
