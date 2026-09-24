"""The REGISTER preparation review producer (Gate 2 G2-C, ADR-0016 §6; review 5810256789 B2).

For every durable preparation that is still work to author, it asks the existing preflight owner
for the **candidate** evaluation, exactly as Registration Management does
(``RegistrationPreparationService.evaluate``). It indexes the ``REVIEW_REQUIRED`` reasons of that
evaluation by reference. It copies no rule and stores no verdict, because none exists
(ADR-0014 M5-03).

**Scope and identity (§8).**
- The scope is ``marketplace_key``, ``marketplace_account_id``, ``draft_id`` and ``preparation_id``.
- The source identity is the preparation's current durable revision
  (``preparation_revision_id``), the anchor ADR-0016 §6 names for a derived-only condition.
- Which preparations are current follows the rule Registration Management uses: a preparation of
  an Item set the Draft's current revision has not frozen
  (``RegistrationStore.current_preparations``).

**The reviewed mapping.** Every indexed reason is REGISTRATION_ERROR.
- Indexed:
  - every REGISTER reason in ``REVIEWED`` that the candidate states as REVIEW_REQUIRED. This
    includes REGISTER's own evaluation of publication assets against the target's asset policy;
  - M4 **pricing** readiness under this target's pricing context (``M4_PRICING.<PRICING_*>``),
    which no other producer indexes (M4 pricing readiness is per context);
  - a candidate the preflight owner refuses to evaluate, as that owner's typed refusal code on
    subject ``preflight``. An example is an account with no target policy. Registration
    Management shows the same refusal.
- Not indexed: true projections, which are an owner's reason re-emitted verbatim with a prefix.
  - ``M4_BASE.*`` is ``products.readiness``'s condition, or through it COLLECT's.
  - ``M4_PRICING.<procurement>`` is the same current-procurement condition that M4 base readiness
    derives and ``products.readiness`` already indexes.
- STALE, BLOCKED and DUPLICATE reasons are the owner's own verdicts, not review work.
- A REVIEW_REQUIRED code that is neither indexed nor excluded above **fails the derivation**
  (``REVIEW_CONDITION_UNMAPPED``), so a new preflight reason can never be silently left out of a
  count.

**The fence.** The truth token must move whenever any evaluation could. It is built from these
parts:
- every owner write the preflight can read, counted through the append-only audit log. That
  covers REGISTER, target policy, category metadata, M4 pricing, M4 images and CONNECT;
- M4 base readiness's own token, which covers membership that is not audited;
- the CONNECT capability each marketplace reads now;
- the current preparation revisions.

Reading capability converges an attestation the clock expired. That write is CONNECT's, done
outside the review unit. Inside the unit, any write a derivation attempts is refused, and the
coordinator rolls the unit back even when a caller swallowed the refusal (``Database.write``).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

from app.audit.service import AuditLog
from app.core.errors import AppError, ErrorClass
from app.db.database import DatabaseWriteReentryError
from app.products.model import ReadinessStatus
from app.products.pricing import (
    MINIMUM_SALE_PRICE_NOT_OFFER_BOUND,
    MINIMUM_SALE_PRICE_NOT_POSITIVE,
    MINIMUM_SALE_PRICE_UNRESOLVED,
    PURCHASE_PRICE_AMBIGUOUS,
    PURCHASE_PRICE_UNRESOLVED,
    QUANTITY_OFFER_UNRESOLVED,
    SHIPPING_CONDITIONAL,
    SHIPPING_UNKNOWN,
    SHIPPING_UNRESOLVED,
    TARGET_MARGIN_UNREACHABLE,
)
from app.products.pricing_service import (
    BINDING_COMPOSITION_INVALID,
    BINDING_MEMBER_NOT_CONFIRMED,
    BINDING_MISSING,
    BINDING_OFFER_INVALID,
    MEMBERSHIP_REVISION_MISSING,
    MEMBERSHIP_REVISION_NOT_CURRENT,
)
from app.products.readiness import GROUP_CANDIDATE_PENDING, ProductReadinessService
from app.register import preparation as p
from app.register.authoring import RegistrationPreparationService
from app.register.preflight import RegistrationPreflightService
from app.register.store import PreparationRecord, RegistrationStore
from app.review.model import ReviewCondition, ReviewKind, is_identifier
from app.review.products_producer import REVIEW_CONDITION_UNMAPPED, ReviewConditionUnmappedError

PREFLIGHT_PRODUCER: Final = "register.preflight"
PREFLIGHT_SUBJECT: Final = "preflight"

# The reviewed table: every REGISTER reason the candidate can state as REVIEW_REQUIRED.
REVIEWED: Final = frozenset(
    {
        p.ACCOUNT_BINDING_MISMATCH,
        p.CONNECT_AUTH_MISMATCH,
        p.CONNECT_AUTH_NOT_READY,
        p.CONNECT_WORKFLOW_REVIEW,
        p.CATEGORY_NOT_SELECTED,
        p.CATEGORY_NOT_CONFIRMED,
        p.CATEGORY_METADATA_MISSING,
        p.CATEGORY_METADATA_UNREVIEWED,
        p.LISTING_NAME_MISSING,
        p.LISTING_NAME_TOO_LONG,
        p.ATTRIBUTE_REQUIRED_MISSING,
        p.NOTICE_REQUIRED_MISSING,
        p.NOTICE_POLICY_MISSING,
        p.FIELD_UNDECLARED,
        p.FIELD_VALUE_EMPTY,
        p.FIELD_VALUE_TOO_LONG,
        p.FIELD_AI_SUGGESTION_UNCONFIRMED,
        p.FIELD_DETAIL_REFERENCE_NOT_PERMITTED,
        p.OPTION_VALUE_MISSING,
        p.OPTION_VALUES_UNEXPECTED,
        p.OPTION_DIMENSIONS_INCONSISTENT,
        p.OPTION_VALUES_NOT_DISTINCT,
        p.POLICY_TEMPLATE_MISSING,
        p.DETAIL_COMPOSITION_MISSING,
        p.DETAIL_BODY_EMPTY,
        p.AUTHORING_REVISIONS_UNOWNED,
        p.PUBLICATION_ASSETS_MISSING,
        p.PUBLICATION_ASSET_COUNT_EXCEEDED,
        p.PUBLICATION_REPRESENTATIVE_MISSING,
        p.PUBLICATION_ASSET_QA_NOT_PASSED,
        p.PROVIDER_DUPLICATE_FOUND,
        p.PROVIDER_DUPLICATE_WEAK_SIGNAL,
        p.DUPLICATE_EVIDENCE_MISSING,
        p.DUPLICATE_EVIDENCE_INCONCLUSIVE,
        p.DUPLICATE_EVIDENCE_INCOMPLETE,
        p.DUPLICATE_EVIDENCE_SCOPE_MISMATCH,
        # Final-stage reasons: the candidate never states them, and they are reviewed all the same.
        p.PREPARED_ASSET_UNEXPECTED,
        p.PREPARED_ASSET_DUPLICATED,
        p.PROVIDER_ASSET_IDENTITY_MISSING,
    }
)
# M4 pricing readiness under this target's context: indexed here, and nowhere else.
M4_PRICING_REVIEWED: Final = frozenset(
    {
        PURCHASE_PRICE_UNRESOLVED,
        PURCHASE_PRICE_AMBIGUOUS,
        SHIPPING_UNRESOLVED,
        SHIPPING_CONDITIONAL,
        SHIPPING_UNKNOWN,
        MINIMUM_SALE_PRICE_UNRESOLVED,
        MINIMUM_SALE_PRICE_NOT_POSITIVE,
        MINIMUM_SALE_PRICE_NOT_OFFER_BOUND,
        QUANTITY_OFFER_UNRESOLVED,
        TARGET_MARGIN_UNREACHABLE,
        # The fallback an M4 layer states when it is not READY but gives no reason.
        ReadinessStatus.REVIEW_REQUIRED.value,
    }
)
# The current-procurement condition M4 base readiness derives and products.readiness indexes:
# pricing readiness re-emits it verbatim, so it is a projection here.
M4_PROCUREMENT: Final = frozenset(
    {
        MEMBERSHIP_REVISION_MISSING,
        MEMBERSHIP_REVISION_NOT_CURRENT,
        BINDING_MISSING,
        BINDING_COMPOSITION_INVALID,
        BINDING_OFFER_INVALID,
        BINDING_MEMBER_NOT_CONFIRMED,
        GROUP_CANDIDATE_PENDING,
    }
)
# A refusal the owner may give instead of an evaluation: a typed cause, never a crash.
_REFUSAL_CLASSES: Final = frozenset(
    {
        ErrorClass.NOT_FOUND,
        ErrorClass.VALIDATION,
        ErrorClass.POLICY_BLOCKED,
        ErrorClass.CONFLICT,
        ErrorClass.REVIEW_REQUIRED,
    }
)


def indexed(code: str) -> bool:
    """Whether a REVIEW_REQUIRED candidate reason is indexed here (True), is a projection another
    producer already indexes (False), or has no reviewed mapping (it raises)."""
    if code.startswith(p.M4_BASE_PREFIX):
        return code == f"{p.M4_BASE_PREFIX}{ReadinessStatus.REVIEW_REQUIRED.value}"
    if code.startswith(p.M4_PRICING_PREFIX):
        rest = code[len(p.M4_PRICING_PREFIX) :]
        if rest in M4_PROCUREMENT:
            return False
        if rest in M4_PRICING_REVIEWED:
            return True
    elif code in REVIEWED:
        return True
    raise ReviewConditionUnmappedError(
        REVIEW_CONDITION_UNMAPPED,
        "a REGISTER REVIEW_REQUIRED reason has no reviewed kind",
        details={"reason_code": code},
    )


def bounded_subject(subject: str | None) -> str:
    """The reason's subject as a bounded identifier. One that carries free text (an attribute
    key, say) keeps its head and a digest of the rest, deterministically, and never the text."""
    if subject is None:
        return PREFLIGHT_SUBJECT
    if is_identifier(subject):
        return subject
    head = subject.split(":", 1)[0]
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
    return f"{head if is_identifier(head) and len(head) <= 32 else 'subject'}:h{digest}"


def _scope(record: PreparationRecord) -> dict[str, str]:
    return {
        "marketplace_key": record.marketplace_key,
        "marketplace_account_id": record.marketplace_account_id,
        "draft_id": record.draft_id,
        "preparation_id": record.preparation_id,
    }


class PreflightReviewProducer:
    """Reads REGISTER's durable preparations through the preflight owner; writes nothing."""

    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        preparations: RegistrationPreparationService,
        preflight: RegistrationPreflightService,
        readiness: ProductReadinessService,
        audit: AuditLog,
    ) -> None:
        self._registrations = registrations
        self._preparations = preparations
        self._preflight = preflight
        self._readiness = readiness
        self._audit = audit

    @property
    def name(self) -> str:
        return PREFLIGHT_PRODUCER

    def scopes(self) -> Sequence[Mapping[str, str]]:
        return tuple(_scope(record) for record in self._registrations.current_preparations())

    def truth_token(self) -> str:
        current = self._registrations.current_preparations()
        state = {
            "owner_writes": self._audit.owner_writes(),
            "m4_base": self._readiness.base_truth_token(),
            "capability": {
                key: self._preflight.capability_reading(key)
                for key in sorted({record.marketplace_key for record in current})
            },
            "preparations": [
                [record.preparation_id, record.current.preparation_revision_id]
                for record in current
            ],
        }
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        conditions: list[ReviewCondition] = []
        for record in self._registrations.current_preparations():
            where = _scope(record)
            if all(where.get(key) == value for key, value in scope.items()):
                conditions.extend(self._conditions(record, where))
        return tuple(conditions)

    def _conditions(
        self, record: PreparationRecord, where: Mapping[str, str]
    ) -> list[ReviewCondition]:
        identity = record.current.preparation_revision_id
        try:
            result = self._preparations.evaluate(record.preparation_id)
        except DatabaseWriteReentryError:
            raise
        except AppError as refused:
            if refused.error_class not in _REFUSAL_CLASSES:
                raise
            return [self._condition(where, PREFLIGHT_SUBJECT, refused.code, identity)]
        found: dict[tuple[str, str], None] = {}
        for reason in result.reasons:
            if reason.status is ReadinessStatus.REVIEW_REQUIRED and indexed(reason.code):
                found[(bounded_subject(reason.subject), reason.code)] = None
        return [self._condition(where, subject, code, identity) for subject, code in found]

    @staticmethod
    def _condition(
        where: Mapping[str, str], subject: str, code: str, identity: str
    ) -> ReviewCondition:
        return ReviewCondition(
            kind=ReviewKind.REGISTRATION_ERROR,
            producer=PREFLIGHT_PRODUCER,
            scope=where,
            subject=subject,
            reason_code=code,
            source_identity=identity,
        )
