"""The image auto-selection rule and its automatic QA (Issue #219, owner decision 2026-10-03).

ADR-0013 §9 amendment: an Item's image selection may be the rule's, recorded as the rule's own
(``DecisionOrigin.RULE``) and never as an operator's; an operator's selection always supersedes it.
The rule reads only the Item's current bound revision, the supplier's own image role rules, the
supplier common image verdicts and stored file facts. It never reads an image's content.

**The rule** (``image-auto-selection/v1``) over the CONFIRMED source images of the revision — a
failed image is never a candidate, and its failure stays the revision's own:

* each image's place on the supplier's page comes from the role-rule name the supplier's own
  classifier recorded with it (``provenance``), looked up in the table that supplier publishes
  (:class:`SupplierImageRoles`) — never from a host, a path or the order alone. A supplier without
  a table, or an image whose rule name the table does not hold, leaves the Item unselected and
  says why;
* the **representative** image is the first image of the representative slot;
* the **additional** images are those of the additional slot, in page order, whose bytes are not
  already used, up to :data:`MAX_ADDITIONAL_IMAGES`;
* the **detail** images are those of the detail slot, in page order, whose bytes are not already
  used and that are not a blocked, or an undecided, supplier common image;
* the same SHA-256 is used once; an image of any other slot is left out.

**Automatic QA** (``image-qa-auto/v1``, Issue #219 §4): each selected source image gets a verdict
from its stored file facts only — its bytes are stored, its media type is an image type ICBM
handles, its size is within the collection bound and its dimensions were decoded. A file that
fails a check is ``REVIEW_REQUIRED`` with the check as a finding; nothing is ever ``FAIL`` on the
rule's word. An operator's verdict under the rule in force supersedes it.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from sqlalchemy import select

from app.capabilities.audit.service import AuditLog
from app.platform.core.clock import Clock
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.collect.facts import ImageRole
from app.stages.collect.models import ProductFactsRevision, SourceAsset
from app.stages.products.common_images import (
    CommonImageVerdict,
    effective_supplier,
    verdicts,
)
from app.stages.products.image_model import (
    AUTO_QA_RULE_VERSION,
    DecisionOrigin,
    ImageAssetKind,
    OutputRole,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
    SourceRef,
    digest,
)
from app.stages.products.image_store import ImageUnit, SelectionRecord
from app.stages.products.images import ProductImageService
from app.stages.products.models import SourceBinding
from app.stages.products.pricing_service import current_procurement
from app.stages.products.store import ProductFoundationStore

AUTO_SELECTION_RULE_VERSION: Final = "image-auto-selection/v1"
# SmartStore's optionalImages hold at most nine images beside the representative one, the first
# vertical's gallery bound (Issue #219 §2.2).
MAX_ADDITIONAL_IMAGES: Final = 9
# The automatic QA's file checks.
AUTO_QA_MEDIA_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
# The byte bound follows the owner's per-image collection limit, 5 MiB for every supplier
# (Issue #219 6086299406), so a collected image is never refused here.
AUTO_QA_MAX_BYTES: Final = 5 * 1024 * 1024


class ImageSlot(StrEnum):
    """Where a supplier's page places an image, as the supplier's own role rules say."""

    REPRESENTATIVE = "REPRESENTATIVE"
    ADDITIONAL = "ADDITIONAL"
    DETAIL = "DETAIL"
    AUXILIARY = "AUXILIARY"


class SupplierImageRoles(Protocol):
    """Each supplier's published role-rule table: role-rule name (``provenance``) → slot."""

    def slots(self, supplier_key: str) -> Mapping[str, ImageSlot] | None:
        """The supplier's table, or ``None`` when the supplier publishes none."""
        ...


class AutoSelectionBlocked(StrEnum):
    """Why the rule leaves an Item unselected."""

    NO_BOUND_REVISION = "NO_BOUND_REVISION"
    ROLE_RULES_UNAVAILABLE = "ROLE_RULES_UNAVAILABLE"
    ROLE_RULE_UNKNOWN = "ROLE_RULE_UNKNOWN"
    REPRESENTATIVE_MISSING = "REPRESENTATIVE_MISSING"


class PlacementNote(StrEnum):
    """Why the rule placed or left out one image (recorded with the selection's audit)."""

    REPRESENTATIVE = "REPRESENTATIVE"
    ADDITIONAL = "ADDITIONAL"
    DETAIL = "DETAIL"
    DUPLICATE_BYTES = "DUPLICATE_BYTES"
    COMMON_IMAGE_BLOCKED = "COMMON_IMAGE_BLOCKED"
    COMMON_IMAGE_UNDECIDED = "COMMON_IMAGE_UNDECIDED"
    OVER_LIMIT = "OVER_LIMIT"
    OTHER_SLOT = "OTHER_SLOT"


@dataclass(frozen=True)
class AutoPlan:
    """The rule's complete decision over one revision, or why it makes none."""

    decisions: tuple[SourceDecision, ...] = ()
    outputs: tuple[SelectedOutput, ...] = ()
    notes: tuple[tuple[str, int, str], ...] = ()
    blocked: AutoSelectionBlocked | None = None
    detail: str | None = None

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "rule": AUTO_SELECTION_RULE_VERSION,
                "decisions": [
                    [d.role.value, d.ordinal, d.sha256, d.decision.value] for d in self.decisions
                ],
                "outputs": [
                    [o.role.value, o.source_role.value, o.source_ordinal] for o in self.outputs
                ],
            }
        )


_SLOT_OUTPUT: Final[Mapping[ImageSlot, OutputRole]] = {
    ImageSlot.REPRESENTATIVE: OutputRole.REPRESENTATIVE,
    ImageSlot.ADDITIONAL: OutputRole.ADDITIONAL,
    ImageSlot.DETAIL: OutputRole.DETAIL,
}
_COMMON_NOTE: Final[Mapping[CommonImageVerdict, PlacementNote]] = {
    CommonImageVerdict.BLOCK: PlacementNote.COMMON_IMAGE_BLOCKED,
    CommonImageVerdict.REVIEW: PlacementNote.COMMON_IMAGE_UNDECIDED,
}


def plan(
    refs: Sequence[SourceRef],
    provenance: Mapping[tuple[ImageRole, int], str],
    slots: Mapping[str, ImageSlot] | None,
    common: Mapping[str, CommonImageVerdict],
) -> AutoPlan:
    """The rule's decision over the CONFIRMED images of one revision. Pure and deterministic."""
    if slots is None:
        return AutoPlan(blocked=AutoSelectionBlocked.ROLE_RULES_UNAVAILABLE)
    placed: dict[tuple[ImageRole, int], ImageSlot] = {}
    for ref in refs:
        name = provenance.get((ref.role, ref.ordinal))
        slot = None if name is None else slots.get(name)
        if slot is None:
            return AutoPlan(blocked=AutoSelectionBlocked.ROLE_RULE_UNKNOWN, detail=name)
        placed[(ref.role, ref.ordinal)] = slot
    ordered = sorted(refs, key=lambda ref: ref.ordinal)

    def of(slot: ImageSlot) -> list[SourceRef]:
        return [ref for ref in ordered if placed[(ref.role, ref.ordinal)] is slot]

    representatives = of(ImageSlot.REPRESENTATIVE)
    if not representatives:
        return AutoPlan(blocked=AutoSelectionBlocked.REPRESENTATIVE_MISSING)
    chosen: list[tuple[SourceRef, OutputRole]] = [(representatives[0], OutputRole.REPRESENTATIVE)]
    used = {representatives[0].sha256}
    notes: dict[tuple[ImageRole, int], PlacementNote] = {
        (representatives[0].role, representatives[0].ordinal): PlacementNote.REPRESENTATIVE
    }
    additional = 0
    for slot in (ImageSlot.REPRESENTATIVE, ImageSlot.ADDITIONAL, ImageSlot.DETAIL):
        for ref in of(slot):
            key = (ref.role, ref.ordinal)
            if key in notes:
                continue
            if ref.sha256 in used:
                notes[key] = PlacementNote.DUPLICATE_BYTES
                continue
            if slot is ImageSlot.REPRESENTATIVE:
                # One representative image; another one of its slot is left for the operator.
                notes[key] = PlacementNote.OTHER_SLOT
                continue
            verdict = common.get(ref.sha256)
            if verdict in _COMMON_NOTE:
                notes[key] = _COMMON_NOTE[verdict]  # type: ignore[index]
                continue
            if slot is ImageSlot.ADDITIONAL:
                if additional >= MAX_ADDITIONAL_IMAGES:
                    notes[key] = PlacementNote.OVER_LIMIT
                    continue
                additional += 1
            chosen.append((ref, _SLOT_OUTPUT[slot]))
            used.add(ref.sha256)
            notes[key] = PlacementNote(_SLOT_OUTPUT[slot].value)
    for ref in ordered:
        notes.setdefault((ref.role, ref.ordinal), PlacementNote.OTHER_SLOT)
    uses = {(ref.role, ref.ordinal) for ref, _role in chosen}
    decisions = tuple(
        SourceDecision(
            role=ref.role,
            ordinal=ref.ordinal,
            sha256=ref.sha256,
            decision=SourceDecisionKind.USE_SOURCE
            if (ref.role, ref.ordinal) in uses
            else SourceDecisionKind.EXCLUDE,
        )
        for ref in refs
    )
    outputs = tuple(
        SelectedOutput(role=role, source_role=ref.role, source_ordinal=ref.ordinal)
        for ref, role in chosen
    )
    return AutoPlan(
        decisions=decisions,
        outputs=outputs,
        notes=tuple(
            (ref.role.value, ref.ordinal, notes[(ref.role, ref.ordinal)].value) for ref in refs
        ),
    )


def auto_qa(asset: SourceAsset | None) -> tuple[QaVerdict, tuple[str, ...]]:
    """The automatic rule QA of one source image from its stored file facts only."""
    if asset is None:
        return QaVerdict.REVIEW_REQUIRED, ("AUTO_QA_BYTES_NOT_STORED",)
    findings = []
    if asset.mime_type not in AUTO_QA_MEDIA_TYPES:
        findings.append("AUTO_QA_MEDIA_TYPE")
    if not 0 < asset.byte_size <= AUTO_QA_MAX_BYTES:
        findings.append("AUTO_QA_BYTE_SIZE")
    if not (asset.width and asset.height and asset.width > 0 and asset.height > 0):
        findings.append("AUTO_QA_DIMENSIONS")
    return (QaVerdict.REVIEW_REQUIRED, tuple(findings)) if findings else (QaVerdict.PASS, ())


class AutoSelectionStatus(StrEnum):
    SELECTED = "SELECTED"  # a new rule selection is current
    UNCHANGED = "UNCHANGED"  # the current rule selection is already this plan
    OPERATOR_HELD = "OPERATOR_HELD"  # an operator's selection is current; the rule never moves it
    BLOCKED = "BLOCKED"  # the rule makes no selection, and says why


@dataclass(frozen=True)
class AutoSelection:
    item_id: str
    status: AutoSelectionStatus
    selection: SelectionRecord | None = None
    blocked: AutoSelectionBlocked | None = None
    detail: str | None = None


class ImageAutoSelector:
    """The rule's owner: it records the rule's selection and the automatic QA of what it chose."""

    def __init__(
        self,
        *,
        store: ProductFoundationStore,
        images: ProductImageService,
        roles: SupplierImageRoles,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._store = store
        self._images = images
        self._roles = roles
        self._audit = audit
        self._clock = clock

    def preview(self, item_id: str) -> AutoPlan:
        """The rule's plan over the Item's current bound revision, read only: what it would
        select, or why it would not (shown to the operator beside the candidates)."""
        with self._store.reading() as unit:
            procurement = current_procurement(unit, item_id)
            binding = procurement.binding
            revision_id = procurement.current_revision_id
            if (
                binding is None
                or revision_id is None
                or (binding.provenance_revision_id != revision_id)
            ):
                return AutoPlan(blocked=AutoSelectionBlocked.NO_BOUND_REVISION)
            return self.plan_for(ImageUnit(unit.session, self._clock), revision_id)

    def plan_for(self, images: ImageUnit, revision_id: str) -> AutoPlan:
        """The rule's plan over one revision, read in the caller's unit."""
        revision = images.session.get(ProductFactsRevision, revision_id)
        if revision is None:
            return AutoPlan(blocked=AutoSelectionBlocked.NO_BOUND_REVISION)
        supplier = effective_supplier(images.session, revision)
        refs = images.confirmed_refs(revision_id)
        return plan(
            refs,
            images.confirmed_provenance(revision_id),
            self._roles.slots(supplier),
            verdicts(images.session, supplier, [ref.sha256 for ref in refs]),
        )

    def auto_select(self, item_id: str, *, correlation_id: str | None = None) -> AutoSelection:
        """Make the rule's selection current for one Item, unless an operator's is."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        with self._store.transaction() as unit:
            procurement = current_procurement(unit, item_id)
            binding = procurement.binding
            revision_id = procurement.current_revision_id
            if (
                binding is None
                or revision_id is None
                or (binding.provenance_revision_id != revision_id)
            ):
                return AutoSelection(
                    item_id,
                    AutoSelectionStatus.BLOCKED,
                    None,
                    AutoSelectionBlocked.NO_BOUND_REVISION,
                )
            images = ImageUnit(unit.session, self._clock)
            move = images.current_move(item_id)
            current = None if move is None else images.selection(move.selection_revision_id)
            if current is not None and current.decision_origin is DecisionOrigin.OPERATOR:
                return AutoSelection(item_id, AutoSelectionStatus.OPERATOR_HELD, current)
            found = self.plan_for(images, revision_id)
            if found.blocked is not None:
                return AutoSelection(
                    item_id, AutoSelectionStatus.BLOCKED, current, found.blocked, found.detail
                )
            if (
                current is not None
                and current.source_revision_id == revision_id
                and _same_plan(current, found)
            ):
                return AutoSelection(item_id, AutoSelectionStatus.UNCHANGED, current)
            selection, _move = self._images.record_selection_in(
                unit,
                item_id,
                source_revision_id=revision_id,
                decisions=found.decisions,
                outputs=found.outputs,
                decided_by=AUTO_SELECTION_RULE_VERSION,
                reason=None,
                origin=DecisionOrigin.RULE,
                correlation_id=correlation,
                notes=found.notes,
            )
        self._record_auto_qa(selection, correlation)
        return AutoSelection(item_id, AutoSelectionStatus.SELECTED, selection)

    def _record_auto_qa(self, selection: SelectionRecord, correlation: str) -> None:
        with self._store.reading() as unit:
            assets = {
                output.sha256: unit.session.get(SourceAsset, output.sha256)
                for output in selection.outputs
                if output.asset_kind is ImageAssetKind.SOURCE_ASSET
            }
            facts = {sha: auto_qa(asset) for sha, asset in assets.items()}
        for sha, (verdict, findings) in sorted(facts.items()):
            self._images.record_qa(
                asset_kind=ImageAssetKind.SOURCE_ASSET,
                sha256=sha,
                derivation_id=None,
                validated_source_revision_id=selection.source_revision_id,
                verdict=verdict,
                findings=findings,
                qa_rule_version=AUTO_QA_RULE_VERSION,
                decided_by=AUTO_QA_RULE_VERSION,
                correlation_id=correlation,
            )

    def auto_select_all(
        self, item_ids: Iterable[str] | None = None, *, correlation_id: str | None = None
    ) -> tuple[AutoSelection, ...]:
        """The rule over the named Items, or every Item with an open source binding."""
        if item_ids is None:
            with self._store.reading() as unit:
                item_ids = list(
                    unit.session.scalars(
                        select(SourceBinding.item_id)
                        .where(SourceBinding.valid_to.is_(None))
                        .order_by(SourceBinding.item_id)
                    )
                )
        return tuple(self.auto_select(i, correlation_id=correlation_id) for i in item_ids)


def _same_plan(current: SelectionRecord, found: AutoPlan) -> bool:
    decided = sorted(
        (d.role.value, d.ordinal, d.sha256, d.decision.value) for d in current.decisions
    )
    planned = sorted((d.role.value, d.ordinal, d.sha256, d.decision.value) for d in found.decisions)
    outputs = [(o.role.value, o.source_role.value, o.source_ordinal) for o in current.outputs]
    planned_outputs = [(o.role.value, o.source_role.value, o.source_ordinal) for o in found.outputs]
    return decided == planned and outputs == planned_outputs


class StaticSupplierImageRoles:
    """A role-rule table source over fixed tables, keyed by supplier."""

    def __init__(self, tables: Mapping[str, Mapping[str, ImageSlot]]) -> None:
        self._tables = {key: dict(table) for key, table in tables.items()}

    def slots(self, supplier_key: str) -> Mapping[str, ImageSlot] | None:
        return self._tables.get(supplier_key)


def slot_table(
    rules: Iterable[tuple[str, str]], slot_of: Callable[[str], ImageSlot | None]
) -> dict[str, ImageSlot]:
    """A supplier's table from its published (rule name, supplier role) pairs; a role that is not
    a product image (a layout asset, an unrecognised reference) has no slot."""
    table: dict[str, ImageSlot] = {}
    for name, role in rules:
        slot = slot_of(role)
        if slot is not None:
            table[name] = slot
    return table
