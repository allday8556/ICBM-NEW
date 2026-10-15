"""SmartStore duplicate evidence — the catalog duplicate source (ADR-0042 S3b, §6; ADR-0014 §13).

Until S3b this module produced no evidence at all: ``POST /v1/products/search`` is adopted for
positive-only reconcile only, a seller-code search is similar, partial or exact, the codes are not
unique, and no search result is complete or fresh enough to be absence (ADR-0014 §13, §17.2). That
stays true. What replaces the always-unavailable lookup is not a search: it is the store's own
**COMPLETE catalog snapshot** (ADR-0042 §5), read by
:class:`~app.stages.operate.catalog.duplicates.CatalogDuplicateSource`:

- evidence exists only while a COMPLETE snapshot of the bound store is fresh under the target
  policy's ``catalog_max_age`` (60 minutes) from its start, and no ICBM mutation of the store
  started since; a stale or missing snapshot is no evidence (``DUPLICATE_EVIDENCE_STALE`` /
  ``DUPLICATE_EVIDENCE_UNAVAILABLE``) and starts the automatic re-sync;
- the SmartStore part is the unit's ``smartstore-seller-management-code/v1`` projection
  (:func:`product.seller_management_code`) and the **positive spot check** of the first CREATE
  copy, which is the adopted §28.2 seller-code search (:class:`SmartStoreReconcileLookup`): an
  exact ``STOREFARM`` hit the snapshot did not show is a match, and zero hits change nothing;
- a ``NO_MATCH`` is duplicate-preflight evidence for a new Intent only. It is never remote absence
  for ADR-0014 §10/§14/§28 and never frees an ``UNKNOWN`` conflict scope (SCP-05).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from app.stages.operate.catalog.coverage import CatalogCoverage
from app.stages.operate.catalog.duplicates import CatalogDuplicateSource
from app.stages.operate.catalog.service import StoreCatalogService
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.execution import SmartStoreReconcileLookup

MARKETPLACE_KEY: Final = "smartstore"
LOOKUP_CONTRACT_VERSION: Final = "smartstore-catalog-duplicate/v1"


def smartstore_duplicate_source(
    *,
    catalog: StoreCatalogService,
    refresh: Callable[[], bool],
    convention_codes: Callable[[Sequence[str]], Iterable[str]],
    mutation_started_at: Callable[[str, str], datetime | None],
    caller: SmartStoreEndpointCaller,
    bearer: Any,
    linked: Callable[[str, str], Mapping[str, str | Sequence[str]]] | None = None,
    coverage: CatalogCoverage | None = None,
    member_gtins: Callable[[Sequence[str]], Iterable[str]] | None = None,
    different: Callable[[str, str], Iterable[tuple[str, str]]] | None = None,
) -> CatalogDuplicateSource:
    """The production SmartStore duplicate source (composition root). ``linked`` is the
    ListingLinks owner's read (ADR-0042 S2a): a listing linked to the unit's product matches."""
    return CatalogDuplicateSource(
        marketplace_key=MARKETPLACE_KEY,
        contract_version=LOOKUP_CONTRACT_VERSION,
        catalog=catalog,
        seller_code=product.seller_management_code,
        convention_codes=convention_codes,
        mutation_started_at=mutation_started_at,
        refresh=refresh,
        spot=SmartStoreReconcileLookup(caller=caller, bearer=bearer),
        linked=linked,
        coverage=coverage,
        member_gtins=member_gtins,
        different=different,
    )


__all__ = ["LOOKUP_CONTRACT_VERSION", "MARKETPLACE_KEY", "smartstore_duplicate_source"]
