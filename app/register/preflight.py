"""M5 PR-C: the registration preflight service (ADR-0014 §3; Issue #89 kickoff 5742880432).

It gathers the current truth of every owner a provider-listing unit depends on, and hands it to
the pure evaluation (:func:`app.register.preparation.evaluate`). It writes nothing and stores no
result: a preflight is recomputed every time.

What it reads, and from whom:
- the Draft, its pins and the Snapshots and Intents of the conflict and duplicate scopes: the
  registration store (PR-B);
- the canonical account binding: the CONNECT account owner; the auth and workflow state: the
  CONNECT capability owner, through :class:`CapabilityReader` so that REGISTER never imports a
  CONNECT service or a provider caller;
- base and target pricing readiness, the current price for the target context and the current
  procurement: the M4 readiness and pricing owners; the selected images and their exact-binary QA:
  the M4 image owner. Nothing is priced, selected or recorded here;
- the category metadata: a :class:`~app.register.policy.RegistrationMetadataSource`.

No provider is called: no duplicate lookup, no upload, no marketplace read. Provider duplicate
evidence and prepared provider assets are inputs a later adapter supplies.
"""

from collections.abc import Sequence
from typing import Protocol

from app.connect.accounts import AccountBinding, binding_state
from app.connect.marketplace.contracts import MarketplaceCapabilityView
from app.core.errors import AppError, NotFoundError, PolicyBlockedError
from app.products.images import ProductImageService
from app.products.pricing_service import ProductPricingService
from app.products.readiness import ProductReadinessService, Readiness
from app.register.policy import (
    RegistrationMetadataSource,
    RegistrationPolicySource,
    TargetPolicy,
)
from app.register.preparation import (
    AccountState,
    BindingCopy,
    ConflictState,
    DraftItemState,
    LiveRegistration,
    OverrideCoverage,
    PinnedPrice,
    PreflightRequest,
    PreflightResult,
    PreflightStage,
    PreparedAsset,
    PublicationImage,
    ReadinessInput,
    ResolvedItem,
    ResolvedUnit,
    evaluate,
    resolve_unit,
)
from app.register.preparation import listing_identity as _identity
from app.register.store import RegistrationStore, RegistrationUnit


class CapabilityReader(Protocol):
    """The CONNECT capability owner's read side (``MarketplaceCapabilityService.capability``)."""

    def capability(self, marketplace_key: str) -> MarketplaceCapabilityView: ...


def _readiness(readiness: Readiness) -> ReadinessInput:
    return ReadinessInput(
        status=readiness.status,
        reasons=readiness.reasons,
        rule_version=readiness.rule_version,
        dependency_fingerprint=readiness.dependency_fingerprint,
    )


class RegistrationPreflightService:
    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        readiness: ProductReadinessService,
        pricing: ProductPricingService,
        images: ProductImageService,
        capability: CapabilityReader,
        metadata: RegistrationMetadataSource,
        policies: RegistrationPolicySource,
    ) -> None:
        self._registrations = registrations
        self._readiness = readiness
        self._pricing = pricing
        self._images = images
        self._capability = capability
        self._metadata = metadata
        self._policies = policies

    def candidate(self, request: PreflightRequest) -> PreflightResult:
        """The mutation-free non-asset candidate: only its READY permits an asset upload."""
        return evaluate(request, self.resolve(request), PreflightStage.CANDIDATE)

    def final(
        self,
        request: PreflightRequest,
        prepared_assets: Sequence[PreparedAsset] = (),
        *,
        identity_generation: int | None = None,
    ) -> PreflightResult:
        """Every dependency, the prepared provider assets included: READY freezes a Snapshot.

        ``identity_generation`` re-evaluates an **already frozen** unit. A unit's identity is
        derived from its generation — the number of its Snapshots an Intent names (§7) — so once
        an Intent exists, deriving it again would name the *next* unit, not this one. A send gate
        re-checks the same frozen unit under current truth (PR-E) and passes the generation the
        Snapshot was frozen at; a fresh preparation passes nothing and gets the next one.
        """
        return evaluate(
            request,
            self.resolve(request, identity_generation=identity_generation),
            PreflightStage.FINAL,
            prepared_assets,
        )

    # ------------------------------------------------------------------ gathering

    def resolve(
        self, request: PreflightRequest, *, identity_generation: int | None = None
    ) -> ResolvedUnit:
        with self._registrations.reading() as unit:
            draft = unit.draft(request.unit.draft_id)
            if draft is None:
                raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "the draft does not exist")
            binding = binding_state(
                unit.session, draft.marketplace_key, draft.marketplace_account_id
            )
        target = self._policies.target(draft.marketplace_key, draft.marketplace_account_id)
        if target is None:
            # No Settings/platform policy for this account: nothing can be evaluated against it.
            raise PolicyBlockedError(
                "REGISTER_TARGET_POLICY_MISSING",
                "the account has no registration policy; configure it before a preflight",
            )
        open_items = tuple(
            DraftItemState(i.item_id, i.ordinal, i.pricing_snapshot_id) for i in draft.items
        )
        unit_ids, _problems = resolve_unit(
            draft.listing_shape, [i.item_id for i in open_items], request.unit.item_ids
        )
        pins = {i.item_id: i.pricing_snapshot_id for i in open_items}
        ordinals = {i.item_id: i.ordinal for i in open_items}
        items = tuple(
            self._item(target, draft.marketplace_account_id, item_id, ordinals, pins)
            for item_id in unit_ids
        )
        unit_key = [item.key for item in items]
        groups = {item.product_group_id for item in items}
        with self._registrations.reading() as unit:
            generation = (
                unit.unit_generation(draft.draft_id, unit_key)
                if identity_generation is None
                else identity_generation
            )
            identity = _identity(
                draft.marketplace_key,
                draft.marketplace_account_id,
                draft.draft_id,
                unit_key,
                generation,
            )
            conflicts = unit.conflicts_for(
                draft.marketplace_key, draft.marketplace_account_id, groups, identity
            )
            live = unit.live_registrations(
                draft.marketplace_key, draft.marketplace_account_id, groups
            )
        category = request.category
        metadata = (
            None
            if category is None
            else self._metadata.category(target.taxonomy_revision, category.category_id)
        )
        return ResolvedUnit(
            marketplace_key=draft.marketplace_key,
            marketplace_account_id=draft.marketplace_account_id,
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            listing_shape=draft.listing_shape,
            open_items=open_items,
            unit_item_ids=unit_ids,
            items=items,
            account=self._account(draft.marketplace_key, binding),
            listing_identity=identity,
            identity_generation=generation,
            conflicts=tuple(ConflictState(c.intent_id, c.state.value) for c in conflicts),
            live_registrations=tuple(LiveRegistration(r) for r in live),
            metadata=metadata,
            target=target,
        )

    def _account(self, marketplace_key: str, binding: AccountBinding) -> AccountState:
        try:
            view = self._capability.capability(marketplace_key)
        except AppError:
            # A capability that cannot be read is never READY (ADR-0014 §3).
            return AccountState(binding=binding, capability_available=False)
        return AccountState(
            binding=binding,
            capability_available=True,
            auth=view.auth,
            write_scope=view.write_scope.status,
            overlays=tuple((o.workflow_scope, o.workflow_state) for o in view.workflow),
        )

    def _item(
        self,
        target: TargetPolicy,
        marketplace_account_id: str,
        item_id: str,
        ordinals: dict[str, int],
        pins: dict[str, str],
    ) -> ResolvedItem:
        context = target.pricing_context
        evaluation = self._pricing.evaluate(item_id, context)
        procurement = evaluation.procurement
        current = evaluation.current_snapshot
        with self._registrations.reading() as unit:
            pin = self._pin(unit, pins[item_id])
            overrides = unit.active_overrides(
                target.marketplace_key, marketplace_account_id, procurement.item.product_group_id
            )
        binding = procurement.binding
        member = procurement.member
        selection_revision_id, images = self._publication(item_id)
        return ResolvedItem(
            item_id=item_id,
            ordinal=ordinals[item_id],
            product_group_id=procurement.item.product_group_id,
            composition_id=procurement.item.composition_id,
            composition_signature=procurement.item.composition_signature,
            pin=pin,
            current_price_id=None if current is None else current.pricing_snapshot_id,
            base=_readiness(self._readiness.base_readiness(item_id)),
            pricing=_readiness(self._readiness.pricing_readiness(item_id, context)),
            binding=None
            if binding is None
            else BindingCopy(
                binding_id=binding.binding_id,
                binding_kind=binding.binding_kind.value,
                group_member_id=binding.group_member_id,
                source_product_uid=None if member is None else member.source_product_uid,
                provenance_revision_id=binding.provenance_revision_id,
                fulfillment_quantity=binding.fulfillment_quantity,
                quantity_offer_id=binding.quantity_offer_id,
            ),
            selection_revision_id=selection_revision_id,
            images=images,
            overrides=tuple(
                OverrideCoverage(o.override_id, o.product_group_id, o.listing_composition_id)
                for o in overrides
            ),
        )

    @staticmethod
    def _pin(unit: RegistrationUnit, pricing_snapshot_id: str) -> PinnedPrice | None:
        record = unit.pricing_pin(pricing_snapshot_id)
        if record is None:
            return None
        return PinnedPrice(
            pricing_snapshot_id=record.pricing_snapshot_id,
            item_id=record.item_id,
            marketplace_key=record.marketplace_key,
            account_id=record.account_id,
            pricing_context_fingerprint=record.pricing_context_fingerprint,
            dependency_fingerprint=record.dependency_fingerprint,
            membership_revision_id=record.membership_revision_id,
            source_binding_id=record.source_binding_id,
            source_product_facts_revision_id=record.source_product_facts_revision_id,
            final_sale_price_krw=record.final_sale_price_krw,
            price_basis=record.price_basis,
            price_guard=record.price_guard,
        )

    def _publication(self, item_id: str) -> tuple[str | None, tuple[PublicationImage, ...]]:
        """The Item's current operator image selection and each exact binary's QA verdict under
        the selection's validated revision (M4 owner, read only)."""
        selection = self._images.current_selection(item_id)
        if selection is None:
            return None, ()
        images = []
        for output in selection.outputs:
            qa = self._images.qa_for_selected_asset(
                asset_kind=output.asset_kind,
                sha256=output.sha256,
                derivation_id=output.derivation_id,
                validated_source_revision_id=selection.source_revision_id,
            )
            images.append(
                PublicationImage(
                    role=output.role,
                    position=output.position,
                    asset_kind=output.asset_kind,
                    sha256=output.sha256,
                    derivation_id=output.derivation_id,
                    qa_result_id=None if qa is None else qa.qa_result_id,
                    qa_verdict=None if qa is None else qa.verdict,
                )
            )
        return selection.selection_revision_id, tuple(images)
