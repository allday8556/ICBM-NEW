"""The catalog duplicate source (ADR-0042 S3b, §6; amends ADR-0014 §13): REGISTER's duplicate
evidence, read from the store's own COMPLETE catalog snapshot.

For one provider-listing unit (its groups' Items, store *S*) it evaluates the last COMPLETE sync of
*S* — only while that sync is fresh under the target policy's ``catalog_max_age`` (60 minutes) from
its **start**, and no ICBM mutation of *S* started since (SCP-05):

- a listing whose seller code equals the unit's ``smartstore-seller-management-code/v1``
  projection, or an owner-declared convention code of one of its Items' sources (ADR-0024 §2):
  ``MATCH``, strong (``SELLER_CODE``);
- a listing whose normalized name equals the unit's listing name: ``MATCH``, weak
  (``NORMALIZED_NAME``) — the seller decides;
- none of the above: ``NO_MATCH``, with the keys checked and the snapshot's ``as_of``;
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

Links of S2a do not exist yet: a listing ICBM registered or adopted for the unit's groups is a
duplicate by REGISTER's own checks (``LIVE_REGISTRATION_EXISTS``, ``ADOPTED_LISTING_EXISTS``). GTIN
and brand + model are not checked: no member GTIN fact and no catalog model or brand field exists,
so neither key is claimed as checked (a policy that requires one stays ``INCOMPLETE``).
"""

import logging
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.stages.operate.catalog.service import FRESH, STALE, CatalogRunView, StoreCatalogService
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


@dataclass(frozen=True)
class _Indexed:
    origin_product_no: str
    codes: frozenset[str]
    name_key: str | None
    name: str | None
    sale_status: str | None


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

    def check(self, unit: DuplicateUnit, *, spot_check: bool = False) -> DuplicateCheck:
        account = self._catalog.account()
        if unit.marketplace_key != self._marketplace_key or account is None:
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE)
        if account != unit.marketplace_account_id:
            # The catalog is the bound store's; another account's unit has no snapshot here.
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE)
        state, run = self._catalog.freshness(
            max_age=unit.max_age,
            mutation_started_at=self._mutation_started_at(self._marketplace_key, account),
        )
        if state != FRESH or run is None:
            return DuplicateCheck(
                None,
                DUPLICATE_EVIDENCE_STALE if state == STALE else DUPLICATE_EVIDENCE_UNAVAILABLE,
                as_of=None if run is None else run.as_of,
                resyncing=self._resync(),
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
        index = self._indexed(run)
        if index is None:
            # Another sync completed while this one was read: never mix two runs, never guess.
            return DuplicateCheck(None, DUPLICATE_EVIDENCE_UNAVAILABLE, as_of=run.as_of)
        found: dict[str, MatchedListing] = {}
        for listing in index:
            if listing.codes & codes:
                kind = DuplicateKeyKind.SELLER_CODE
            elif name_key is not None and listing.name_key == name_key:
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
                if shown is not None and shown.key_kind is DuplicateKeyKind.SELLER_CODE:
                    continue
                seen = known.get(origin)
                found[origin] = MatchedListing(
                    DuplicateKeyKind.SELLER_CODE,
                    origin,
                    None if seen is None else seen.name,
                    None if seen is None else seen.sale_status,
                )
        keys = frozenset(
            {DuplicateKeyKind.SELLER_CODE}
            | ({DuplicateKeyKind.NORMALIZED_NAME} if name_key is not None else set())
        )
        matched = tuple(
            sorted(
                found.values(),
                key=lambda m: (
                    m.key_kind is not DuplicateKeyKind.SELLER_CODE,
                    m.marketplace_product_id,
                ),
            )
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
