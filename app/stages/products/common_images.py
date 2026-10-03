"""Supplier common images: the operator's BLOCK / KEEP decisions and their detection (Issue #219).

A supplier common image is a file that the supplier repeats across products' detail pages — a
shipping notice, a seller warning, a contact card, a blank spacer or a brand banner — that may not
belong on a listing of one product. It is keyed by its supplier and its SHA-256: the same bytes
from the same supplier are the same image wherever they appear. Nothing here reads an image's
content; the only inputs are file fingerprints, the collection history and the operator's
decisions (Issue #219 §6: no AI, no OCR).

**Verdict** of one key, in order:

1. the operator's newest decision, ``BLOCK`` or ``KEEP``, which is remembered;
2. without one, the owner's own decision of Issue #219 §1 (:data:`OWNER_SEED`);
3. without either, ``REVIEW`` when the file is a detection candidate — it appears in the detail
   images of at least :data:`DETECTION_MIN_PRODUCTS` different products of that supplier over the
   supplier's whole collection history. ``REVIEW`` is treated as blocked until an operator decides,
   so a supplier's contact details or sales warnings never reach a listing by default;
4. otherwise no verdict: the file is the product's own.

A synthetic test product (``icbm-synthetic``) copies a collected revision unchanged, so it reads
its template supplier's verdicts and never counts towards detection.

This owner decides nothing about a selection. The image auto-selection reads its verdicts.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.collect.facts import FieldStatus, ImageRole
from app.stages.collect.models import (
    SYNTHETIC_SUPPLIER_KEY,
    ProductFactsImageRef,
    ProductFactsRevision,
    SyntheticTestProduct,
)
from app.stages.products.image_models import SupplierCommonImageDecision

# A file in the detail images of this many different products of one supplier is a candidate.
DETECTION_MIN_PRODUCTS: Final = 3
DETECTION_RULE_VERSION: Final = "supplier-common-image-detection/v1"
_SHA = frozenset("0123456789abcdef")


class CommonImageVerdict(StrEnum):
    """The verdict on one supplier common image. Only BLOCK and KEEP are ever decided; REVIEW is
    a detected candidate no operator has decided yet, and it is treated as blocked."""

    BLOCK = "BLOCK"
    KEEP = "KEEP"
    REVIEW = "REVIEW"


DECIDABLE: Final = frozenset({CommonImageVerdict.BLOCK, CommonImageVerdict.KEEP})

# The owner's decisions of Issue #219 §1 (2026-10-03) on KM Retail's common images: six files
# blocked and two brand banners kept, because consignment selling sometimes needs the brand banner
# for trust. They are decisions, not detections; an operator decision recorded later supersedes
# them.
OWNER_SEED_ACTOR: Final = "owner-decision-2026-10-03"
OWNER_SEED_REFERENCE: Final = "github-issue-219"
_B, _K = CommonImageVerdict.BLOCK, CommonImageVerdict.KEEP
OWNER_SEED: Final[Mapping[tuple[str, str], tuple[CommonImageVerdict, str]]] = MappingProxyType(
    {
        ("kmretail", sha256): (verdict, reason)
        for sha256, verdict, reason in (
            (
                "0cf0a06abcfc2782d4c25ee89d47fb95745fc9c011200022329787b4b4cf2f62",
                _B,
                "KM October shipping schedule notice",
            ),
            (
                "dfcff93a994916519e7e33b5d440dac7b03e1272ae07ec33ec6178a47bf7684d",
                _B,
                "head-office price-monitoring warning",
            ),
            (
                "c1bd24fc00ed56d225c68ab464ec7aba22014e44ac221f173e5b477e84c37a05",
                _B,
                "Abottle customer-service, business-number and returns card",
            ),
            (
                "71b69eb9e1f13b46086aeb01663c41e1e589b7aa554a684aa7c63e9cb8262e36",
                _B,
                "Origin Korea customer-service and business-number card",
            ),
            (
                "b2b2920ee888079e0321b19506a27bcb83d0fc5c46f3035beeb00b0aaa26732f",
                _B,
                "blank spacer",
            ),
            (
                "d30b1f96bf8150deed73d6dd813012aa9f05b4e2998d19c6ab44c90af8934f53",
                _B,
                "blank spacer",
            ),
            (
                "351fc658988fca9a89bdafcd5c6cf87a8ec106fd642f0d38ab36c667804e6959",
                _K,
                "Origin health brand banner",
            ),
            (
                "e251a58accb130763f07a5d57d3047b71308c158b541caca24428cd226641d22",
                _K,
                "Abottle brand banner",
            ),
        )
    }
)
# What a selection may not use: a blocked file, and a candidate no operator has decided.
EXCLUDED: Final = frozenset({CommonImageVerdict.BLOCK, CommonImageVerdict.REVIEW})


@dataclass(frozen=True)
class CommonImageDecision:
    """One recorded decision."""

    decision_id: str
    supplier_key: str
    sha256: str
    revision_no: int
    verdict: CommonImageVerdict
    reason: str | None
    decided_by: str
    correlation_id: str


@dataclass(frozen=True)
class CommonImage:
    """One supplier common image as the operator reviews it: its current verdict, whether an
    operator decided it, and how many different products of the supplier show it."""

    supplier_key: str
    sha256: str
    verdict: CommonImageVerdict
    decided: bool
    product_count: int
    decision: CommonImageDecision | None


def _decision(row: SupplierCommonImageDecision) -> CommonImageDecision:
    return CommonImageDecision(
        decision_id=row.decision_id,
        supplier_key=row.supplier_key,
        sha256=row.sha256,
        revision_no=row.revision_no,
        verdict=CommonImageVerdict(row.verdict),
        reason=row.reason,
        decided_by=row.decided_by,
        correlation_id=row.correlation_id,
    )


def _refusal(code: str, message: str) -> InputValidationError:
    return InputValidationError(code, message)


def effective_supplier(session: Session, revision: ProductFactsRevision) -> str:
    """The supplier whose images a revision shows: its own, or for a synthetic test product the
    collected supplier of the revision it copies."""
    if revision.supplier_key != SYNTHETIC_SUPPLIER_KEY:
        return revision.supplier_key
    label = session.scalars(
        select(SyntheticTestProduct).where(
            SyntheticTestProduct.supplier_key == revision.supplier_key,
            SyntheticTestProduct.source_product_id == revision.source_product_id,
        )
    ).one_or_none()
    return revision.supplier_key if label is None else label.template_supplier_key


def _current_decisions(
    session: Session, supplier_key: str, shas: Iterable[str] | None = None
) -> dict[str, CommonImageDecision]:
    query = select(SupplierCommonImageDecision).where(
        SupplierCommonImageDecision.supplier_key == supplier_key
    )
    if shas is not None:
        query = query.where(SupplierCommonImageDecision.sha256.in_(list(shas)))
    wanted = None if shas is None else set(shas)
    current: dict[str, CommonImageDecision] = {
        sha: CommonImageDecision(
            decision_id=f"{OWNER_SEED_REFERENCE}:{sha[:12]}",
            supplier_key=supplier,
            sha256=sha,
            revision_no=0,
            verdict=verdict,
            reason=reason,
            decided_by=OWNER_SEED_ACTOR,
            correlation_id=OWNER_SEED_REFERENCE,
        )
        for (supplier, sha), (verdict, reason) in OWNER_SEED.items()
        if supplier == supplier_key and (wanted is None or sha in wanted)
    }
    for row in session.scalars(query.order_by(SupplierCommonImageDecision.revision_no)):
        current[row.sha256] = _decision(row)
    return current


def _product_counts(
    session: Session, supplier_key: str, shas: Iterable[str] | None = None
) -> dict[str, int]:
    """How many different products of the supplier show each file among their CONFIRMED detail
    images, over the whole collection history."""
    query = (
        select(
            ProductFactsImageRef.sha256,
            func.count(func.distinct(ProductFactsRevision.source_product_id)),
        )
        .join(
            ProductFactsRevision,
            ProductFactsRevision.revision_id == ProductFactsImageRef.revision_id,
        )
        .where(
            ProductFactsRevision.supplier_key == supplier_key,
            ProductFactsImageRef.role == ImageRole.DETAIL.value,
            ProductFactsImageRef.status == FieldStatus.CONFIRMED.value,
            ProductFactsImageRef.sha256.is_not(None),
        )
        .group_by(ProductFactsImageRef.sha256)
    )
    if shas is not None:
        query = query.where(ProductFactsImageRef.sha256.in_(list(shas)))
    return {str(sha): int(count) for sha, count in session.execute(query)}


def verdicts(
    session: Session, supplier_key: str, shas: Iterable[str]
) -> Mapping[str, CommonImageVerdict]:
    """The verdict of each named file of one supplier, read in the caller's session. A file with
    no verdict is absent: it is the product's own."""
    wanted = sorted(set(shas))
    decided = _current_decisions(session, supplier_key, wanted)
    counts = _product_counts(session, supplier_key, [s for s in wanted if s not in decided])
    found: dict[str, CommonImageVerdict] = {
        sha: decision.verdict for sha, decision in decided.items()
    }
    for sha, count in counts.items():
        if sha not in found and count >= DETECTION_MIN_PRODUCTS:
            found[sha] = CommonImageVerdict.REVIEW
    return found


class SupplierCommonImageService:
    """The operator's supplier common images: what is detected and decided, and a new decision."""

    def __init__(self, db: Database, audit: AuditLog, clock: Clock) -> None:
        self._db = db
        self._audit = audit
        self._clock = clock

    @staticmethod
    def _known_supplier(session: Session, supplier_key: str) -> None:
        if supplier_key == SYNTHETIC_SUPPLIER_KEY:
            raise _refusal(
                "PRODUCTS_COMMON_IMAGE_SUPPLIER_SYNTHETIC",
                "a synthetic test product reads its template supplier's common images",
            )
        seen = session.scalar(
            select(ProductFactsRevision.revision_id)
            .where(ProductFactsRevision.supplier_key == supplier_key)
            .limit(1)
        )
        if seen is None:
            raise NotFoundError(
                "PRODUCTS_COMMON_IMAGE_SUPPLIER_UNKNOWN", "no collection of that supplier exists"
            )

    def images(self, supplier_key: str) -> tuple[CommonImage, ...]:
        """Every decided file and every detection candidate of one supplier, the most widely
        repeated first."""
        with self._db.read() as session:
            self._known_supplier(session, supplier_key)
            decided = _current_decisions(session, supplier_key)
            counts = _product_counts(session, supplier_key)
        keys = set(decided) | {s for s, n in counts.items() if n >= DETECTION_MIN_PRODUCTS}
        found = []
        for sha in keys:
            decision = decided.get(sha)
            found.append(
                CommonImage(
                    supplier_key=supplier_key,
                    sha256=sha,
                    verdict=CommonImageVerdict.REVIEW if decision is None else decision.verdict,
                    decided=decision is not None,
                    product_count=counts.get(sha, 0),
                    decision=decision,
                )
            )
        return tuple(sorted(found, key=lambda image: (-image.product_count, image.sha256)))

    def decide(
        self,
        supplier_key: str,
        sha256: str,
        verdict: CommonImageVerdict,
        *,
        decided_by: str,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> CommonImageDecision:
        """Record the operator's BLOCK or KEEP for one file of one supplier the supplier has
        actually shown. The same verdict again is the same decision; a new one appends."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        if verdict not in DECIDABLE:
            raise _refusal(
                "PRODUCTS_COMMON_IMAGE_VERDICT_INVALID", "an operator decides BLOCK or KEEP"
            )
        if len(sha256) != 64 or not set(sha256) <= _SHA:
            raise _refusal("PRODUCTS_COMMON_IMAGE_SHA_INVALID", "a SHA-256 is 64 hex characters")
        if not decided_by.strip() or len(decided_by) > 64:
            raise _refusal("PRODUCTS_COMMON_IMAGE_ACTOR", "a decision names its operator")
        if reason is not None and (not reason.strip() or len(reason) > 200):
            raise _refusal("PRODUCTS_COMMON_IMAGE_REASON", "a reason is 1 to 200 characters")
        with self._db.write() as session:
            self._known_supplier(session, supplier_key)
            if not _product_counts(session, supplier_key, [sha256]):
                raise NotFoundError(
                    "PRODUCTS_COMMON_IMAGE_UNSEEN",
                    "the supplier has shown no CONFIRMED detail image with that SHA-256",
                )
            current = _current_decisions(session, supplier_key, [sha256]).get(sha256)
            if current is not None and current.verdict is verdict:
                return current
            row = SupplierCommonImageDecision(
                decision_id=str(uuid.uuid4()),
                supplier_key=supplier_key,
                sha256=sha256,
                revision_no=1 if current is None else current.revision_no + 1,
                verdict=verdict.value,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation,
                created_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            recorded = _decision(row)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.SUPPLIER_COMMON_IMAGE_DECIDED,
                    action="supplier_common_image.decide",
                    actor=decided_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=recorded.decision_id,
                    reason_code=recorded.verdict.value,
                    before=None if current is None else {"verdict": current.verdict.value},
                    after={"verdict": recorded.verdict.value},
                    details={
                        "supplier_key": supplier_key,
                        "sha256": sha256,
                        "revision_no": recorded.revision_no,
                    },
                    correlation_id=correlation,
                ),
                session=session,
            )
        return recorded
