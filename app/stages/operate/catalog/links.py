"""The ListingLinks owner (ADR-0042 S2a, §7): which common product each store listing is.

A store listing is never a product (SCP-01). A **link** says which ``ProductGroup`` — the common
product (공통상품) — a listing of the bound store is. Five kinds exist; at most one is ``ACTIVE``
per listing (SCP-09):

- ``REGISTERED_BY_ICBM``: the listing is an ``ACTIVE`` ICBM registration's product; its group is
  the registration's. Read from REGISTER, never stored here.
- ``ADOPTED``: an ``ACTIVE`` ADR-0024 adoption; its Item's group. Read from the adoption owner,
  never rewritten (ADR-0024 amendment, ADR-0042 §13).
- ``STRONG_KEY``: the server linked it **automatically** by an equal seller code or a valid equal
  GTIN (owner ``6190978708`` Q3), against an owner-declared seller-code convention of a member
  (ADR-0024 §2) or a listing of another store. Always with a notice and an undo (SCP-10).
- ``USER_CONFIRMED``: the seller's 「같은 상품」. Audited, reversible.
- ``IMPORTED``: the listing's own common product, created for it because no strong key linked it
  (owner Q4, SCP-12): a ``ProductGroup`` with **no** supplier member, shown 「도매처 미연결」.

**After each COMPLETE sync** (:meth:`ListingLinkService.after_sync`) every listing of the snapshot
that has no link is linked, in order: strong keys first, then a new common product for the rest,
so one product sold in two stores becomes one common product when a strong key joins them
(ADR-0042 §7). A strong key never links when it is ambiguous — shared by two listings of one store,
or matching more than one product — when the product already has a listing in the store (unless a
``DuplicateOverride`` covers it), or when the two sides state a different capacity, count or pack
(SCP-11): such a listing gets its own product and the match is offered to the seller instead.

**Proposals** (:meth:`ListingLinkService.overview`) are derived, never stored: a strong match that
was held back, and a similar name (a weak signal, SCP-10), for every listing that is still its own
product. The seller answers 「같은 상품」 (:meth:`ListingLinkService.confirm`, an ADR-0013 §4
MERGE where two products become one, the seller picking the survivor) or 「다른 상품」
(:meth:`ListingLinkService.reject`, revocable). Nothing links or merges on a weak signal.

**Unlink** ends a link and keeps the group, the listing and every observation. The listing gets
its own product again and the seller's 「다른 상품」 is recorded, so the next sync does not link it
back. Undoing an automatic link is exactly this.

Nothing here calls a marketplace (ADR-0042: S2a is read only towards every marketplace).
"""

import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, NotFoundError
from app.platform.db.database import Database
from app.stages.operate.catalog.facts import COMPLETE, OBSERVED
from app.stages.operate.catalog.keys import (
    SELLER_CODE,
    amounts,
    conflicts,
    name_similarity,
    name_tokens,
    seller_code_key,
    strong_keys,
)
from app.stages.operate.catalog.link_models import (
    StoreLinkNotice,
    StoreListingDecision,
    StoreListingLink,
)
from app.stages.operate.catalog.models import (
    CatalogFieldSet,
    CatalogListing,
    CatalogListingEnding,
    CatalogObservation,
)
from app.stages.operate.catalog.service import (
    ENDED,
    LISTED,
    CatalogListingView,
    CatalogRunView,
    StoreCatalogService,
)
from app.stages.products.model import GroupStatus
from app.stages.products.store import (
    GroupShape,
    MemberName,
    ProductFoundationStore,
    ProductFoundationUnit,
)

logger = logging.getLogger("icbm.operate.catalog.links")

# Link kinds (ADR-0042 §7). The first three are stored; the last two are read from their owners.
IMPORTED: Final = "IMPORTED"
STRONG_KEY: Final = "STRONG_KEY"
USER_CONFIRMED: Final = "USER_CONFIRMED"
REGISTERED_BY_ICBM: Final = "REGISTERED_BY_ICBM"
ADOPTED: Final = "ADOPTED"
DERIVED: Final = frozenset({REGISTERED_BY_ICBM, ADOPTED})

ACTIVE: Final = "ACTIVE"
LINK_ENDED: Final = "ENDED"
# Why a link ended.
UNDO: Final = "UNDO"
UNLINK: Final = "UNLINK"
MERGED: Final = "MERGED"

# The signal of a proposal: a held-back strong key, or a similar name.
NAME_SIMILAR: Final = "NAME_SIMILAR"
# ICBM policy: a name proposal needs this share of words in common and at least two words.
WEAK_THRESHOLD: Final = 0.6
WEAK_MIN_SHARED: Final = 2
MAX_PROPOSALS_PER_LISTING: Final = 3

ACTOR: Final = "operate.store-links"
# ICBM policy: how many listings one unit of work of the post-sync pass links.
CHUNK: Final = 200

LINK_UNKNOWN: Final = "STORE_LINK_UNKNOWN"
LISTING_UNKNOWN: Final = "STORE_LINK_LISTING_UNKNOWN"
DECISION_UNKNOWN: Final = "STORE_LINK_DECISION_UNKNOWN"
GROUP_NOT_ACTIVE: Final = "STORE_LINK_GROUP_NOT_ACTIVE"
LISTING_OWNED: Final = "STORE_LINK_LISTING_OWNED"
LISTING_LINKED_ELSEWHERE: Final = "STORE_LINK_LISTING_LINKED_ELSEWHERE"
GROUP_ALREADY_IN_STORE: Final = "STORE_LINK_GROUP_ALREADY_IN_STORE"
SURVIVOR_INVALID: Final = "STORE_LINK_SURVIVOR_INVALID"
OWN_PRODUCT: Final = "STORE_LINK_OWN_PRODUCT"
NOT_ACTIVE: Final = "STORE_LINK_NOT_ACTIVE"


class StoreLinkConflict(AppError):
    error_class = ErrorClass.CONFLICT


# ---------------------------------------------------------------- views


@dataclass(frozen=True)
class LinkView:
    """One listing of the bound store with the common product it is linked to now.

    ``link_id`` is set for a stored link only; ``unlinkable`` says whether the seller may end it
    here (an automatic or confirmed link, never the listing's own product nor a registration or
    adoption, which their owners hold)."""

    link_id: str | None
    kind: str
    key_kind: str | None
    listing_id: str
    marketplace_key: str
    marketplace_account_id: str
    origin_product_no: str
    channel_product_no: str | None
    product_group_id: str
    name: str | None
    sale_status: str | None
    sale_price: int | None
    stock_quantity: int | None
    listing_state: str
    linked_at: datetime | None
    unlinkable: bool


@dataclass(frozen=True)
class GroupLinks:
    """The common product side of the 통합DB: its listings, whether it has a supplier, and the
    seller's 「다른 상품」 decisions on its listings."""

    product_group_id: str
    listings: tuple[LinkView, ...]
    supplier_connected: bool


@dataclass(frozen=True)
class NoticeView:
    """An automatic link the seller has not acknowledged yet (「자동 연결됨 · 되돌리기」)."""

    notice_id: str
    link_id: str
    key_kind: str
    created_at: datetime
    listing: LinkView
    product_name: str | None


@dataclass(frozen=True)
class ProposalView:
    """「같은 상품인가요?」: one listing that is still its own product, and a common product it may
    be. ``signal`` is ``SELLER_CODE``/``GTIN`` for a strong key that was held back (``ambiguous``
    or ``conflicts`` say why) or ``NAME_SIMILAR``. ``survivors`` are the products the seller may
    keep when both become one; nothing happens until the seller answers."""

    listing: LinkView
    product_group_id: str
    product_name: str | None
    supplier_keys: tuple[str, ...]
    product_listings: tuple[LinkView, ...]
    signal: str
    similarity: float | None
    ambiguous: bool
    conflicts: tuple[str, ...]
    survivors: tuple[str, ...]


@dataclass(frozen=True)
class DecisionView:
    decision_id: str
    listing_id: str
    origin_product_no: str | None
    listing_name: str | None
    product_group_id: str
    product_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class LinksOverview:
    """What the 통합DB shows above the product list after an import (ADR-0042 §0 b3)."""

    marketplace_key: str
    marketplace_account_id: str | None
    as_of: datetime | None
    linked: int
    unconnected: int
    notices: tuple[NoticeView, ...]
    proposals: tuple[ProposalView, ...]
    decisions: tuple[DecisionView, ...]


@dataclass(frozen=True)
class PassResult:
    created: int
    linked: int
    superseded: int


@dataclass(frozen=True)
class LinkResult:
    """What one seller action did: the listing's link now, and the product a merge retired."""

    link: LinkView | None
    retired_group_id: str | None = None
    decision_id: str | None = None


# ---------------------------------------------------------------- internal values


@dataclass(frozen=True)
class _Listing:
    view: CatalogListingView
    keys: frozenset[tuple[str, str]]
    name: str | None
    texts: tuple[str | None, ...]


@dataclass(frozen=True)
class _Link:
    kind: str
    group_id: str
    link_id: str | None
    key_kind: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class _Current:
    stored: _Link | None
    derived: _Link | None

    @property
    def effective(self) -> _Link | None:
        return self.derived or self.stored


# A linked listing of another store: (listing, origin product, group, name, strong keys, texts).
_Other = tuple[str, str, str, str | None, frozenset[tuple[str, str]], tuple[str | None, ...]]


def _name(fields: Mapping[str, Any] | None) -> str | None:
    value = None if fields is None else fields.get("name")
    return value if isinstance(value, str) and value else None


def _option_texts(fields: Mapping[str, Any] | None) -> list[str | None]:
    options = None if fields is None else fields.get("options")
    if not isinstance(options, Mapping):
        return []
    texts: list[str | None] = []
    for combination in options.get("combinations") or ():
        if isinstance(combination, Mapping):
            texts.extend(n for n in combination.get("names") or () if isinstance(n, str))
    return texts


def _listing(view: CatalogListingView) -> _Listing:
    name = _name(view.fields)
    return _Listing(
        view=view,
        keys=strong_keys(
            (view.seller_management_code, view.listed_seller_code), (view.seller_barcode,)
        ),
        name=name,
        texts=(name, *_option_texts(view.fields)),
    )


def _stored(row: StoreListingLink) -> _Link:
    return _Link(row.kind, row.product_group_id, row.link_id, row.key_kind, row.created_at)


class ListingLinkService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        catalog: StoreCatalogService,
        products: ProductFoundationStore,
        registered: Callable[[str, str], Mapping[str, str]],
        adopted: Callable[[str], Mapping[str, str]],
        overridden: Callable[[str, str, str], bool],
        conventions: Mapping[str, str],
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._catalog = catalog
        self._products = products
        # origin product → group of the ACTIVE ICBM registrations of one store (REGISTER's).
        self._registered = registered
        # origin product → group of the ACTIVE adoptions of one marketplace (ADR-0024's).
        self._adopted = adopted
        # Whether an active DuplicateOverride covers a group in a store (ADR-0014 §13).
        self._overridden = overridden
        # ADR-0024 §2: the owner-declared seller-code convention of each supplier.
        self._conventions = dict(conventions)
        # One writer at a time: the post-sync pass and the seller's actions.
        self._lock = threading.RLock()

    @property
    def marketplace_key(self) -> str:
        return self._catalog.marketplace_key

    # ------------------------------------------------------------------ reads of the owners

    def _store(self) -> tuple[str, str] | None:
        account = self._catalog.account()
        return None if account is None else (self.marketplace_key, account)

    def _derived(self, marketplace_key: str, account: str) -> dict[str, _Link]:
        """origin product → the link its registration or adoption owner holds."""
        found: dict[str, _Link] = {}
        for origin, group in self._adopted(marketplace_key).items():
            found[origin] = _Link(ADOPTED, group, None, None, None)
        for origin, group in self._registered(marketplace_key, account).items():
            found[origin] = _Link(REGISTERED_BY_ICBM, group, None, None, None)
        return found

    @staticmethod
    def _stored_links(
        session: Session, listing_ids: Iterable[str] | None = None
    ) -> dict[str, StoreListingLink]:
        query = select(StoreListingLink).where(StoreListingLink.state == ACTIVE)
        if listing_ids is not None:
            query = query.where(StoreListingLink.listing_id.in_(sorted(set(listing_ids))))
        return {row.listing_id: row for row in session.scalars(query)}

    def _current(
        self, session: Session, views: Sequence[CatalogListingView], store: tuple[str, str]
    ) -> dict[str, _Current]:
        stored = self._stored_links(session, (view.listing_id for view in views))
        derived = self._derived(*store)
        return {
            view.listing_id: _Current(
                None if view.listing_id not in stored else _stored(stored[view.listing_id]),
                derived.get(view.origin_product_no),
            )
            for view in views
        }

    @staticmethod
    def _decisions(session: Session) -> set[tuple[str, str]]:
        return {
            (listing, group)
            for listing, group in session.execute(
                select(
                    StoreListingDecision.listing_id, StoreListingDecision.product_group_id
                ).where(StoreListingDecision.revoked_at.is_(None))
            ).tuples()
        }

    def _member_codes(
        self, unit: ProductFoundationUnit
    ) -> tuple[dict[str, set[str]], dict[str, tuple[MemberName, ...]]]:
        """Seller code → the ACTIVE groups whose member's convention gives it, and each group's
        member names (for conflicts and proposals)."""
        names = unit.member_names()
        codes: dict[str, set[str]] = {}
        for group, members in names.items():
            for member in members:
                convention = self._conventions.get(member.supplier_key)
                if convention is None:
                    continue
                code = seller_code_key(
                    convention.format(source_product_id=member.source_product_id)
                )
                if code is not None:
                    codes.setdefault(code, set()).add(group)
        return codes, names

    @staticmethod
    def _other_stores(session: Session, store: tuple[str, str]) -> list[_Other]:
        """The linked listings of every **other** store: (listing, origin, group, name, keys,
        texts), from each one's latest successful read. Empty while one store is connected."""
        rows = session.execute(
            select(StoreListingLink.listing_id, StoreListingLink.product_group_id, CatalogListing)
            .join(CatalogListing, CatalogListing.listing_id == StoreListingLink.listing_id)
            .where(
                StoreListingLink.state == ACTIVE,
                (CatalogListing.marketplace_key != store[0])
                | (CatalogListing.marketplace_account_id != store[1]),
            )
        ).tuples()
        found: list[_Other] = []
        for listing_id, group, listing in rows:
            observation = session.scalars(
                select(CatalogObservation)
                .where(
                    CatalogObservation.listing_id == listing_id,
                    CatalogObservation.result == OBSERVED,
                )
                .order_by(CatalogObservation.observed_at.desc())
                .limit(1)
            ).first()
            fields = None
            if observation is not None and observation.field_set_digest is not None:
                content = session.get(CatalogFieldSet, observation.field_set_digest)
                fields = None if content is None else json.loads(content.fields_json)
            keys = (
                frozenset()
                if observation is None
                else strong_keys(
                    (observation.seller_management_code, observation.listed_seller_code),
                    (observation.seller_barcode,),
                )
            )
            name = _name(fields)
            found.append(
                (
                    listing_id,
                    listing.origin_product_no,
                    group,
                    name,
                    keys,
                    (name, *_option_texts(fields)),
                )
            )
        return found

    # ------------------------------------------------------------------ the post-sync pass

    def after_sync(self, run: CatalogRunView) -> None:
        """The StoreCatalog listener: link the listings of a COMPLETE sync. A failure here never
        changes the sync's own outcome; the next COMPLETE sync tries again."""
        if run.outcome != COMPLETE:
            return
        try:
            self.reconcile(correlation_id=f"store-links-{run.run_id}")
        except Exception:
            logger.exception("operate.store_links_pass_failed", extra={"run_id": run.run_id})

    def reconcile(self, *, correlation_id: str) -> PassResult:
        """Link every unlinked listing of the last COMPLETE snapshot: strong keys first, then one
        new common product per remaining listing (ADR-0042 §7, SCP-10 to SCP-12)."""
        with self._lock:
            store = self._store()
            snapshot = self._catalog.snapshot()
            if store is None or snapshot.run is None:
                return PassResult(0, 0, 0)
            views = self._catalog.listings()
            listed = sorted(
                (_listing(view) for view in snapshot.listings),
                key=lambda listing: listing.view.origin_product_no,
            )
            with self._products.reading() as unit:
                current = self._current(unit.session, views, store)
                decisions = self._decisions(unit.session)
                codes, names = self._member_codes(unit)
                others = self._other_stores(unit.session, store)
            other_keys: dict[tuple[str, str], set[str]] = {}
            group_texts: dict[str, list[str | None]] = {
                group: [member.name for member in members] for group, members in names.items()
            }
            for _, _, group, _, keys, texts in others:
                for key in keys:
                    other_keys.setdefault(key, set()).add(group)
                group_texts.setdefault(group, []).extend(texts)
            shared = Counter(key for listing in listed for key in listing.keys)
            ended = {view.listing_id for view in views if view.state == ENDED}
            in_store = Counter(
                link.group_id
                for listing_id, now in current.items()
                if listing_id not in ended and (link := now.effective) is not None
            )
            created = linked = superseded = 0
            for start in range(0, len(listed), CHUNK):
                with self._products.transaction() as unit:
                    for listing in listed[start : start + CHUNK]:
                        now = current.get(listing.view.listing_id, _Current(None, None))
                        if now.derived is not None:
                            superseded += self._supersede(unit, listing, now, correlation_id)
                            continue
                        if now.stored is not None:
                            continue
                        target = self._automatic(
                            store,
                            listing,
                            shared,
                            codes,
                            other_keys,
                            decisions,
                            in_store,
                            group_texts,
                        )
                        if target is not None:
                            group, key_kind = target
                            self._add_link(
                                unit,
                                listing.view.listing_id,
                                store,
                                group,
                                STRONG_KEY,
                                key_kind=key_kind,
                                decided_by=ACTOR,
                                correlation_id=correlation_id,
                                notice=True,
                            )
                            in_store[group] += 1
                            linked += 1
                            continue
                        group = unit.create_group(decided_by=ACTOR)
                        self._add_link(
                            unit,
                            listing.view.listing_id,
                            store,
                            group,
                            IMPORTED,
                            decided_by=ACTOR,
                            correlation_id=correlation_id,
                        )
                        in_store[group] += 1
                        group_texts[group] = list(listing.texts)
                        created += 1
            result = PassResult(created, linked, superseded)
            if created or linked or superseded:
                self._audit.append(
                    AuditEntry(
                        event_type=AuditEventType.STORE_LISTINGS_LINKED,
                        action="operate.store_links.pass",
                        actor=ACTOR,
                        outcome=AuditOutcome.RECORDED,
                        target_ref=snapshot.run.run_id,
                        after={
                            "marketplace_key": store[0],
                            "marketplace_account_id": store[1],
                            "created": created,
                            "linked": linked,
                            "superseded": superseded,
                        },
                        correlation_id=correlation_id,
                    )
                )
            logger.info(
                "operate.store_links_pass",
                extra={"created": created, "linked": linked, "superseded": superseded},
            )
            return result

    def _strong_targets(
        self,
        listing: _Listing,
        codes: Mapping[str, set[str]],
        other_keys: Mapping[tuple[str, str], set[str]],
        decisions: set[tuple[str, str]],
    ) -> dict[str, str]:
        """group → the key kind that matches it, without the seller's 「다른 상품」."""
        found: dict[str, str] = {}
        for kind, value in sorted(listing.keys):
            groups = set(other_keys.get((kind, value), set()))
            if kind == SELLER_CODE:
                groups |= codes.get(value, set())
            for group in sorted(groups):
                if (listing.view.listing_id, group) not in decisions:
                    found.setdefault(group, kind)
        return found

    def _automatic(
        self,
        store: tuple[str, str],
        listing: _Listing,
        shared: Counter[tuple[str, str]],
        codes: Mapping[str, set[str]],
        other_keys: Mapping[tuple[str, str], set[str]],
        decisions: set[tuple[str, str]],
        in_store: Counter[str],
        group_texts: Mapping[str, Sequence[str | None]],
    ) -> tuple[str, str] | None:
        """The one product a strong key links automatically, or ``None`` (SCP-10, SCP-11)."""
        targets = self._strong_targets(listing, codes, other_keys, decisions)
        if len(targets) != 1 or any(shared[key] > 1 for key in listing.keys):
            return None
        ((group, kind),) = targets.items()
        if in_store[group] and not self._overridden(store[0], store[1], group):
            return None
        if conflicts(amounts(listing.texts), amounts(group_texts.get(group, ()))):
            return None
        return group, kind

    def _supersede(
        self, unit: ProductFoundationUnit, listing: _Listing, now: _Current, correlation_id: str
    ) -> int:
        """A listing its registration or adoption owner now holds keeps only that link: a stored
        link to the same product ends, and the listing's own empty product merges into it."""
        stored, derived = now.stored, now.derived
        if stored is None or derived is None or stored.link_id is None:
            return 0
        if stored.group_id != derived.group_id:
            shape = unit.group_shape(stored.group_id)
            if (
                stored.kind != IMPORTED
                or shape is None
                or shape.confirmed_members
                or shape.items
                or self._other_links(unit.session, stored.group_id, stored.link_id)
            ):
                # Not an empty product of its own: the seller decides (shown as two links).
                return 0
        self._end(unit.session, stored.link_id, MERGED, ACTOR)
        if stored.group_id != derived.group_id:
            unit.retire_into(
                stored.group_id, derived.group_id, decided_by=ACTOR, correlation_id=correlation_id
            )
        return 1

    # ------------------------------------------------------------------ rows

    def _add_link(
        self,
        unit: ProductFoundationUnit,
        listing_id: str,
        store: tuple[str, str],
        group: str,
        kind: str,
        *,
        decided_by: str,
        correlation_id: str,
        key_kind: str | None = None,
        notice: bool = False,
    ) -> StoreListingLink:
        now = self._clock.now()
        row = StoreListingLink(
            link_id=str(uuid.uuid4()),
            listing_id=listing_id,
            marketplace_key=store[0],
            marketplace_account_id=store[1],
            product_group_id=group,
            kind=kind,
            key_kind=key_kind,
            state=ACTIVE,
            decided_by=decided_by,
            correlation_id=correlation_id,
            created_at=now,
            ended_at=None,
            ended_by=None,
            end_reason=None,
        )
        unit.session.add(row)
        unit.session.flush()
        if notice:
            unit.session.add(
                StoreLinkNotice(
                    notice_id=str(uuid.uuid4()),
                    link_id=row.link_id,
                    created_at=now,
                    acknowledged_at=None,
                    acknowledged_by=None,
                )
            )
            unit.session.flush()
        return row

    def _end(self, session: Session, link_id: str, reason: str, actor: str) -> StoreListingLink:
        row = session.get(StoreListingLink, link_id)
        if row is None:
            raise NotFoundError(LINK_UNKNOWN, "no store listing link has that id")
        if row.state != ACTIVE:
            raise StoreLinkConflict(NOT_ACTIVE, "this link has already ended")
        row.state, row.ended_at, row.ended_by, row.end_reason = (
            LINK_ENDED,
            self._clock.now(),
            actor,
            reason,
        )
        session.flush()
        return row

    @staticmethod
    def _other_links(session: Session, group: str, except_link: str | None) -> int:
        query = (
            select(func.count())
            .select_from(StoreListingLink)
            .where(StoreListingLink.product_group_id == group, StoreListingLink.state == ACTIVE)
        )
        if except_link is not None:
            query = query.where(StoreListingLink.link_id != except_link)
        return int(session.scalar(query) or 0)

    def _record_decision(
        self, session: Session, listing_id: str, group: str, actor: str, correlation_id: str
    ) -> str:
        existing = session.scalars(
            select(StoreListingDecision).where(
                StoreListingDecision.listing_id == listing_id,
                StoreListingDecision.product_group_id == group,
                StoreListingDecision.revoked_at.is_(None),
            )
        ).first()
        if existing is not None:
            return existing.decision_id
        decision_id = str(uuid.uuid4())
        session.add(
            StoreListingDecision(
                decision_id=decision_id,
                listing_id=listing_id,
                product_group_id=group,
                decided_by=actor,
                correlation_id=correlation_id,
                created_at=self._clock.now(),
                revoked_at=None,
                revoked_by=None,
            )
        )
        session.flush()
        return decision_id

    def _revoke_decisions(self, session: Session, listing_id: str, group: str, actor: str) -> None:
        for row in session.scalars(
            select(StoreListingDecision).where(
                StoreListingDecision.listing_id == listing_id,
                StoreListingDecision.product_group_id == group,
                StoreListingDecision.revoked_at.is_(None),
            )
        ):
            row.revoked_at, row.revoked_by = self._clock.now(), actor
        session.flush()

    def _audit_action(
        self,
        session: Session,
        event: AuditEventType,
        action: str,
        actor: str,
        target: str,
        after: Mapping[str, Any],
        correlation_id: str,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=event,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=target,
                after=dict(after),
                correlation_id=correlation_id,
            ),
            session=session,
        )

    # ------------------------------------------------------------------ the seller's actions

    def _listing_row(self, session: Session, listing_id: str) -> CatalogListing:
        row = session.get(CatalogListing, listing_id)
        if row is None:
            raise NotFoundError(LISTING_UNKNOWN, "no store listing has that id")
        return row

    def listing_id_of(
        self, marketplace_key: str, marketplace_account_id: str, origin_product_no: str
    ) -> str:
        """The store listing a duplicate row names by its product number."""
        with self._db.read() as session:
            found = session.scalar(
                select(CatalogListing.listing_id).where(
                    CatalogListing.marketplace_key == marketplace_key,
                    CatalogListing.marketplace_account_id == marketplace_account_id,
                    CatalogListing.origin_product_no == origin_product_no,
                )
            )
        if found is None:
            raise NotFoundError(LISTING_UNKNOWN, "no store listing has that product number")
        return str(found)

    def _in_store(
        self,
        session: Session,
        store: tuple[str, str],
        group: str,
        *,
        excluding: Iterable[str] = (),
    ) -> int:
        """How many live listings of the store are linked to the group now (stored or read)."""
        skip = set(excluding)
        ended = set(session.scalars(select(CatalogListingEnding.listing_id)))
        count = 0
        for row in session.scalars(
            select(StoreListingLink).where(
                StoreListingLink.product_group_id == group,
                StoreListingLink.state == ACTIVE,
                StoreListingLink.marketplace_key == store[0],
                StoreListingLink.marketplace_account_id == store[1],
            )
        ):
            if row.listing_id not in skip and row.listing_id not in ended:
                count += 1
        derived = self._derived(*store)
        count += sum(1 for link in derived.values() if link.group_id == group)
        return count

    def _require_store_room(
        self, session: Session, store: tuple[str, str], group: str, *, excluding: Iterable[str]
    ) -> None:
        """SCP-09: one listing per product and store, unless a DuplicateOverride covers it."""
        if self._in_store(session, store, group, excluding=excluding) and not self._overridden(
            store[0], store[1], group
        ):
            raise StoreLinkConflict(
                GROUP_ALREADY_IN_STORE,
                "this product already has a listing in this store",
                details={"product_group_id": group},
            )

    def confirm(
        self,
        listing_id: str,
        product_group_id: str,
        *,
        survivor_group_id: str | None = None,
        actor: str,
        correlation_id: str,
    ) -> LinkResult:
        """「같은 상품」: this listing is that common product (``USER_CONFIRMED``).

        When the listing is still its own empty product, the two become one — an ADR-0013 §4
        MERGE with a ``GroupChangeEvent``. The seller picks the survivor: the chosen product
        (default) or, when the chosen one is itself an empty product of other listings, the
        listing's own. A product holding a supplier member or an Item is never retired here.
        """
        with self._lock, self._products.transaction() as unit:
            session = unit.session
            listing = self._listing_row(session, listing_id)
            store = (listing.marketplace_key, listing.marketplace_account_id)
            target = unit.group_shape(product_group_id)
            if target is None or target.status is not GroupStatus.ACTIVE:
                raise StoreLinkConflict(GROUP_NOT_ACTIVE, "that product is not active")
            derived = self._derived(*store).get(listing.origin_product_no)
            if derived is not None:
                if derived.group_id == product_group_id:
                    return LinkResult(self._view_of(session, listing_id))
                raise StoreLinkConflict(
                    LISTING_OWNED, "this listing is held by its registration or adoption"
                )
            current = self._stored_links(session, (listing_id,)).get(listing_id)
            if current is not None and current.product_group_id == product_group_id:
                return LinkResult(self._view_of(session, listing_id))
            self._revoke_decisions(session, listing_id, product_group_id, actor)
            survivor = survivor_group_id or product_group_id
            retired: str | None = None
            if current is None:
                if survivor != product_group_id:
                    raise StoreLinkConflict(SURVIVOR_INVALID, "only the chosen product survives")
                self._require_store_room(session, store, product_group_id, excluding=())
                self._add_link(
                    unit,
                    listing_id,
                    store,
                    product_group_id,
                    USER_CONFIRMED,
                    decided_by=actor,
                    correlation_id=correlation_id,
                )
            else:
                own = current.product_group_id
                own_shape = unit.group_shape(own)
                assert own_shape is not None
                own_others = self._other_links(session, own, current.link_id)
                if current.kind != IMPORTED or own_shape.confirmed_members or own_shape.items:
                    raise StoreLinkConflict(
                        LISTING_LINKED_ELSEWHERE,
                        "this listing is linked to another product; unlink it first",
                    )
                if survivor == product_group_id:
                    self._require_store_room(
                        session, store, product_group_id, excluding=(listing_id,)
                    )
                    # The listing's own product merges only when nothing else is linked to it.
                    self._end(session, current.link_id, UNLINK if own_others else MERGED, actor)
                    self._add_link(
                        unit,
                        listing_id,
                        store,
                        product_group_id,
                        USER_CONFIRMED,
                        decided_by=actor,
                        correlation_id=correlation_id,
                    )
                    if not own_others:
                        unit.retire_into(
                            own, product_group_id, decided_by=actor, correlation_id=correlation_id
                        )
                        retired = own
                elif survivor == own and not own_others:
                    if (
                        target.confirmed_members
                        or target.items
                        or any(
                            link.group_id == product_group_id
                            for link in self._derived(*store).values()
                        )
                    ):
                        raise StoreLinkConflict(
                            SURVIVOR_INVALID,
                            "a product with a supplier, an Item or a registration survives",
                        )
                    moving = list(
                        session.scalars(
                            select(StoreListingLink).where(
                                StoreListingLink.product_group_id == product_group_id,
                                StoreListingLink.state == ACTIVE,
                            )
                        )
                    )
                    for row in moving:
                        moved_store = (row.marketplace_key, row.marketplace_account_id)
                        self._require_store_room(session, moved_store, own, excluding=())
                    for row in moving:
                        self._end(session, row.link_id, MERGED, actor)
                        self._add_link(
                            unit,
                            row.listing_id,
                            (row.marketplace_key, row.marketplace_account_id),
                            own,
                            USER_CONFIRMED,
                            decided_by=actor,
                            correlation_id=correlation_id,
                        )
                    unit.retire_into(
                        product_group_id, own, decided_by=actor, correlation_id=correlation_id
                    )
                    retired = product_group_id
                else:
                    raise StoreLinkConflict(SURVIVOR_INVALID, "that product cannot survive here")
            self._audit_action(
                session,
                AuditEventType.STORE_LISTING_LINK_CONFIRMED,
                "operate.store_links.confirm",
                actor,
                listing_id,
                {
                    "origin_product_no": listing.origin_product_no,
                    "product_group_id": product_group_id,
                    "survivor_group_id": survivor,
                    "retired_group_id": retired,
                },
                correlation_id,
            )
            return LinkResult(self._view_of(session, listing_id), retired)

    def reject(
        self, listing_id: str, product_group_id: str, *, actor: str, correlation_id: str
    ) -> LinkResult:
        """「다른 상품」: this listing is not that product. Revocable; links nothing."""
        with self._lock, self._products.transaction() as unit:
            session = unit.session
            listing = self._listing_row(session, listing_id)
            if unit.group_shape(product_group_id) is None:
                raise NotFoundError("PRODUCTS_PRODUCT_UNKNOWN", "no product has that identifier")
            decision = self._record_decision(
                session, listing_id, product_group_id, actor, correlation_id
            )
            self._audit_action(
                session,
                AuditEventType.STORE_LISTING_DECISION_RECORDED,
                "operate.store_links.reject",
                actor,
                decision,
                {
                    "origin_product_no": listing.origin_product_no,
                    "product_group_id": product_group_id,
                },
                correlation_id,
            )
            return LinkResult(self._view_of(session, listing_id), decision_id=decision)

    def revoke_decision(self, decision_id: str, *, actor: str, correlation_id: str) -> None:
        """Undo 「다른 상품」: the pair may be proposed again."""
        with self._lock, self._db.write() as session:
            row = session.get(StoreListingDecision, decision_id)
            if row is None or row.revoked_at is not None:
                raise NotFoundError(DECISION_UNKNOWN, "no active decision has that id")
            row.revoked_at, row.revoked_by = self._clock.now(), actor
            session.flush()
            self._audit_action(
                session,
                AuditEventType.STORE_LISTING_DECISION_RECORDED,
                "operate.store_links.revoke_decision",
                actor,
                decision_id,
                {"product_group_id": row.product_group_id, "revoked": True},
                correlation_id,
            )

    def unlink(
        self, link_id: str, *, actor: str, correlation_id: str, undo: bool = False
    ) -> LinkResult:
        """End an automatic or confirmed link (「되돌리기」 / 「연결 해제」).

        The group, the listing and every observation stay. The listing gets its own common product
        again, and 「다른 상품」 is recorded for the pair so the next sync does not link it back.
        A listing's own product, a registration and an adoption are not unlinked here.
        """
        with self._lock, self._products.transaction() as unit:
            session = unit.session
            row = session.get(StoreListingLink, link_id)
            if row is None:
                raise NotFoundError(LINK_UNKNOWN, "no store listing link has that id")
            if row.state != ACTIVE:
                raise StoreLinkConflict(NOT_ACTIVE, "this link has already ended")
            if row.kind == IMPORTED:
                raise StoreLinkConflict(OWN_PRODUCT, "this is the listing's own product")
            shape = unit.group_shape(row.product_group_id)
            if (
                shape is not None
                and not shape.confirmed_members
                and not shape.items
                and not self._other_links(session, row.product_group_id, row.link_id)
            ):
                raise StoreLinkConflict(OWN_PRODUCT, "this is the listing's only product")
            group, listing_id = row.product_group_id, row.listing_id
            store = (row.marketplace_key, row.marketplace_account_id)
            self._end(session, link_id, UNDO if undo else UNLINK, actor)
            decision = self._record_decision(session, listing_id, group, actor, correlation_id)
            own = unit.create_group(decided_by=actor)
            self._add_link(
                unit,
                listing_id,
                store,
                own,
                IMPORTED,
                decided_by=actor,
                correlation_id=correlation_id,
            )
            self._audit_action(
                session,
                AuditEventType.STORE_LISTING_UNLINKED,
                "operate.store_links.undo" if undo else "operate.store_links.unlink",
                actor,
                link_id,
                {"product_group_id": group, "own_group_id": own, "kind": row.kind},
                correlation_id,
            )
            return LinkResult(self._view_of(session, listing_id), decision_id=decision)

    def acknowledge(
        self, notice_ids: Iterable[str] | None, *, actor: str, correlation_id: str
    ) -> int:
        """「확인」: the seller saw these automatic links (all open notices when ``None``)."""
        with self._lock, self._db.write() as session:
            query = select(StoreLinkNotice).where(StoreLinkNotice.acknowledged_at.is_(None))
            if notice_ids is not None:
                query = query.where(StoreLinkNotice.notice_id.in_(sorted(set(notice_ids))))
            rows = list(session.scalars(query))
            now = self._clock.now()
            for row in rows:
                row.acknowledged_at, row.acknowledged_by = now, actor
            session.flush()
            if rows:
                self._audit_action(
                    session,
                    AuditEventType.STORE_LISTING_LINK_CONFIRMED,
                    "operate.store_links.acknowledge",
                    actor,
                    rows[0].notice_id,
                    {"acknowledged": len(rows)},
                    correlation_id,
                )
            return len(rows)

    # ------------------------------------------------------------------ views

    def _views(self) -> tuple[tuple[str, str] | None, tuple[CatalogListingView, ...]]:
        store = self._store()
        return store, (() if store is None else self._catalog.listings())

    def _link_view(
        self, view: CatalogListingView, link: _Link, store: tuple[str, str], session: Session
    ) -> LinkView:
        unlinkable = False
        if link.link_id is not None and link.kind in (STRONG_KEY, USER_CONFIRMED):
            unlinkable = True
        return LinkView(
            link_id=link.link_id,
            kind=link.kind,
            key_kind=link.key_kind,
            listing_id=view.listing_id,
            marketplace_key=store[0],
            marketplace_account_id=store[1],
            origin_product_no=view.origin_product_no,
            channel_product_no=view.channel_product_no,
            product_group_id=link.group_id,
            name=_name(view.fields),
            sale_status=view.sale_status,
            sale_price=view.sale_price,
            stock_quantity=view.stock_quantity,
            listing_state=view.state,
            linked_at=link.created_at,
            unlinkable=unlinkable,
        )

    def _view_of(self, session: Session, listing_id: str) -> LinkView | None:
        store = self._store()
        views = self._catalog.listing_views((listing_id,))
        if store is None or not views:
            return None
        link = self._current(session, views, store)[listing_id].effective
        return None if link is None else self._link_view(views[0], link, store, session)

    def _store_links(
        self, session: Session, store: tuple[str, str], groups: Iterable[str] | None = None
    ) -> dict[str, str]:
        """listing → its common product, for every link of the store (stored or read), without
        reading any listing's details."""
        wanted = None if groups is None else set(groups)
        query = select(StoreListingLink.listing_id, StoreListingLink.product_group_id).where(
            StoreListingLink.state == ACTIVE,
            StoreListingLink.marketplace_key == store[0],
            StoreListingLink.marketplace_account_id == store[1],
        )
        if wanted is not None:
            query = query.where(StoreListingLink.product_group_id.in_(sorted(wanted)))
        found = {str(listing): str(group) for listing, group in session.execute(query).tuples()}
        derived = self._derived(*store)
        if derived:
            origins = {
                origin: link.group_id
                for origin, link in derived.items()
                if wanted is None or link.group_id in wanted
            }
            for listing_id, origin in session.execute(
                select(CatalogListing.listing_id, CatalogListing.origin_product_no).where(
                    CatalogListing.marketplace_key == store[0],
                    CatalogListing.marketplace_account_id == store[1],
                    CatalogListing.origin_product_no.in_(sorted(origins)),
                )
            ).tuples():
                found[str(listing_id)] = origins[str(origin)]
        return found

    def _linked(self) -> list[LinkView]:
        store, views = self._views()
        if store is None:
            return []
        with self._db.read() as session:
            current = self._current(session, views, store)
            return [
                self._link_view(view, link, store, session)
                for view in views
                if (link := current[view.listing_id].effective) is not None
            ]

    def for_groups(self, product_group_ids: Iterable[str]) -> tuple[GroupLinks, ...]:
        """The 통합DB rows' store side: each product's listings in the bound store, and whether
        it has a supplier (「도매처 미연결」 when it has none)."""
        wanted = list(dict.fromkeys(product_group_ids))
        if not wanted:
            return ()
        store = self._store()
        by_group: dict[str, list[LinkView]] = {}
        with self._products.reading() as unit:
            shapes = unit.group_shapes(wanted)
            if store is not None:
                listed = self._store_links(unit.session, store, wanted)
                views = self._catalog.listing_views(listed)
                current = self._current(unit.session, views, store)
                for view in views:
                    link = current[view.listing_id].effective
                    if link is not None and link.group_id in wanted:
                        by_group.setdefault(link.group_id, []).append(
                            self._link_view(view, link, store, unit.session)
                        )
        return tuple(
            GroupLinks(
                product_group_id=group,
                listings=tuple(by_group.get(group, ())),
                supplier_connected=bool(
                    (shape := shapes.get(group)) is not None and shape.confirmed_members
                ),
            )
            for group in wanted
        )

    def search(self, text: str) -> set[str]:
        """The products whose linked listing's name contains the search (통합DB search)."""
        needle = text.strip().casefold()
        if not needle:
            return set()
        return {
            link.product_group_id
            for link in self._linked()
            if link.name is not None and needle in link.name.casefold()
        }

    def linked_groups(self, marketplace_key: str, marketplace_account_id: str) -> dict[str, str]:
        """origin product → its common product, for every link of the store (the catalog
        duplicate source, ADR-0042 §6: a listing linked to the unit's product is a duplicate)."""
        store = self._store()
        if store is None or store != (marketplace_key, marketplace_account_id):
            return {}
        with self._db.read() as session:
            listed = self._store_links(session, store)
            origins = {
                str(listing_id): str(origin)
                for listing_id, origin in session.execute(
                    select(CatalogListing.listing_id, CatalogListing.origin_product_no).where(
                        CatalogListing.listing_id.in_(sorted(listed))
                    )
                ).tuples()
            }
        return {str(origins[lid]): group for lid, group in listed.items() if lid in origins}

    def overview(self) -> LinksOverview:
        """What the 통합DB shows above its list (ADR-0042 §0 b3): the automatic links not yet
        acknowledged, the proposals still open, and the 「다른 상품」 decisions."""
        store = self._store()
        status = self._catalog.status()
        as_of = None if status.last_complete is None else status.last_complete.as_of
        if store is None:
            return LinksOverview(self.marketplace_key, None, as_of, 0, 0, (), (), ())
        with self._products.reading() as unit:
            session = unit.session
            listed = self._store_links(session, store)
            codes, names = self._member_codes(unit)
            others = self._other_stores(session, store)
            decisions = self._decisions(session)
            open_notices = list(
                session.execute(
                    select(StoreLinkNotice, StoreListingLink)
                    .join(StoreListingLink, StoreListingLink.link_id == StoreLinkNotice.link_id)
                    .where(
                        StoreLinkNotice.acknowledged_at.is_(None),
                        StoreListingLink.state == ACTIVE,
                    )
                    .order_by(StoreLinkNotice.created_at, StoreLinkNotice.notice_id)
                ).tuples()
            )
            decided = list(
                session.scalars(
                    select(StoreListingDecision)
                    .where(StoreListingDecision.revoked_at.is_(None))
                    .order_by(
                        StoreListingDecision.created_at.desc(), StoreListingDecision.decision_id
                    )
                )
            )
            # A proposal needs every listing's details, but only while there is a product with a
            # supplier, or a listing of another store, to propose; otherwise only the few listings
            # a notice or a decision names are read.
            proposing = bool(names or others)
            if proposing:
                views = self._catalog.listings()
            else:
                views = self._catalog.listing_views(
                    {row.listing_id for _notice, row in open_notices}
                    | {row.listing_id for row in decided}
                )
            current = self._current(session, views, store)
            links = {
                view.listing_id: self._link_view(view, link, store, session)
                for view in views
                if (link := current[view.listing_id].effective) is not None
            }
            shapes = unit.group_shapes(
                {*listed.values(), *names, *(group for _l, _o, group, *_ in others)}
            )
            group_names = self._group_names(names, links.values())
            notices = self._notices(open_notices, links, group_names)
            proposals = (
                self._proposals(
                    store,
                    views,
                    current,
                    links,
                    codes,
                    names,
                    others,
                    decisions,
                    group_names,
                    shapes,
                )
                if proposing
                else ()
            )
            decision_views = self._decision_views(decided, views, group_names)
            unconnected = sum(
                1
                for group in set(listed.values())
                if (shape := shapes.get(group)) is not None
                and shape.status is GroupStatus.ACTIVE
                and not shape.confirmed_members
            )
        return LinksOverview(
            marketplace_key=store[0],
            marketplace_account_id=store[1],
            as_of=as_of,
            linked=len(listed),
            unconnected=unconnected,
            notices=notices,
            proposals=proposals,
            decisions=decision_views,
        )

    @staticmethod
    def _group_names(
        names: Mapping[str, Sequence[MemberName]], links: Iterable[LinkView]
    ) -> dict[str, str | None]:
        found: dict[str, str | None] = {}
        for group, members in names.items():
            found[group] = next((m.name for m in members if m.name), None)
        for link in links:
            if found.get(link.product_group_id) is None and link.name:
                found[link.product_group_id] = link.name
        return found

    @staticmethod
    def _notices(
        open_notices: Sequence[tuple[StoreLinkNotice, StoreListingLink]],
        links: Mapping[str, LinkView],
        group_names: Mapping[str, str | None],
    ) -> tuple[NoticeView, ...]:
        by_link = {link.link_id: link for link in links.values() if link.link_id is not None}
        found = []
        for notice, row in open_notices:
            link = by_link.get(row.link_id)
            if link is None or row.key_kind is None:
                continue
            found.append(
                NoticeView(
                    notice_id=notice.notice_id,
                    link_id=row.link_id,
                    key_kind=row.key_kind,
                    created_at=notice.created_at,
                    listing=link,
                    product_name=group_names.get(row.product_group_id),
                )
            )
        return tuple(found)

    def _proposals(
        self,
        store: tuple[str, str],
        views: Sequence[CatalogListingView],
        current: Mapping[str, _Current],
        links: Mapping[str, LinkView],
        codes: Mapping[str, set[str]],
        names: Mapping[str, Sequence[MemberName]],
        others: Sequence[_Other],
        decisions: set[tuple[str, str]],
        group_names: Mapping[str, str | None],
        shapes: Mapping[str, GroupShape],
    ) -> tuple[ProposalView, ...]:
        """「같은 상품인가요?」 for every listing that is still its own product (derived)."""
        listed = [_listing(view) for view in views if view.state == LISTED]
        other_keys: dict[tuple[str, str], set[str]] = {}
        group_texts: dict[str, list[str | None]] = {
            group: [m.name for m in members] for group, members in names.items()
        }
        group_tokens: dict[str, set[str]] = {}
        for group, members in names.items():
            for member in members:
                group_tokens.setdefault(group, set()).update(name_tokens(member.name))
        other_links: dict[str, list[LinkView]] = {}
        for listing_id, origin, group, name, keys, texts in others:
            for key in keys:
                other_keys.setdefault(key, set()).add(group)
            group_texts.setdefault(group, []).extend(texts)
            group_tokens.setdefault(group, set()).update(name_tokens(name))
            other_links.setdefault(group, []).append(
                LinkView(
                    link_id=None,
                    kind=USER_CONFIRMED,
                    key_kind=None,
                    listing_id=listing_id,
                    marketplace_key="",
                    marketplace_account_id="",
                    origin_product_no=origin,
                    channel_product_no=None,
                    product_group_id=group,
                    name=name,
                    sale_status=None,
                    sale_price=None,
                    stock_quantity=None,
                    listing_state=LISTED,
                    linked_at=None,
                    unlinkable=False,
                )
            )
        ended = {view.listing_id for view in views if view.state == ENDED}
        in_store = Counter(
            link.product_group_id for listing_id, link in links.items() if listing_id not in ended
        )
        shared = Counter(key for listing in listed for key in listing.keys)
        # Words in too many names say nothing about sameness.
        frequency = Counter(token for tokens in group_tokens.values() for token in tokens)
        common = {token for token, count in frequency.items() if count > 50}
        index: dict[str, set[str]] = {}
        for group, tokens in group_tokens.items():
            for token in tokens - common:
                index.setdefault(token, set()).add(group)
        found: list[ProposalView] = []
        for listing in sorted(listed, key=lambda item: item.view.origin_product_no):
            now = current.get(listing.view.listing_id)
            link = links.get(listing.view.listing_id)
            if now is None or link is None or now.derived is not None or now.stored is None:
                continue
            if now.stored.kind != IMPORTED:
                continue
            own = now.stored.group_id
            own_shape = shapes.get(own)
            if own_shape is None or own_shape.confirmed_members or own_shape.items:
                continue
            ambiguous = any(shared[key] > 1 for key in listing.keys)
            candidates: dict[str, tuple[str, float | None]] = {}
            for group, kind in self._strong_targets(listing, codes, other_keys, decisions).items():
                if group != own:
                    candidates[group] = (kind, None)
            words = name_tokens(listing.name)
            scored: list[tuple[float, str]] = []
            for group in {g for token in words - common for g in index.get(token, ())}:
                if group == own or group in candidates:
                    continue
                if (listing.view.listing_id, group) in decisions:
                    continue
                other = frozenset(group_tokens.get(group, set()))
                similarity = name_similarity(words, other)
                if similarity >= WEAK_THRESHOLD and len(words & other) >= WEAK_MIN_SHARED:
                    scored.append((similarity, group))
            for similarity, group in sorted(scored, key=lambda pair: (-pair[0], pair[1]))[
                :MAX_PROPOSALS_PER_LISTING
            ]:
                candidates[group] = (NAME_SIMILAR, round(similarity, 2))
            for group, (signal, score) in sorted(candidates.items()):
                shape = shapes.get(group)
                if shape is None or shape.status is not GroupStatus.ACTIVE:
                    continue
                if in_store[group] and not self._overridden(store[0], store[1], group):
                    # One listing per product and store (SCP-09): never proposed.
                    continue
                survivors = [group]
                if not shape.confirmed_members and not shape.items:
                    survivors.append(own)
                found.append(
                    ProposalView(
                        listing=link,
                        product_group_id=group,
                        product_name=group_names.get(group),
                        supplier_keys=tuple(sorted({m.supplier_key for m in names.get(group, ())})),
                        product_listings=tuple(other_links.get(group, ())),
                        signal=signal,
                        similarity=score,
                        ambiguous=signal != NAME_SIMILAR and ambiguous,
                        conflicts=conflicts(
                            amounts(listing.texts), amounts(group_texts.get(group, ()))
                        ),
                        survivors=tuple(survivors),
                    )
                )
        return tuple(found)

    @staticmethod
    def _decision_views(
        decided: Sequence[StoreListingDecision],
        views: Sequence[CatalogListingView],
        group_names: Mapping[str, str | None],
    ) -> tuple[DecisionView, ...]:
        by_id = {view.listing_id: view for view in views}
        found = []
        for row in decided:
            view = by_id.get(row.listing_id)
            found.append(
                DecisionView(
                    decision_id=row.decision_id,
                    listing_id=row.listing_id,
                    origin_product_no=None if view is None else view.origin_product_no,
                    listing_name=None if view is None else _name(view.fields),
                    product_group_id=row.product_group_id,
                    product_name=group_names.get(row.product_group_id),
                    created_at=row.created_at,
                )
            )
        return tuple(found)


__all__ = [
    "ADOPTED",
    "IMPORTED",
    "NAME_SIMILAR",
    "REGISTERED_BY_ICBM",
    "STRONG_KEY",
    "USER_CONFIRMED",
    "DecisionView",
    "GroupLinks",
    "LinkResult",
    "LinkView",
    "LinksOverview",
    "ListingLinkService",
    "NoticeView",
    "PassResult",
    "ProposalView",
    "StoreLinkConflict",
]
