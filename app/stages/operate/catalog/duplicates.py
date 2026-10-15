"""The catalog duplicate source (ADR-0042 S3b, §6; amends ADR-0014 §13): REGISTER's duplicate
evidence, read from the store's own COMPLETE catalog snapshot.

For one provider-listing unit (its groups' Items, store *S*) it evaluates the last COMPLETE sync of
*S* — only while that sync is fresh under the target policy's ``catalog_max_age`` (60 minutes) from
its **start**, and no ICBM mutation of *S* started since (SCP-05):

- a listing whose seller code equals the unit's ``smartstore-seller-management-code/v1``
  projection, or an owner-declared convention code of one of its Items' sources (ADR-0024 §2):
  ``MATCH``, strong (``SELLER_CODE``);
- a listing whose barcode is a valid GTIN equal to a member's CONFIRMED GTIN fact: ``MATCH``,
  strong (``GTIN``). GTIN is claimed in ``keys_checked`` only when every listing of the snapshot
  had its details read since the snapshot's start; otherwise a barcode changed since its last
  read could be missed, so GTIN stays unclaimed and a policy requiring it stays ``INCOMPLETE``;
- a listing whose name equals the unit's listing name, or is **similar** to it by the S2a
  name-similarity rule (``keys.name_similarity`` ≥ 60 % with at least two words in common):
  ``MATCH``, weak (``NORMALIZED_NAME``) — ``REVIEW_REQUIRED``; the seller chooses 「같은 상품」,
  「다른 상품」 (an S2a decision, after which that listing no longer matches the product) or
  「의도적으로 한 번 더 등록」. A similar name is never a ``NO_MATCH``;
- none of the above: ``NO_MATCH``, with the keys checked and the snapshot's ``as_of`` — but only
  once the seller has confirmed the store's catalog coverage (:mod:`.coverage`, ADR-0042 §5.2);
  until then no evidence (``DUPLICATE_EVIDENCE_UNAVAILABLE``, the row pointing to
  「스토어 상품」). A match is positive evidence and needs no confirmation;
- no COMPLETE snapshot, or a stale one: no evidence (``DUPLICATE_EVIDENCE_UNAVAILABLE`` /
  ``DUPLICATE_EVIDENCE_STALE``), and the automatic re-sync (§5.2).

What it never is:

- **remote absence.** A ``NO_MATCH`` holds relative to that snapshot, for a **new** Intent's
  duplicate preflight only. It is never reconcile evidence, never ADR-0014 §10/§14/§28 absence,
  and it frees no ``UNKNOWN`` conflict scope: REGISTER's own checks of §13 (live or unresolved
  registrations, ``UNKNOWN`` scopes, adopted listings) run first and are unchanged;
- **a provider call on a screen.** It reads the database. The only provider read it can make is the
  **positive spot check** of the first CREATE copy (``spot_check``): the unit's own projected
  seller code is searched (ADR-0014 §28.2, positive only). An exact hit the snapshot did not show
  is a ``MATCH``; zero hits, or a failed search, change nothing.

**The fingerprint stays stable.** The evidence digest covers only the verdict-relevant content —
the verdict, the matches, the keys checked and the contract version — never the snapshot's run or
time, so a re-sync that finds the same thing keeps the ASSET and CREATE stages on one fingerprint.
Freshness is checked again on every read, which is how send time checks it.

**Links (S2a).** A listing **linked** to one of the unit's common products — by any link kind,
the ICBM registration and the adoption included (ADR-0042 §7) — is a ``MATCH``, strong
(``LISTING_LINK``): one listing per product and store. A listing ICBM registered or adopted is also
a duplicate by REGISTER's own checks (``LIVE_REGISTRATION_EXISTS``, ``ADOPTED_LISTING_EXISTS``).
Brand + model is not checked: no catalog model or brand field exists, so the key is never
claimed as checked (a policy that requires it stays ``INCOMPLETE``).
"""

import logging
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from app.stages.operate.catalog.coverage import CONFIRMED, CatalogCoverage
from app.stages.operate.catalog.keys import gtin_key, name_similarity, name_tokens
from app.stages.operate.catalog.links import WEAK_MIN_SHARED, WEAK_THRESHOLD
from app.stages.operate.catalog.service import (
    FRESH,
    LISTED,
    STALE,
    CatalogRunView,
    StoreCatalogService,
    listing_filter,
)
from app.stages.register.model import sanitized_digest
from app.stages.register.policy import CATALOG_MAX_AGE, DuplicateKeyKind
from app.stages.register.preparation import (
    DUPLICATE_EVIDENCE_STALE,
    DUPLICATE_EVIDENCE_UNAVAILABLE,
    DuplicateEvidence,
    DuplicateMatch,
    DuplicateVerdict,
)
from app.stages.register.provider import (
    DuplicateCheck,
    DuplicateUnit,
    LookupStatus,
    MatchedListing,
    ReconcileLookup,
)

logger = logging.getLogger("icbm.operate.catalog.duplicates")

# The verdict-relevant content of one check, hashed into the evidence digest.
DIGEST_VERSION: Final = "catalog-duplicate-evidence/v1"


def normalized_code(value: str | None) -> str | None:
    """A seller code compared exactly after trim and case (ADR-0042 §7)."""
    if value is None:
        return None
    text = value.strip().casefold()
    return text or None


def normalized_name(value: str | None) -> str | None:
    """A listing name as a weak signal: NFKC, case-folded, letters and digits only. Equality only;
    no similarity score is ever a match."""
    if value is None:
        return None
    folded = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(ch for ch in folded if ch.isalnum())
    return text or None


def similar_names(left: frozenset[str], right: frozenset[str]) -> bool:
    """The S2a weak name signal (ADR-0042 §7): enough words in common to propose, never link."""
    return name_similarity(left, right) >= WEAK_THRESHOLD and len(left & right) >= WEAK_MIN_SHARED


@dataclass(frozen=True)
class _Indexed:
    origin_product_no: str
    codes: frozenset[str]
    name_key: str | None
    name: str | None
    sale_status: str | None
    words: frozenset[str] = frozenset()
    gtin: str | None = None
    read_at: datetime | None = None
    filter_key: str | None = None


class CatalogDuplicateSource:
    """``DuplicateLookupSource`` over the StoreCatalog owner (ADR-0042 §6)."""

    def __init__(
        self,
        *,
        marketplace_key: str,
        contract_version: str,
        catalog: StoreCatalogService,
        seller_code: Callable[[str], str],
        convention_codes: Callable[[Sequence[str]], Iterable[str]],
        mutation_started_at: Callable[[str, str], datetime | None],
        refresh: Callable[[], bool],
        spot: ReconcileLookup | None = None,
        linked: Callable[[str, str], Mapping[str, str | Sequence[str]]] | None = None,
        coverage: CatalogCoverage | None = None,
        member_gtins: Callable[[Sequence[str]], Iterable[str]] | None = None,
        different: Callable[[str, str], Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        self._marketplace_key = marketplace_key
        self._contract_version = contract_version
        self._catalog = catalog
        # The marketplace's own projection of a listing identity (a pure function).
        self._seller_code = seller_code
        # ADR-0024 §2: the owner-declared convention codes of the unit's Items' sources.
        self._convention_codes = convention_codes
        # REGISTER's last listing mutation of the store (a CREATE or a deletion attempt).
        self._mutation_started_at = mutation_started_at
        # The automatic pre-registration sync; True while a sync runs.
        self._refresh = refresh
        self._spot = spot
        # ADR-0042 S2a: origin product → common product, for every listing link of a store.
        self._linked = linked
        # ADR-0042 §5.2 tightening: NO_MATCH only on a seller-confirmed coverage. Without the
        # owner nothing is confirmed (fail closed).
        self._coverage = coverage
        # ADR-0042 §6: the CONFIRMED GTIN facts of the unit's products' members.
        self._member_gtins = member_gtins
        # ADR-0042 S2a: (origin product, common product) pairs the seller said 「다른 상품」.
        self._different = different
        # A finished COMPLETE run never changes, so its index is kept per run.
        self._index_lock = threading.Lock()
        self._index_run: str | None = None
        self._index: tuple[_Indexed, ...] = ()

    @property
    def marketplace_key(self) -> str:
        return self._marketplace_key

    def available(self) -> bool:
        """Whether a COMPLETE, fresh snapshot exists for the bound account now (ADR-0042 §6), under
        the default 60-minute bound."""
        account = self._catalog.account()
        if account is None:
            return False
        state, _ = self._catalog.freshness(
            max_age=CATALOG_MAX_AGE,
            mutation_started_at=self._mutation_started_at(self._marketplace_key, account),
        )
        return state == FRESH

    def truth_token(self, marketplace_account_id: str, max_age: timedelta) -> str:
        account = self._catalog.account()
        if account is None or account != marketplace_account_id:
            return sanitized_digest({"account": None})
        mutation = self._mutation_started_at(self._marketplace_key, account)
        state, run = self._catalog.freshness(max_age=max_age, mutation_started_at=mutation)
        links = self._links(account)
        return sanitized_digest(
            {
                "contract": self._contract_version,
                "state": state,
                "run": None if run is None else run.run_id,
                "mutation": None if mutation is None else mutation.isoformat(),
                # A link moves an answer as much as a sync does (ADR-0042 §6, S2a); a failed
                # read is its own state, never an empty set of links.
                "links": None
                if links is None
                else sorted([origin, sorted(groups)] for origin, groups in links.items()),
                # The seller's coverage confirmation and 「다른 상품」 decisions move answers too.
                "coverage": self._coverage_token(account),
                "different": self._different_token(account),
            }
        )

    def _coverage_token(self, account: str) -> str | None:
        if self._coverage is None:
            return None
        try:
            confirmation = self._coverage.confirmation(account)
        except Exception:
            logger.warning("operate.catalog_duplicate_coverage_failed", exc_info=True)
            return "failed"
        return None if confirmation is None else confirmation.event_id

    def _different_token(self, account: str) -> list[list[str]] | str | None:
        pairs = self._different_pairs(account)
        return "failed" if pairs is None else sorted([list(pair) for pair in pairs])

    def _different_pairs(self, account: str) -> set[tuple[str, str]] | None:
        """「다른 상품」 pairs; ``set()`` when no owner is wired, ``None`` when it failed. A failed
        read only means no weak match is dismissed: it never hides a match."""
        if self._different is None:
            return set()
        try:
            return set(self._different(self._marketplace_key, account))
        except Exception:
            logger.warning("operate.catalog_duplicate_decisions_failed", exc_info=True)
            return None

    def _unit_gtins(self, unit: DuplicateUnit) -> set[str] | None:
        """The unit's members' valid GTINs, or ``None`` when no GTIN owner answered."""
        if self._member_gtins is None:
            return None
        try:
            found = self._member_gtins(unit.group_ids)
        except Exception:
            logger.warning("operate.catalog_duplicate_gtins_failed", exc_info=True)
            return None
        return {key for key in map(gtin_key, found) if key is not None}

    def _links(self, account: str) -> Mapping[str, frozenset[str]] | None:
        """The store's links (origin product → **every** common product it is linked to, the one
        its registration or adoption holds and any stored link still ACTIVE beside it), ``{}``
        when no links owner is wired, and ``None`` when the owner failed to answer: a failed read
        is never "no link"."""
        if self._linked is None:
            return {}
        try:
            found = self._linked(self._marketplace_key, account)
        except Exception:
            logger.warning("operate.catalog_duplicate_links_failed", exc_info=True)
            return None
        return {
            origin: frozenset((groups,) if isinstance(groups, str) else groups)
            for origin, groups in found.items()
        }

    def check(
        self, unit: DuplicateUnit, *, spot_check: bool = False, resync: bool = True
    ) -> DuplicateCheck:
        account = self._catalog.account()
        if unit.marketplace_key != self._marketplace_key or account is None:
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE)
        if account != unit.marketplace_account_id:
            # The catalog is the bound store's; another account's unit has no snapshot here.
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE)
        mutation = self._mutation_started_at(self._marketplace_key, account)
        state, run = self._catalog.freshness(max_age=unit.max_age, mutation_started_at=mutation)
        if state != FRESH or run is None:
            # Stale only because ICBM itself changed the store since the sync started: the CREATE
            # send path waits for the re-sync then (ADR-0042 §5.2).
            by_mutation = (
                state == STALE
                and mutation is not None
                and self._catalog.freshness(max_age=unit.max_age)[0] == FRESH
            )
            return DuplicateCheck(
                None,
                DUPLICATE_EVIDENCE_STALE if state == STALE else DUPLICATE_EVIDENCE_UNAVAILABLE,
                as_of=None if run is None else run.as_of,
                resyncing=self._resync() if resync else False,
                stale_since_mutation=mutation if by_mutation else None,
            )
        try:
            own = self._seller_code(unit.listing_identity)
        except ValueError:
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE, as_of=run.as_of)
        codes = {normalized_code(own)} | {
            normalized_code(code) for code in self._convention_codes(unit.item_ids)
        }
        codes.discard(None)
        name_key = normalized_name(unit.listing_name)
        words = name_tokens(unit.listing_name)
        index = self._indexed(run)
        if index is None:
            # Another sync completed while this one was read: never mix two runs, never guess.
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE, as_of=run.as_of)
        groups = set(unit.group_ids)
        links = self._links(account)
        if links is None:
            # Fail closed (SCP-06): the links owner did not answer, so whether a store listing is
            # already this product's is unknown. No evidence, never a NO_MATCH that names the
            # link key as checked.
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE, as_of=run.as_of)
        gtins = self._unit_gtins(unit)
        different = self._different_pairs(account) or set()
        found: dict[str, MatchedListing] = {}
        for listing in index:
            if groups & links.get(listing.origin_product_no, frozenset()):
                kind = DuplicateKeyKind.LISTING_LINK
            elif listing.codes & codes:
                kind = DuplicateKeyKind.SELLER_CODE
            elif gtins and listing.gtin is not None and listing.gtin in gtins:
                kind = DuplicateKeyKind.GTIN
            elif (name_key is not None and listing.name_key == name_key) or (
                words and similar_names(words, listing.words)
            ):
                # A weak signal; dismissed only by the seller's 「다른 상품」 for every product
                # of the unit (S2a decisions), never by ICBM.
                if groups and all((listing.origin_product_no, g) in different for g in groups):
                    continue
                kind = DuplicateKeyKind.NORMALIZED_NAME
            else:
                continue
            found[listing.origin_product_no] = MatchedListing(
                kind, listing.origin_product_no, listing.name, listing.sale_status
            )
        if spot_check:
            known = {listing.origin_product_no: listing for listing in index}
            for origin in self._spot_hits(account, unit.listing_identity):
                shown = found.get(origin)
                if shown is not None and shown.key_kind is not DuplicateKeyKind.NORMALIZED_NAME:
                    continue
                seen = known.get(origin)
                found[origin] = MatchedListing(
                    DuplicateKeyKind.SELLER_CODE,
                    origin,
                    None if seen is None else seen.name,
                    None if seen is None else seen.sale_status,
                )
        # GTIN is claimed only when it was compared (a GTIN owner answered) and every listing's
        # details — where its barcode is read — were read since this snapshot's start.
        gtin_fresh = gtins is not None and all(
            listing.read_at is not None and listing.read_at >= run.as_of for listing in index
        )
        keys = frozenset(
            {DuplicateKeyKind.SELLER_CODE}
            | ({DuplicateKeyKind.NORMALIZED_NAME} if name_key is not None else set())
            | ({DuplicateKeyKind.LISTING_LINK} if self._linked is not None else set())
            | ({DuplicateKeyKind.GTIN} if gtin_fresh else set())
        )
        matched = tuple(
            sorted(
                found.values(),
                key=lambda m: (
                    m.key_kind is not DuplicateKeyKind.LISTING_LINK,
                    m.key_kind is not DuplicateKeyKind.SELLER_CODE,
                    m.marketplace_product_id,
                ),
            )
        )
        if not matched and not self._covered(account, index):
            # ADR-0042 §5.2 tightening: a status class may be missing from the enumeration until
            # the seller confirms the per-status counts; no NO_MATCH before that.
            return DuplicateCheck(
                None, DUPLICATE_EVIDENCE_UNAVAILABLE, as_of=run.as_of, coverage_unconfirmed=True
            )
        verdict = DuplicateVerdict.MATCH if matched else DuplicateVerdict.NO_MATCH
        matches = tuple(DuplicateMatch(m.key_kind, m.marketplace_product_id) for m in matched)
        evidence = DuplicateEvidence(
            marketplace_key=unit.marketplace_key,
            marketplace_account_id=unit.marketplace_account_id,
            listing_identity=unit.listing_identity,
            lookup_contract_version=self._contract_version,
            evidence_digest=self._digest(verdict, keys, matches),
            verdict=verdict,
            keys_checked=keys,
            matches=matches,
        )
        return DuplicateCheck(evidence, None, as_of=run.as_of, matched=matched)

    # ------------------------------------------------------------------ helpers

    def _covered(self, account: str, index: Sequence[_Indexed]) -> bool:
        """Whether the seller's coverage confirmation holds for this snapshot's counts."""
        if self._coverage is None:
            return False
        counts: dict[str, int] = {}
        for listing in index:
            if listing.filter_key is not None:
                counts[listing.filter_key] = counts.get(listing.filter_key, 0) + 1
        try:
            return self._coverage.state(account, sum(counts.values()), counts) == CONFIRMED
        except Exception:
            logger.warning("operate.catalog_duplicate_coverage_failed", exc_info=True)
            return False

    def _digest(
        self,
        verdict: DuplicateVerdict,
        keys: frozenset[DuplicateKeyKind],
        matches: Sequence[DuplicateMatch],
    ) -> str:
        """The stable fingerprint digest (ADR-0042 §6): verdict-relevant content only."""
        return sanitized_digest(
            {
                "digest_version": DIGEST_VERSION,
                "lookup_contract_version": self._contract_version,
                "verdict": verdict.value,
                "keys_checked": sorted(key.value for key in keys),
                "matches": sorted([m.key_kind.value, m.provider_listing_ref] for m in matches),
            }
        )

    def _resync(self) -> bool:
        try:
            return bool(self._refresh())
        except Exception:
            # The re-sync is a convenience of the screen; no evidence exists either way.
            logger.warning("operate.catalog_duplicate_resync_failed", exc_info=True)
            return False

    def _spot_hits(self, account: str, listing_identity: str) -> tuple[str, ...]:
        """The exact ``STOREFARM`` origin products of the unit's own projected code, or none.
        Positive only: a failure, a partial result or zero hits change nothing."""
        if self._spot is None or not self._spot.available():
            return ()
        try:
            found: Mapping[str, Any] = self._spot.find(
                marketplace_account_id=account, listing_identity=listing_identity
            )
        except Exception:
            logger.warning("operate.catalog_duplicate_spot_check_failed", exc_info=True)
            return ()
        if found.get("status") != LookupStatus.COMPLETE.value:
            return ()
        hits = []
        for candidate in found.get("candidates") or ():
            origin = candidate.get("origin_product_no") if isinstance(candidate, Mapping) else None
            if isinstance(origin, str) and origin:
                hits.append(origin)
        return tuple(sorted(set(hits)))

    def _indexed(self, run: CatalogRunView) -> tuple[_Indexed, ...] | None:
        """The listings of exactly this COMPLETE run, or ``None`` when the last COMPLETE run is
        no longer it."""
        with self._index_lock:
            if self._index_run == run.run_id:
                return self._index
        snapshot = self._catalog.snapshot()
        if snapshot.run is None or snapshot.run.run_id != run.run_id:
            return None
        index = tuple(
            _Indexed(
                origin_product_no=view.origin_product_no,
                codes=frozenset(
                    code
                    for code in (
                        normalized_code(view.seller_management_code),
                        normalized_code(view.listed_seller_code),
                    )
                    if code is not None
                ),
                name_key=normalized_name(_name(view.fields)),
                name=_name(view.fields),
                sale_status=view.sale_status,
                words=name_tokens(_name(view.fields)),
                gtin=gtin_key(view.seller_barcode),
                read_at=view.details_read_at,
                filter_key=listing_filter(view) if view.state == LISTED else None,
            )
            for view in snapshot.listings
        )
        with self._index_lock:
            self._index_run, self._index = run.run_id, index
        return index


def _name(fields: Mapping[str, Any] | None) -> str | None:
    value = None if fields is None else fields.get("name")
    return value if isinstance(value, str) and value else None


__all__ = [
    "DIGEST_VERSION",
    "CatalogDuplicateSource",
    "normalized_code",
    "normalized_name",
]
