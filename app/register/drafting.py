"""A RegistrationDraft from the operator's Product DB selection (Gate 1 G1-D, ADR-0015 §5).

Issue #89 authorization 5796323069. One command composes the canonical owners and replaces none:

    Product DB selection      ProductsService.registration_target (G1-C), revalidated now
    → canonical account       MarketplaceAccountStore: it exists for the marketplace, and is BOUND
    → current target policy   the production RegistrationPolicySource (G1-A), read once
    → price of every Item     M4 ProductPricingService.price(item_id, policy.pricing_context)
    → one Draft               RegistrationStore.create_draft + add_draft_item, one unit of work

The client names only its choices: the Product, the membership revision and the Items it chose,
the marketplace and canonical account, the listing shape and itself. Everything else — the policy
revision, the pricing context, every price and snapshot identity — comes from its owner.

**No partial Draft.** Every chosen Item is priced before anything opens. An Item M4 does not
price refuses the whole command with M4's own reasons; snapshots M4 already recorded for the
other Items stay, as M4's own history, but no Draft or Draft Item exists. The Draft and all its
Items are written in one registration unit of work, so a failure there leaves nothing either.

**Nothing is re-decided.** No price is computed here and no pricing rule is read: the pin is the
exact snapshot M4 returned — ``RECORDED`` or ``UNCHANGED`` alike. The only checks on it are
identity checks: the snapshot names the Item, the membership revision and the binding the
revalidated selection named, and its pricing context is the policy's. Anything else means the
selection moved while the command ran, and nothing is written.

It creates no preparation, Snapshot, Intent, Attempt or job, and calls no provider.
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.connect.accounts import AccountBinding, MarketplaceAccountStore
from app.core.errors import NotFoundError, PolicyBlockedError
from app.products.contracts import RegistrationTargetView
from app.products.pricing import PriceBasis, PriceGuard
from app.products.pricing_service import PricingOutcome, ProductPricingService
from app.products.pricing_store import PricingSnapshotRecord
from app.products.service import ProductsService
from app.register.model import ListingShape, RegistrationConflictError
from app.register.policy import RegistrationPolicySource, TargetPolicy
from app.register.store import RegistrationStore

TARGET_ACCOUNT_UNKNOWN = "REGISTER_TARGET_ACCOUNT_UNKNOWN"
ACCOUNT_NOT_BOUND = "MARKETPLACE_ACCOUNT_NOT_BOUND"
TARGET_POLICY_MISSING = "REGISTER_TARGET_POLICY_MISSING"
PRICING_CONTEXT_MISMATCH = "REGISTER_PRICING_CONTEXT_MISMATCH"
ITEM_NOT_PRICED = "REGISTER_DRAFT_ITEM_NOT_PRICED"
SELECTION_MOVED = "REGISTER_DRAFT_SELECTION_MOVED"

Identifier = Annotated[StrictStr, Field(min_length=1, max_length=36)]


class CreateDraftRequest(BaseModel):
    """What the operator chose, and nothing an owner decides."""

    # Unknown fields are refused, so no server-owned value can ride along; every identifier is a
    # strict string, and the listing shape one of the existing ListingShape values.
    model_config = ConfigDict(extra="forbid")

    product_group_id: Identifier
    membership_revision_id: Identifier
    item_ids: list[Identifier] = Field(min_length=1, max_length=50)
    marketplace_key: Annotated[StrictStr, Field(min_length=1, max_length=40)]
    marketplace_account_id: Annotated[StrictStr, Field(min_length=1, max_length=40)]
    listing_shape: ListingShape
    actor: Annotated[StrictStr, Field(min_length=2, max_length=64)]


class DraftTargetView(BaseModel):
    """One canonical account a Draft may target, as its owners hold it now.

    ``unavailable_reason`` is the owners' verdict, not a hint: the command checks it again."""

    marketplace_key: str
    marketplace_account_id: str
    binding: AccountBinding
    policy_revision: str | None
    pricing_account_scoped: bool | None
    unavailable_reason: str | None


class DraftTargetsView(BaseModel):
    targets: list[DraftTargetView]
    listing_shapes: list[ListingShape]


class DraftItemPinView(BaseModel):
    """One Draft Item and the exact M4 snapshot pinned to it, with M4's own price and basis."""

    item_id: str
    pricing_snapshot_id: str
    pricing_outcome: PricingOutcome
    final_sale_price_krw: int
    price_basis: PriceBasis
    price_guard: PriceGuard


class DraftCreatedView(BaseModel):
    draft_id: str
    draft_revision: int
    marketplace_key: str
    marketplace_account_id: str
    listing_shape: ListingShape
    product_group_id: str
    membership_revision_id: str
    policy_revision: str
    items: list[DraftItemPinView]


class DraftCommandService:
    """The one owner of turning a Product DB selection into a RegistrationDraft."""

    def __init__(
        self,
        *,
        products: ProductsService,
        accounts: MarketplaceAccountStore,
        policies: RegistrationPolicySource,
        pricing: ProductPricingService,
        registrations: RegistrationStore,
        marketplaces: Sequence[str],
    ) -> None:
        self._products = products
        self._accounts = accounts
        self._policies = policies
        self._pricing = pricing
        self._registrations = registrations
        self._marketplaces = tuple(marketplaces)

    # ------------------------------------------------------------------ what may be targeted

    def targets(self) -> DraftTargetsView:
        """Every canonical account of every known marketplace, with its binding and current
        policy. Read-only; the command decides again."""
        targets = []
        for marketplace_key in self._marketplaces:
            for account in self._accounts.accounts(marketplace_key):
                account_id = account.marketplace_account_id
                binding = self._accounts.binding(marketplace_key, account_id)
                policy = self._policies.target(marketplace_key, account_id)
                reason = (
                    ACCOUNT_NOT_BOUND
                    if binding is not AccountBinding.BOUND
                    else TARGET_POLICY_MISSING
                    if policy is None
                    else None
                )
                targets.append(
                    DraftTargetView(
                        marketplace_key=marketplace_key,
                        marketplace_account_id=account_id,
                        binding=binding,
                        policy_revision=None if policy is None else policy.policy_revision,
                        pricing_account_scoped=None
                        if policy is None
                        else policy.pricing_context.account_id is not None,
                        unavailable_reason=reason,
                    )
                )
        return DraftTargetsView(targets=targets, listing_shapes=list(ListingShape))

    # ------------------------------------------------------------------ the command

    def create(self, request: CreateDraftRequest, *, correlation_id: str) -> DraftCreatedView:
        # 1. The selection, revalidated now: a stale one creates nothing (G1-C reasons).
        selection = self._products.registration_target(
            request.product_group_id, request.membership_revision_id, request.item_ids
        )
        # 2. The canonical target, and its current policy.
        policy = self._target(request.marketplace_key, request.marketplace_account_id)
        # 3. Every Item priced by M4, before anything opens.
        priced = self._price(selection, policy, correlation_id)
        # 4. One Draft with every pin, in one unit of work.
        with self._registrations.transaction() as unit:
            draft = unit.create_draft(
                request.marketplace_key,
                request.marketplace_account_id,
                request.listing_shape,
                created_by=request.actor,
                correlation_id=correlation_id,
            )
            for item in selection.items:
                draft = unit.add_draft_item(
                    draft.draft_id,
                    item.item_id,
                    priced[item.item_id][1].pricing_snapshot_id,
                    added_by=request.actor,
                    correlation_id=correlation_id,
                )
        return DraftCreatedView(
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            marketplace_key=request.marketplace_key,
            marketplace_account_id=request.marketplace_account_id,
            listing_shape=request.listing_shape,
            product_group_id=selection.product_group_id,
            membership_revision_id=selection.membership_revision_id,
            policy_revision=policy.policy_revision,
            items=[
                DraftItemPinView(
                    item_id=item.item_id,
                    pricing_snapshot_id=snapshot.pricing_snapshot_id,
                    pricing_outcome=outcome,
                    final_sale_price_krw=snapshot.final_sale_price_krw,
                    price_basis=snapshot.price_basis,
                    price_guard=snapshot.price_guard,
                )
                for item in selection.items
                for outcome, snapshot in (priced[item.item_id],)
            ],
        )

    def _target(self, marketplace_key: str, marketplace_account_id: str) -> TargetPolicy:
        account = self._accounts.account(marketplace_account_id)
        if account is None or account.marketplace_key != marketplace_key:
            raise NotFoundError(
                TARGET_ACCOUNT_UNKNOWN, "a Draft targets a canonical account of its marketplace"
            )
        binding = self._accounts.binding(marketplace_key, marketplace_account_id)
        if binding is not AccountBinding.BOUND:
            raise PolicyBlockedError(
                ACCOUNT_NOT_BOUND,
                "registration state is scoped only by a bound canonical marketplace account",
                details={"binding": binding.value},
            )
        policy = self._policies.target(marketplace_key, marketplace_account_id)
        if policy is None:
            raise RegistrationConflictError(
                TARGET_POLICY_MISSING, "the account has no current target policy"
            )
        context = policy.pricing_context
        if context.marketplace_key != marketplace_key or context.account_id not in (
            None,
            marketplace_account_id,
        ):
            # ADR-0015 §2: the policy prices for its own marketplace and account, or explicitly
            # for none. Anything else is refused, never repaired here.
            raise RegistrationConflictError(
                PRICING_CONTEXT_MISMATCH,
                "the target policy's pricing context is not this marketplace and account",
                details={"policy_revision": policy.policy_revision},
            )
        return policy

    def _price(
        self, selection: RegistrationTargetView, policy: TargetPolicy, correlation_id: str
    ) -> dict[str, tuple[PricingOutcome, PricingSnapshotRecord]]:
        """Every chosen Item through M4, in selection order. All are priced before any refusal,
        so the operator sees every Item M4 did not price, with every one of M4's reasons."""
        priced: dict[str, tuple[PricingOutcome, PricingSnapshotRecord]] = {}
        refused: dict[str, list[dict[str, str | None]]] = {}
        for item in selection.items:
            result = self._pricing.price(
                item.item_id, policy.pricing_context, correlation_id=correlation_id
            )
            if result.outcome is PricingOutcome.NOT_PRICED or result.snapshot is None:
                refused[item.item_id] = [
                    {"code": reason.code, "subject": reason.subject} for reason in result.reasons
                ]
            else:
                priced[item.item_id] = (result.outcome, result.snapshot)
        if refused:
            raise RegistrationConflictError(
                ITEM_NOT_PRICED,
                "M4 did not price every chosen Item for this target; no Draft was created",
                details={"items": refused, "policy_revision": policy.policy_revision},
            )
        moved = sorted(
            item.item_id
            for item in selection.items
            if not _prices_the_selection(priced[item.item_id][1], selection, item.binding_id)
        )
        if moved:
            raise RegistrationConflictError(
                SELECTION_MOVED,
                "the Product changed while it was priced; no Draft was created",
                details={"item_ids": moved},
            )
        return priced


def _prices_the_selection(
    snapshot: PricingSnapshotRecord, selection: RegistrationTargetView, binding_id: str
) -> bool:
    """Whether M4 priced exactly what the revalidated selection named: the same Product, its
    membership revision and the binding the Item held. An identity check only."""
    return (
        snapshot.product_group_id == selection.product_group_id
        and snapshot.membership_revision_id == selection.membership_revision_id
        and snapshot.source_binding_id == binding_id
    )
