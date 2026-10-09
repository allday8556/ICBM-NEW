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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from sqlalchemy.exc import IntegrityError

from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.products.image_model import ImageAssetKind
from app.stages.products.model import ReadinessStatus
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.contracts import AuthoredInputsView, FieldValueView
from app.stages.register.execution import (
    decode_category,
    decode_detail,
    decode_field,
    encode_category,
    encode_detail,
    encode_field,
)
from app.stages.register.model import (
    DUPLICATE_EVIDENCE_UNAVAILABLE,
    RegistrationConflictError,
    sanitized_digest,
)
from app.stages.register.payload import build_payload
from app.stages.register.policy import SATISFYING, Provenance
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.preparation import (
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
    resolve_unit,
)
from app.stages.register.provider import DuplicateLookupSource, PreparedAssetSource
from app.stages.register.sanitize import require_clean
from app.stages.register.store import (
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
# A submitted authoring revision that is not exactly the target policy's own (decision 5801915996).
AUTHORING_REVISION_NOT_OWNED = "REGISTER_AUTHORING_REVISION_NOT_OWNED"
# The policy needs provider asset identities but no prepared-asset owner is wired (5919917893 §3).
PREPARED_ASSETS_UNAVAILABLE = "REGISTER_PREPARED_ASSETS_UNAVAILABLE"
MAPPING_REVISION = "mapping_revision"
COMPOSITION_REVISION = "detail_composition_revision"
# B-DETAIL: a composition names a section its owned profile does not hold.
DETAIL_SECTIONS_NOT_PROFILE = "REGISTER_DETAIL_SECTIONS_NOT_PROFILE"


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


@dataclass(frozen=True)
class ExecutionCopy:
    """The first CREATE job input reconstructed from immutable authored provenance."""

    request: PreflightRequest
    final: PreflightResult


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
            # Only an AI set names its provenance, so every operator revision encodes as before.
            **(
                {"tags_provenance": listing.tags_provenance.value}
                if listing.tags and listing.tags_provenance is not None
                else {}
            ),
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
        # A body is authored content and is kept whether or not its composition revision has an
        # owner: an unowned revision stays None, exactly (decision 5800619183).
        detail=(
            None
            if view.detail_body is None
            else DetailComposition(
                composition_revision=view.detail_composition_revision,
                body=view.detail_body,
                sections=tuple(view.detail_sections) or ("BODY",),
            )
        ),
    )


def submitted_revisions(inputs: AuthoredInputs) -> dict[str, str | None]:
    """The server-owned authoring revisions these inputs would store, by name."""
    submitted: dict[str, str | None] = {}
    if inputs.category is not None:
        submitted[MAPPING_REVISION] = inputs.category.mapping_revision
    if inputs.detail is not None:
        submitted[COMPOSITION_REVISION] = inputs.detail.composition_revision
    return submitted


def submitted_view_revisions(view: AuthoredInputsView) -> dict[str, str | None]:
    """Every authoring revision a client sent, including a composition revision sent without a
    body, which would not be stored: a client never names a revision the owner does not hold."""
    submitted: dict[str, str | None] = {}
    if view.category is not None:
        submitted[MAPPING_REVISION] = view.category.mapping_revision
    if view.detail_body is not None or view.detail_composition_revision is not None:
        submitted[COMPOSITION_REVISION] = view.detail_composition_revision
    return submitted


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
            tags_provenance=(
                Provenance(listing["tags_provenance"]) if listing.get("tags_provenance") else None
            ),
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


class AppliedResult(Protocol):
    """What an apply reads of one enrichment result (PRODUCT DB's ``ResultView``)."""

    @property
    def status(self) -> str: ...
    @property
    def stale(self) -> bool: ...
    @property
    def stale_reasons(self) -> list[str]: ...
    @property
    def value(self) -> dict[str, Any] | None: ...
    @property
    def sequence(self) -> int: ...
    @property
    def input_fingerprint(self) -> str: ...


class EnrichmentResultSource(Protocol):
    """PRODUCT DB's enrichment owner, as an apply reads it. The register owner never imports the
    AI capability or the enrichment owner: the composition root hands it this read (ADR-0026
    AIF-02: an acceptance run that loads the register owners loads no AI module)."""

    def current_result(
        self,
        product_group_id: str,
        task_key: str,
        result_key: str,
        marketplace_key: str | None,
        marketplace_account_id: str | None,
    ) -> AppliedResult | None: ...


AI_APPLY_FIELD_INVALID: Final = "AI_APPLY_FIELD_INVALID"
AI_APPLY_FIELD_LOCKED: Final = "AI_APPLY_FIELD_LOCKED"
AI_APPLY_REVISION_CHANGED: Final = "AI_APPLY_REVISION_CHANGED"
AI_APPLY_RESULT_FOREIGN: Final = "AI_APPLY_RESULT_FOREIGN"
AI_APPLY_RESULT_UNUSABLE: Final = "AI_APPLY_RESULT_UNUSABLE"
AI_APPLY_RESULT_CHANGED: Final = "AI_APPLY_RESULT_CHANGED"
AI_APPLY_VALUE_INVALID: Final = "AI_APPLY_VALUE_INVALID"


@dataclass(frozen=True)
class EnrichmentApply:
    """One enrichment result applied to one Preparation field (ADR-0026 §7; AIF-4).

    ``field`` is ``name``, ``attribute.<key>`` or ``notice.<key>``: a field whose value carries a
    provenance. The result subject is named whole: its product, task and result key, and
    ``result_targeted`` (the result of this Preparation's own target, or the target-free one).
    ``result_sequence`` names the exact revision the operator saw, and ``value_field`` the scalar
    of its value that becomes the field's value.
    ``expected_revision_no`` is the Preparation revision the operator read."""

    field: str
    product_group_id: str
    task_key: str
    result_key: str
    result_targeted: bool
    result_sequence: int
    value_field: str
    expected_revision_no: int


TAGS_FIELD: Final = "tags"
CATEGORY_FIELD: Final = "category"
# ADR-0028 §4: a tag set holds at most 10 tags.
TAGS_MAX: Final = 10


def _with_tags(inputs: AuthoredInputs, texts: tuple[str, ...]) -> AuthoredInputs:
    listing = replace(
        inputs.listing, tags=frozenset(texts), tags_provenance=Provenance.AI_SUGGESTION
    )
    return replace(inputs, listing=listing)


def _tag_texts(value: object) -> tuple[str, ...] | None:
    """The texts of a tag result's ``recommended`` list: 1–10 distinct non-blank strings; a
    platform tag's code stays with the result, never in the Preparation (ADR-0028 §6)."""
    if not isinstance(value, list) or not 1 <= len(value) <= TAGS_MAX:
        return None
    texts: list[str] = []
    for item in value:
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text.strip() or text in texts:
            return None
        texts.append(text)
    return tuple(texts)


def _with_field(inputs: AuthoredInputs, field: str, value: FieldValue) -> AuthoredInputs:
    listing = inputs.listing
    if field == "name":
        return replace(inputs, listing=replace(listing, name=value))
    kind, _, key = field.partition(".")
    if kind == "attribute":
        return replace(
            inputs, listing=replace(listing, attributes={**listing.attributes, key: value})
        )
    return replace(inputs, listing=replace(listing, notices={**listing.notices, key: value}))


def _field_of(inputs: AuthoredInputs, field: str) -> FieldValue | None:
    listing = inputs.listing
    if field == "name":
        return listing.name
    kind, _, key = field.partition(".")
    return (listing.attributes if kind == "attribute" else listing.notices).get(key)


def _valid_field(field: str) -> bool:
    if field in ("name", TAGS_FIELD, CATEGORY_FIELD):
        return True
    kind, dot, key = field.partition(".")
    return kind in ("attribute", "notice") and dot == "." and bool(key.strip())


class RegistrationPreparationService:
    """Create, revise, read and evaluate a preparation, and freeze the unit it prepares."""

    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        preflight: RegistrationPreflightService,
        builder: RegistrationSnapshotBuilder,
        duplicate_lookup: DuplicateLookupSource | None = None,
        prepared_assets: PreparedAssetSource | None = None,
        enrichment: EnrichmentResultSource | None = None,
    ) -> None:
        self._enrichment = enrichment
        self._registrations = registrations
        self._preflight = preflight
        self._builder = builder
        self._duplicate_lookup = duplicate_lookup
        self._prepared_assets = prepared_assets

    # ------------------------------------------------------------------ authoring

    def create(
        self,
        draft_id: str,
        *,
        item_ids: Sequence[str],
        inputs: AuthoredInputs,
        actor: str,
        correlation_id: str | None = None,
        revisions: Mapping[str, str | None] | None = None,
    ) -> PreparationRecord:
        """``revisions`` are the authoring revisions as the client submitted them, checked beside
        the ones ``inputs`` would store (see :func:`submitted_view_revisions`)."""
        draft = self._registrations.draft(draft_id)
        if draft is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "the draft does not exist")
        inputs = self._require_owned_revisions(
            draft.marketplace_key, draft.marketplace_account_id, inputs, revisions
        )
        chosen, problems = resolve_unit(
            draft.listing_shape, [item.item_id for item in draft.items], item_ids
        )
        if problems:
            raise InputValidationError(
                "REGISTER_PREPARATION_UNIT_INVALID",
                "the requested Items do not form one provider-listing unit",
                details={"reasons": [problem.code for problem in problems]},
            )
        encoded = encode_inputs(inputs)
        try:
            with self._registrations.transaction() as unit:
                return unit.create_preparation(
                    draft_id,
                    item_ids=chosen,
                    inputs=encoded,
                    created_by=actor,
                    correlation_id=self._correlation(correlation_id),
                )
        except IntegrityError as exc:
            if "registration_preparations.draft_id" not in str(exc):
                raise
            raise RegistrationConflictError(
                "REGISTER_PREPARATION_UNIT_EXISTS",
                "this Draft unit already has a preparation",
            ) from exc

    def update(
        self,
        preparation_id: str,
        *,
        item_ids: Sequence[str],
        inputs: AuthoredInputs,
        actor: str,
        correlation_id: str | None = None,
        revisions: Mapping[str, str | None] | None = None,
    ) -> PreparationRecord:
        """Append the next revision. An earlier one, and any Snapshot it froze, stay as they are."""
        current = self.preparation(preparation_id)
        inputs = self._require_owned_revisions(
            current.marketplace_key, current.marketplace_account_id, inputs, revisions
        )
        encoded = encode_inputs(inputs)
        with self._registrations.transaction() as unit:
            return unit.revise_preparation(
                preparation_id,
                item_ids=item_ids,
                inputs=encoded,
                authored_by=actor,
                correlation_id=self._correlation(correlation_id),
            )

    def apply_enrichment(
        self,
        preparation_id: str,
        apply: EnrichmentApply,
        *,
        actor: str,
        correlation_id: str | None = None,
    ) -> PreparationRecord:
        """ADR-0026 §7 (AIF-4): append one revision whose ``apply.field`` is the enrichment
        result's value with provenance ``AI_SUGGESTION``, or change nothing.

        It is refused when:
        - the field is locked: its value is ``OPERATOR_CONFIRMED`` or ``SOURCE_FACT``, and an AI
          value never overwrites it;
        - the Preparation moved past the revision the operator read: it is skipped, never
          overwritten;
        - the result is not this unit's product's, not this target's, not current, not ``OK`` or
          stale.

        The value never satisfies a required field: ``AI_SUGGESTION`` stays
        ``FIELD_AI_SUGGESTION_UNCONFIRMED`` until the operator confirms it (ADR-0014 §18)."""
        if not _valid_field(apply.field):
            raise InputValidationError(
                AI_APPLY_FIELD_INVALID,
                "an enrichment result is applied to name, attribute.<key> or notice.<key>",
                details={"field": apply.field},
            )
        current = self.preparation(preparation_id)
        draft = self._registrations.draft(current.draft_id)
        products = {
            item.product_group_id
            for item in (draft.items if draft is not None else ())
            if item.item_id in current.current.item_ids
        }
        if apply.product_group_id not in products:
            raise InputValidationError(
                AI_APPLY_RESULT_FOREIGN, "the result is not of a product this unit prepares"
            )
        result = self._named_result(apply, current.marketplace_key, current.marketplace_account_id)
        value = (result.value or {}).get(apply.value_field)
        selection = (
            self._category_selection(result, current, value)
            if apply.field == CATEGORY_FIELD
            else None
        )
        texts = _tag_texts(value) if apply.field == TAGS_FIELD else None
        if apply.field == TAGS_FIELD and texts is None:
            raise InputValidationError(
                AI_APPLY_VALUE_INVALID,
                "the result names no list of 1 to 10 distinct tags under that value field",
                details={"value_field": apply.value_field},
            )
        if apply.field not in (TAGS_FIELD, CATEGORY_FIELD) and (
            not isinstance(value, str | bool | int) or (isinstance(value, str) and not value)
        ):
            raise InputValidationError(
                AI_APPLY_VALUE_INVALID,
                "the result names no text, boolean or integer under that value field",
                details={"value_field": apply.value_field},
            )
        cid = self._correlation(correlation_id)
        with self._registrations.transaction() as unit:
            # Read again in the unit of work that writes: the lock and the revision check are both
            # decided on what is current now (Canonical §7.7: lock and optimistic concurrency).
            now = unit.preparation(preparation_id)
            if now is None:
                raise NotFoundError(
                    "REGISTER_PREPARATION_NOT_FOUND", "the preparation does not exist"
                )
            if now.current.revision_no != apply.expected_revision_no:
                raise RegistrationConflictError(
                    AI_APPLY_REVISION_CHANGED,
                    "the preparation changed since it was read; it was skipped, not overwritten",
                    details={"current_revision_no": now.current.revision_no},
                )
            inputs = decode_inputs(now.current)
            if selection is not None:
                # ADR-0029 AIC-05: an operator-confirmed category is never overwritten.
                if (
                    inputs.category is not None
                    and inputs.category.confirmation is CategoryConfirmation.OPERATOR_CONFIRMED
                ):
                    raise RegistrationConflictError(
                        AI_APPLY_FIELD_LOCKED,
                        "the category is confirmed; an AI category never overwrites it",
                        details={"provenance": "OPERATOR_CONFIRMED"},
                    )
                applied = replace(inputs, category=selection)
            elif texts is not None:
                # A non-empty operator set is confirmed and locked, exactly like the name.
                if inputs.listing.tags and inputs.listing.tags_provenance is None:
                    raise RegistrationConflictError(
                        AI_APPLY_FIELD_LOCKED,
                        "the tags are the operator's own; an AI set never overwrites them",
                        details={"provenance": "OPERATOR_CONFIRMED"},
                    )
                applied = _with_tags(inputs, texts)
            else:
                existing = _field_of(inputs, apply.field)
                if existing is not None and existing.provenance in SATISFYING:
                    raise RegistrationConflictError(
                        AI_APPLY_FIELD_LOCKED,
                        "the field holds a confirmed value; an AI value never overwrites it",
                        details={"provenance": existing.provenance.value},
                    )
                # Proved a non-empty text, boolean or integer above, before the unit of work.
                assert isinstance(value, str | bool | int)
                applied = _with_field(
                    inputs,
                    apply.field,
                    FieldValue(value=value, provenance=Provenance.AI_SUGGESTION),
                )
            revised = unit.revise_preparation(
                preparation_id,
                item_ids=now.current.item_ids,
                inputs=encode_inputs(applied),
                authored_by=actor,
                correlation_id=cid,
            )
            # The revision above holds the database's write lock, so no result can be recorded
            # until this unit of work ends: the named result is checked again under it, and any
            # change since the first check refuses the whole apply (nothing is written).
            again = self._named_result(apply, now.marketplace_key, now.marketplace_account_id)
            if again.value != result.value:
                raise RegistrationConflictError(
                    AI_APPLY_RESULT_CHANGED,
                    "the named result changed while it was applied",
                    details={"result_sequence": again.sequence},
                )
            unit.note_enrichment_applied(
                preparation_id,
                revision_no=revised.current.revision_no,
                field=apply.field,
                enrichment={
                    "product_group_id": apply.product_group_id,
                    "task_key": apply.task_key,
                    "result_key": apply.result_key,
                    "result_sequence": result.sequence,
                    "input_fingerprint": result.input_fingerprint,
                },
                actor=actor,
                correlation_id=cid,
            )
            return revised

    def _category_selection(
        self, result: Any, current: PreparationRecord, value: object
    ) -> CategorySelection:
        """ADR-0029 §5: the AI category as an unconfirmed selection under the target's current
        mapping revision. The result's taxonomy revision must be the target's current one."""
        taxonomy = (result.value or {}).get("taxonomy_revision")
        if not isinstance(value, str) or not value.strip() or not isinstance(taxonomy, str):
            raise InputValidationError(
                AI_APPLY_VALUE_INVALID,
                "the result names no category id and taxonomy revision",
            )
        policy = self._preflight.target_policy(
            current.marketplace_key, current.marketplace_account_id
        )
        if policy is None or policy.taxonomy_revision != taxonomy:
            raise RegistrationConflictError(
                AI_APPLY_RESULT_UNUSABLE,
                "the category was recommended under another taxonomy than the target's current one",
                details={
                    "result_taxonomy_revision": taxonomy,
                    "target_taxonomy_revision": (
                        None if policy is None else policy.taxonomy_revision
                    ),
                },
            )
        return CategorySelection(
            category_id=value,
            mapping_revision=policy.category_mapping_revision,
            taxonomy_revision=taxonomy,
            confirmation=CategoryConfirmation.AI_SUGGESTION,
        )

    def _named_result(
        self, apply: EnrichmentApply, marketplace_key: str, marketplace_account_id: str
    ) -> AppliedResult:
        """The exact result revision the operator named, if it is still the current, fresh OK
        result of exactly that subject; refused otherwise."""
        result = (
            None
            if self._enrichment is None
            else self._enrichment.current_result(
                apply.product_group_id,
                apply.task_key,
                apply.result_key,
                marketplace_key if apply.result_targeted else None,
                marketplace_account_id if apply.result_targeted else None,
            )
        )
        if result is None or result.status != "OK" or result.stale or result.value is None:
            raise RegistrationConflictError(
                AI_APPLY_RESULT_UNUSABLE,
                "only a current, fresh OK result of this target is applied",
                details={
                    "status": None if result is None else result.status,
                    "stale_reasons": [] if result is None else result.stale_reasons,
                },
            )
        if result.sequence != apply.result_sequence:
            raise RegistrationConflictError(
                AI_APPLY_RESULT_CHANGED,
                "the named result is no longer the current one; review the newer result first",
                details={"result_sequence": result.sequence},
            )
        return result

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

    def stage_candidate(self, preparation_id: str) -> PreflightResult:
        """The candidate a mutation stage binds: what is authored now, evaluated the way the
        CREATE path evaluates it (Issue #89 resolution 5915900049 D4).

        When the target policy requires duplicate proof, the final preflight and the first
        CREATE copy evaluate the unit with the admissible evidence of the duplicate-evidence
        owner seam, and that evidence is part of the candidate fingerprint. The ASSET stage —
        its candidate gate, its grant and its eligibility review packet — evaluates with the same
        evidence here, so every stage binds one fingerprint for one canonical candidate. Missing
        evidence refuses (``DUPLICATE_EVIDENCE_UNAVAILABLE``): the stage stays fail-closed and
        nothing is uploaded. A policy without duplicate proof never consults the seam. Nothing
        here changes what the fingerprint covers.
        """
        candidate = self.evaluate(preparation_id)
        if not candidate.resolved.target.duplicate_proof_required:
            return candidate
        evidence = self._current_duplicate_evidence(candidate)
        return self.evaluate(preparation_id, duplicate_evidence=evidence)

    def freeze_current(
        self, preparation_id: str, *, actor: str, correlation_id: str | None = None
    ) -> FrozenUnit:
        """Freeze with the exact current inputs the owners hold — the application freeze path
        (Issue #89 architect follow-up 5919917893 §3).

        The candidate is :meth:`stage_candidate`'s: with a duplicate-proof policy it carries the
        owner seam's admissible evidence, exactly as the ASSET stage and the first CREATE copy do,
        and missing evidence refuses (``DUPLICATE_EVIDENCE_UNAVAILABLE``). When the policy needs
        provider asset identities, the prepared assets are the ones the ASSET upload-attempt owner
        holds for this exact preparation revision under that candidate fingerprint. Nothing is
        substituted: missing duplicate evidence and a missing, stale or mismatched asset are left
        to the final preflight, which names them and refuses (``REGISTER_PREFLIGHT_NOT_READY``,
        the same refusal the unit's FREEZE action shows), and :meth:`freeze` re-evaluates every
        rule and freezes nothing unless READY.
        """
        try:
            candidate = self.stage_candidate(preparation_id)
        except RegistrationConflictError as refused:
            if refused.code != DUPLICATE_EVIDENCE_UNAVAILABLE:
                raise
            # No admissible evidence: the final preflight carries DUPLICATE_EVIDENCE_MISSING under
            # this policy, so it can never be READY, and nothing is frozen.
            return self.freeze(preparation_id, actor=actor, correlation_id=correlation_id)
        prepared: tuple[PreparedAsset, ...] = ()
        if candidate.resolved.target.asset_policy.provider_asset_identity_required:
            source = self._prepared_assets
            if source is None:
                raise RegistrationConflictError(
                    PREPARED_ASSETS_UNAVAILABLE,
                    "no prepared-asset owner is wired, so no provider asset can be frozen",
                )
            preparation = self.preparation(preparation_id)
            prepared = source.prepared_assets(
                marketplace_key=preparation.marketplace_key,
                marketplace_account_id=preparation.marketplace_account_id,
                preparation_revision_id=preparation.current.preparation_revision_id,
                candidate_fingerprint=candidate.candidate_fingerprint,
                selected_artifacts=tuple(
                    image.key for item in candidate.resolved.items for image in item.images
                ),
                asset_profile=candidate.resolved.target.asset_policy.profile,
            )
        return self.freeze(
            preparation_id,
            actor=actor,
            duplicate_evidence=candidate.request.duplicate_evidence,
            prepared_assets=prepared,
            correlation_id=correlation_id,
        )

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
        with self._registrations.transaction() as unit:
            # Snapshot, provenance, Batch and Intent are one commit. A failure at any point leaves
            # none of them, so a restart never strands SNAPSHOT_FROZEN authoring work.
            snapshot = self._builder.freeze(
                final,
                created_by=actor,
                correlation_id=correlation,
                preparation_revision_id=revision.preparation_revision_id,
                registrations=unit,
            )
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

    def execution_copy(self, registration_snapshot_id: str) -> ExecutionCopy:
        """Build the first job copy from the exact revision that produced this Snapshot.

        Current owner truth is evaluated at the Snapshot's pinned generation, then the resulting
        fingerprint, listing identity and payload are compared with the immutable Snapshot. A
        later preparation revision is never consulted.
        """
        snapshot = self._registrations.snapshot(registration_snapshot_id)
        if snapshot is None:
            raise NotFoundError("REGISTER_SNAPSHOT_NOT_FOUND", "the Snapshot does not exist")
        provenance = self._registrations.snapshot_preparation(registration_snapshot_id)
        if provenance is None:
            raise RegistrationConflictError(
                "REGISTER_SNAPSHOT_PROVENANCE_MISSING",
                "the Snapshot has no authored preparation provenance",
            )
        preparation = self._registrations.preparation_of_revision(
            provenance.preparation_revision_id
        )
        if preparation is None:
            raise RegistrationConflictError(
                "REGISTER_PREPARATION_REVISION_NOT_FOUND",
                "the Snapshot's authored revision no longer exists",
            )
        revision = next(
            (
                revision
                for revision in preparation.revisions
                if revision.preparation_revision_id == provenance.preparation_revision_id
            ),
            None,
        )
        if revision is None:
            raise RegistrationConflictError(
                "REGISTER_PREPARATION_REVISION_NOT_FOUND",
                "the Snapshot's authored revision no longer exists",
            )
        request = preflight_request(preparation, revision)
        candidate = self._preflight.candidate(
            request, identity_generation=provenance.identity_generation
        )
        if candidate.resolved.target.duplicate_proof_required:
            evidence = self._current_duplicate_evidence(candidate)
            request = preflight_request(preparation, revision, duplicate_evidence=evidence)
            candidate = self._preflight.candidate(
                request, identity_generation=provenance.identity_generation
            )
        prepared_assets = _prepared_assets(
            self._registrations.snapshot_payload(registration_snapshot_id) or {},
            candidate.candidate_fingerprint,
        )
        final = self._preflight.final(
            request,
            prepared_assets,
            identity_generation=provenance.identity_generation,
        )
        if final.status is not ReadinessStatus.READY:
            raise RegistrationConflictError(
                "REGISTER_FIRST_CREATE_INPUTS_NOT_READY",
                "current owner truth cannot reproduce the Snapshot's first CREATE inputs",
                details={"status": final.status.value, "reasons": list(final.codes)},
            )
        outbound = build_payload(final)
        if (
            final.resolved.listing_identity != snapshot.listing_identity
            or final.dependency_fingerprint != snapshot.preflight_fingerprint
            or outbound.payload_digest != snapshot.payload_hash
        ):
            raise RegistrationConflictError(
                "REGISTER_FIRST_CREATE_INPUTS_STALE",
                "current approved execution inputs do not match the immutable Snapshot",
            )
        return ExecutionCopy(request=request, final=final)

    # ------------------------------------------------------------------ helpers

    def _require_owned_revisions(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        inputs: AuthoredInputs,
        submitted: Mapping[str, str | None] | None,
    ) -> AuthoredInputs:
        """The authoring revisions are server-owned (decision 5801915996): each one a client
        submits, and each one these inputs would store, must be **exactly** the account's current
        target-policy value — ``None`` included under a policy revision appended before the
        authoring-revision owner existed (ADR-0014 §27.1). Anything else is refused
        before anything is written, so no sentinel, default or stale revision ever becomes an
        authored input.

        B-DETAIL: the composition's sections are server-derived. A composition is stored with
        exactly its owned profile's sections, never a client's own; a client section the profile
        does not hold is refused. Without a profile the sections stay as authored (BODY-only)."""
        target = self._preflight.target_policy(marketplace_key, marketplace_account_id)
        owned: dict[str, str | None] = {
            MAPPING_REVISION: None if target is None else target.category_mapping_revision,
            COMPOSITION_REVISION: None if target is None else target.detail_composition_revision,
        }
        checked = [*submitted_revisions(inputs).items(), *(submitted or {}).items()]
        mismatched = sorted({name for name, value in checked if value != owned[name]})
        if mismatched:
            raise InputValidationError(
                AUTHORING_REVISION_NOT_OWNED,
                "authoring revisions are server-owned: send back exactly the server's values",
                details={"fields": mismatched},
            )
        detail = inputs.detail
        profile = (
            None if target is None else self._preflight.detail_profile(owned[COMPOSITION_REVISION])
        )
        if detail is None or profile is None:
            return inputs
        if not set(detail.sections) <= set(profile.sections):
            raise InputValidationError(
                DETAIL_SECTIONS_NOT_PROFILE,
                "the detail sections are the composition profile's own",
                details={"sections": sorted(set(detail.sections) - set(profile.sections))},
            )
        return replace(inputs, detail=replace(detail, sections=profile.sections))

    def _draft_revision(self, draft_id: str) -> int:
        draft = self._registrations.draft(draft_id)
        if draft is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "the draft does not exist")
        return draft.draft_revision

    def _current_duplicate_evidence(self, candidate: PreflightResult) -> DuplicateEvidence:
        """Read current evidence through the provider-neutral owner seam, or fail closed.

        The production SmartStore implementation reports unavailable while no duplicate lookup is
        adopted, so this method performs no provider call in that state. Preparations never store
        the outcome; the first CREATE copy and the mutation-stage candidate
        (:meth:`stage_candidate`) only use this current read.
        """
        source = self._duplicate_lookup
        if source is None or not source.available():
            raise RegistrationConflictError(
                DUPLICATE_EVIDENCE_UNAVAILABLE,
                "current duplicate evidence is unavailable for the first CREATE copy",
            )
        unit = candidate.resolved
        evidence = source.evidence(
            marketplace_account_id=unit.marketplace_account_id,
            listing_identity=unit.listing_identity,
        )
        if evidence is None:
            raise RegistrationConflictError(
                DUPLICATE_EVIDENCE_UNAVAILABLE,
                "the duplicate-evidence owner returned no current evidence",
            )
        return evidence

    @staticmethod
    def _correlation(correlation_id: str | None) -> str:
        return correlation_id or get_correlation_id() or new_correlation_id()


def _prepared_assets(
    payload: Mapping[str, Any], candidate_fingerprint: str
) -> tuple[PreparedAsset, ...]:
    """Provider asset identities frozen in the Snapshot, rebound to the rechecked candidate."""
    found: dict[tuple[str, str, str], PreparedAsset] = {}
    for item in payload.get("items", ()):
        for asset in item.get("publication_assets", ()):
            provider_ref = asset.get("provider_asset_ref")
            if provider_ref is None:
                continue
            prepared = PreparedAsset(
                asset_kind=ImageAssetKind(asset["asset_kind"]),
                sha256=asset["sha256"],
                derivation_id=asset.get("derivation_id"),
                asset_profile=asset["asset_profile"],
                candidate_fingerprint=candidate_fingerprint,
                provider_asset_ref=provider_ref,
            )
            found[prepared.key] = prepared
    return tuple(found[key] for key in sorted(found))
