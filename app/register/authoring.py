"""M5 PR-F: the operator-authored registration preparation (ADR-0014 §27, decision `5751540323`).

A preflight is **derived** from the operator's own authored inputs and current owner truth (§3).
Until now those inputs had no application-owned durable source: they survived only inside a queued
`register.create` job's payload, which is execution state. This owner is that source, and it owns
**inputs only**:

- what it stores: the Draft and Draft revision the inputs were authored against, the exact Item
  membership of the provider-listing unit, the category selection, the listing values and the
  detail composition, each revision append-only with its own sanitized fingerprint;
- what it never stores: a readiness, a status or a reason code, a price, an image or QA verdict, a
  capability or auth fact, a provider duplicate outcome, a marketplace asset identity, or any
  Snapshot, Intent, Attempt or Registration truth. Those stay with the owners that hold them, and
  every verdict here is asked of them at the moment it is needed.

It hands work to the owners that already exist: the preflight service evaluates, the Snapshot
builder freezes, and the registration store writes. The job payload stays a frozen **execution**
copy of a preparation revision, never the authoring truth, and nothing here needs a job to exist.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import NotFoundError
from app.register.builder import RegistrationSnapshotBuilder
from app.register.contracts import AuthoredInputsView, FieldValueView
from app.register.execution import (
    decode_category,
    decode_detail,
    decode_field,
    encode_category,
    encode_detail,
    encode_field,
)
from app.register.model import sanitized_digest
from app.register.policy import Provenance
from app.register.preflight import RegistrationPreflightService
from app.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    DuplicateEvidence,
    FieldValue,
    ListingValues,
    PreflightRequest,
    PreflightResult,
    PreparedAsset,
    UnitRequest,
)
from app.register.sanitize import require_clean
from app.register.store import (
    IntentRecord,
    PreparationInputs,
    PreparationRecord,
    PreparationRevisionRecord,
    RegistrationStore,
    SnapshotRecord,
)

logger = logging.getLogger("icbm.register.authoring")

# The durable shape of one authored revision. A revision of another version is refused rather than
# guessed at, exactly as the send request's codec refuses one.
PREPARATION_VERSION = "registration-preparation/v1"


@dataclass(frozen=True)
class AuthoredInputs:
    """What an operator authored for one provider-listing unit. Typed, and nothing derived."""

    category: CategorySelection | None
    listing: ListingValues
    detail: DetailComposition | None


@dataclass(frozen=True)
class FrozenUnit:
    """What one accepted freeze produced: the immutable Snapshot and the Intent it opened."""

    snapshot: SnapshotRecord
    intent: IntentRecord
    preparation_revision_id: str


def encode_inputs(inputs: AuthoredInputs) -> PreparationInputs:
    """The sanitized canonical form of one authored revision, with its digest (§15).

    The same typed boundary the send request uses: a business value carrying secret or URL-shaped
    material is refused **before** anything durable is written or hashed.
    """
    listing = inputs.listing
    document: dict[str, Any] = {
        "version": PREPARATION_VERSION,
        "category": encode_category(inputs.category),
        "listing": {
            "name": None if listing.name is None else encode_field(listing.name),
            "tags": sorted(listing.tags),
            "attributes": {k: encode_field(v) for k, v in sorted(listing.attributes.items())},
            "notices": {k: encode_field(v) for k, v in sorted(listing.notices.items())},
            "options": {
                item: dict(sorted(values.items()))
                for item, values in sorted(listing.options.items())
            },
        },
        "detail": encode_detail(inputs.detail),
    }
    require_clean(document, "preparation")
    return PreparationInputs(
        category=document["category"],
        listing=document["listing"],
        detail=document["detail"],
        fingerprint=sanitized_digest(document),
    )


def inputs_from_view(view: AuthoredInputsView) -> AuthoredInputs:
    """The typed inputs an operator submitted. A plain read of the request, with no default value
    invented: an absent optional value stays absent (§4)."""
    return AuthoredInputs(
        category=(
            None
            if view.category is None
            else CategorySelection(
                category_id=view.category.category_id,
                mapping_revision=view.category.mapping_revision,
                taxonomy_revision=view.category.taxonomy_revision,
                confirmation=CategoryConfirmation(view.category.confirmation),
            )
        ),
        listing=ListingValues(
            name=None if view.name is None else _field(view.name),
            tags=frozenset(view.tags),
            attributes={key: _field(value) for key, value in view.attributes.items()},
            notices={key: _field(value) for key, value in view.notices.items()},
            options={item: dict(values) for item, values in view.options.items()},
        ),
        detail=(
            None
            if view.detail_composition_revision is None or view.detail_body is None
            else DetailComposition(
                composition_revision=view.detail_composition_revision,
                body=view.detail_body,
                sections=tuple(view.detail_sections) or ("BODY",),
            )
        ),
    )


def _field(view: FieldValueView) -> FieldValue:
    return FieldValue(
        value=view.value,
        provenance=Provenance(view.provenance),
        detail_page_reference=view.detail_page_reference,
    )


def decode_inputs(revision: PreparationRevisionRecord) -> AuthoredInputs:
    """The typed inputs one recorded revision holds. It reconstructs nothing from a payload."""
    listing = revision.listing
    return AuthoredInputs(
        category=decode_category(revision.category),
        listing=ListingValues(
            name=None if listing["name"] is None else decode_field(listing["name"]),
            tags=frozenset(listing["tags"]),
            attributes={k: decode_field(v) for k, v in listing["attributes"].items()},
            notices={k: decode_field(v) for k, v in listing["notices"].items()},
            options={item: dict(values) for item, values in listing["options"].items()},
        ),
        detail=decode_detail(revision.detail),
    )


def preflight_request(
    preparation: PreparationRecord,
    revision: PreparationRevisionRecord,
    *,
    draft_revision: int | None = None,
    duplicate_evidence: DuplicateEvidence | None = None,
) -> PreflightRequest:
    """The request one revision states, for the owner that evaluates it.

    ``draft_revision`` is the revision the evaluation expects to find now: the current Draft when
    an operator is working, and the authored one when a caller is checking what was authored. The
    preflight compares it with the Draft itself, which is how a moved Draft becomes `STALE`.
    """
    inputs = decode_inputs(revision)
    return PreflightRequest(
        unit=UnitRequest(
            draft_id=preparation.draft_id,
            expected_draft_revision=(
                revision.draft_revision if draft_revision is None else draft_revision
            ),
            item_ids=revision.item_ids,
        ),
        category=inputs.category,
        listing=inputs.listing,
        detail=inputs.detail,
        duplicate_evidence=duplicate_evidence,
    )


class RegistrationPreparationService:
    """Create, revise, read and evaluate a preparation, and freeze the unit it prepares."""

    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        preflight: RegistrationPreflightService,
        builder: RegistrationSnapshotBuilder,
    ) -> None:
        self._registrations = registrations
        self._preflight = preflight
        self._builder = builder

    # ------------------------------------------------------------------ authoring

    def create(
        self,
        draft_id: str,
        *,
        item_ids: Sequence[str],
        inputs: AuthoredInputs,
        actor: str,
        correlation_id: str | None = None,
    ) -> PreparationRecord:
        encoded = encode_inputs(inputs)
        with self._registrations.transaction() as unit:
            return unit.create_preparation(
                draft_id,
                item_ids=item_ids,
                inputs=encoded,
                created_by=actor,
                correlation_id=self._correlation(correlation_id),
            )

    def update(
        self,
        preparation_id: str,
        *,
        item_ids: Sequence[str],
        inputs: AuthoredInputs,
        actor: str,
        correlation_id: str | None = None,
    ) -> PreparationRecord:
        """Append the next revision. An earlier one, and any Snapshot it froze, stay as they are."""
        encoded = encode_inputs(inputs)
        with self._registrations.transaction() as unit:
            return unit.revise_preparation(
                preparation_id,
                item_ids=item_ids,
                inputs=encoded,
                authored_by=actor,
                correlation_id=self._correlation(correlation_id),
            )

    def preparation(self, preparation_id: str) -> PreparationRecord:
        found = self._registrations.preparation(preparation_id)
        if found is None:
            raise NotFoundError("REGISTER_PREPARATION_NOT_FOUND", "the preparation does not exist")
        return found

    def of_draft(self, draft_id: str) -> tuple[PreparationRecord, ...]:
        return self._registrations.preparations_of_draft(draft_id)

    # ------------------------------------------------------------------ evaluation (§3)

    def evaluate(
        self,
        preparation_id: str,
        *,
        duplicate_evidence: DuplicateEvidence | None = None,
    ) -> PreflightResult:
        """The **candidate** evaluation of what is authored now, against current owner truth.

        No job is needed, and nothing is stored: the verdict, its status and every reason code are
        the preflight owner's, recomputed here each time it is asked for (§3).
        """
        preparation = self.preparation(preparation_id)
        request = preflight_request(
            preparation,
            preparation.current,
            draft_revision=self._draft_revision(preparation.draft_id),
            duplicate_evidence=duplicate_evidence,
        )
        return self._preflight.candidate(request)

    def freeze(
        self,
        preparation_id: str,
        *,
        actor: str,
        duplicate_evidence: DuplicateEvidence | None = None,
        prepared_assets: Sequence[PreparedAsset] = (),
        correlation_id: str | None = None,
    ) -> FrozenUnit:
        """Freeze the unit this preparation authored, and open its CREATE Intent.

        Every rule stays with its owner: the final preflight must be `READY` under current truth,
        the builder re-evaluates it and refuses a drifted one (`REGISTER_PREFLIGHT_STALE`), and the
        store's own invariants and triggers guard the write. This owner adds the provenance — which
        exact authored revision produced the Snapshot — in that same unit of work.
        """
        preparation = self.preparation(preparation_id)
        revision = preparation.current
        correlation = self._correlation(correlation_id)
        request = preflight_request(
            preparation,
            revision,
            draft_revision=self._draft_revision(preparation.draft_id),
            duplicate_evidence=duplicate_evidence,
        )
        final = self._preflight.final(request, prepared_assets)
        snapshot = self._builder.freeze(
            final,
            created_by=actor,
            correlation_id=correlation,
            preparation_revision_id=revision.preparation_revision_id,
        )
        with self._registrations.transaction() as unit:
            existing = unit.intent_of_snapshot(snapshot.registration_snapshot_id)
            if existing is not None:
                return FrozenUnit(snapshot, existing, revision.preparation_revision_id)
            batch = unit.create_batch(
                snapshot.marketplace_key,
                snapshot.marketplace_account_id,
                created_by=actor,
                correlation_id=correlation,
            )
            intent = unit.create_intent(
                batch,
                snapshot.registration_snapshot_id,
                created_by=actor,
                correlation_id=correlation,
            )
        return FrozenUnit(snapshot, intent, revision.preparation_revision_id)

    # ------------------------------------------------------------------ helpers

    def _draft_revision(self, draft_id: str) -> int:
        draft = self._registrations.draft(draft_id)
        if draft is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "the draft does not exist")
        return draft.draft_revision

    @staticmethod
    def _correlation(correlation_id: str | None) -> str:
        return correlation_id or get_correlation_id() or new_correlation_id()
