"""The running supplier registry: repository suppliers merged with onboarded sites (ADR-0036 §4).

``integrations.suppliers.registry`` binds the repository suppliers once, at import, exactly as
before (OB-07). ``SupplierCatalog`` holds them unchanged and adds every onboarded site, bound from
its database record through the same template ``bind`` (``site.onboarded_site``). The container
creates one catalog per process, and the services read the supplier set through its live views,
so a rebind is seen by CONNECT, COLLECT, the extension transport and the source gates at once.

**Rebinding.** The onboarding store tells the catalog the key of every supplier it changed; the
catalog rebuilds that one supplier and publishes a new merged view in one reference swap. No
restart is needed. A request already holding the previous definition finishes with it.

**What is bound.**
- A ``DRAFT``, ``RECON`` or ``ACTIVE`` site is bound: CONNECT can test it, and its egress grant is
  its confirmed hosts (with its template's own platform hosts, such as Cafe24's shared secure-login
  host, which are reviewed template code).
- A ``DISABLED`` site is not bound at all: no CONNECT, no collection, no capture policy, and so no
  egress grant (ADR-0036 §7).
- A site whose key or storefront host is a repository supplier's, or that its template refuses,
  is not bound, and why is kept in ``problems``.

**Collectability** (OB-10). Only an ``ACTIVE`` onboarded site is collectable; ``collectable`` says
so for COLLECT submit, the extension capture and the list queue. A repository site keeps its own
rule: a ``RECON`` repository site still collects (ADR-0030 §7, OB-07). ``site_active`` answers the
registration preparation's PT-06 gate for both kinds.
"""

import logging
import threading
from collections.abc import Callable, ItemsView, Iterator, KeysView, Mapping, Sequence, ValuesView
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar
from urllib.parse import urlsplit

from sqlalchemy.exc import OperationalError

from app.stages.collect.collection import RegisteredCollection
from app.stages.collect.onboarding.site import OnboardingRecord, OnboardingState, onboarded_site
from app.stages.collect.onboarding.store import OnboardingStore
from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.platforms import PlatformTemplate
from integrations.suppliers.sites import (
    PLATFORM_DIRECTORY,
    TEMPLATES,
    BoundSite,
    bind_site,
    capture_policy_bytes,
)

logger = logging.getLogger(__name__)

K = TypeVar("K")
V = TypeVar("V")


class LiveMapping(Mapping[K, V]):
    """A read-only mapping that always reads the catalog's current view."""

    def __init__(self, current: Callable[[], Mapping[K, V]]) -> None:
        self._current = current

    def __getitem__(self, key: K) -> V:
        return self._current()[key]

    def __iter__(self) -> Iterator[K]:
        return iter(tuple(self._current()))

    def __len__(self) -> int:
        return len(self._current())

    def __contains__(self, key: object) -> bool:
        return key in self._current()

    # One snapshot per call: a rebind between reading a key and its value never splits a read.
    def keys(self) -> KeysView[K]:
        return self._current().keys()

    def items(self) -> ItemsView[K, V]:
        return self._current().items()

    def values(self) -> ValuesView[V]:
        return self._current().values()


@dataclass(frozen=True)
class OnboardedEntry:
    record: OnboardingRecord
    bound: BoundSite | None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class _View:
    definitions: Mapping[str, SupplierDefinition]
    collections: Mapping[str, RegisteredCollection]
    capture_policies: Mapping[str, bytes]
    names: Mapping[str, str]
    onboarded: Mapping[str, OnboardedEntry] = field(default_factory=dict)


def _host(definition: SupplierDefinition) -> str | None:
    return urlsplit(definition.profile.base_url).hostname


class SupplierCatalog:
    """Every supplier this process can connect to and collect from, rebound at run time."""

    def __init__(
        self,
        *,
        definitions: Sequence[SupplierDefinition],
        collections: Sequence[RegisteredCollection],
        capture_policies: Mapping[str, bytes],
        repository_active: Callable[[str], bool | None],
        store: OnboardingStore | None = None,
        templates: Mapping[str, PlatformTemplate] = TEMPLATES,
        platforms: Path = PLATFORM_DIRECTORY,
    ) -> None:
        repository: dict[str, SupplierDefinition] = {}
        for definition in definitions:
            key = definition.profile.supplier_key
            if key in repository:
                raise ValueError(f"duplicate supplier definition: {key}")
            repository[key] = definition
        self._definitions = MappingProxyType(repository)
        self._collections = MappingProxyType({c.supplier_key: c for c in collections})
        self._policies = MappingProxyType(dict(capture_policies))
        self._repository_active = repository_active
        self._repository_hosts = frozenset(
            host for host in (_host(d) for d in repository.values()) if host is not None
        )
        self._templates = templates
        self._platforms = platforms
        self._lock = threading.Lock()
        self._onboarded: dict[str, OnboardedEntry] = {}
        self._view = self._compose()
        self.definitions: Mapping[str, SupplierDefinition] = LiveMapping(
            lambda: self._view.definitions
        )
        self.collections: Mapping[str, RegisteredCollection] = LiveMapping(
            lambda: self._view.collections
        )
        self.capture_policies: Mapping[str, bytes] = LiveMapping(
            lambda: self._view.capture_policies
        )
        self.names: Mapping[str, str] = LiveMapping(lambda: self._view.names)
        self._store = store
        if store is not None:
            store.subscribe(self.rebind)
            self.reload()

    # ------------------------------------------------------------------ reads

    @property
    def repository_keys(self) -> frozenset[str]:
        return frozenset(self._definitions)

    @property
    def repository_hosts(self) -> frozenset[str]:
        return self._repository_hosts

    def onboarded(self, supplier_key: str) -> OnboardedEntry | None:
        return self._view.onboarded.get(supplier_key)

    def problems(self) -> Mapping[str, tuple[str, ...]]:
        return {key: e.problems for key, e in self._view.onboarded.items() if e.problems}

    def site_active(self, supplier_key: str) -> bool | None:
        """PT-06: True for an ACTIVE site, False for a site that is not, None for a key that is no
        site (the KM package). An onboarded site is ACTIVE only while it is also bound."""
        entry = self._view.onboarded.get(supplier_key)
        if entry is None:
            return self._repository_active(supplier_key)
        return entry.bound is not None and entry.record.state is OnboardingState.ACTIVE

    def collectable(self, supplier_key: str) -> bool:
        """OB-10: an onboarded site collects only while ACTIVE; every repository supplier keeps
        its own rule (a RECON repository site collects, ADR-0030 §7)."""
        entry = self._view.onboarded.get(supplier_key)
        if entry is None:
            return True
        return entry.bound is not None and entry.record.state is OnboardingState.ACTIVE

    # ------------------------------------------------------------------ binding

    def reload(self) -> None:
        """Bind every onboarded site from the database (process start)."""
        if self._store is None:
            return
        try:
            records = self._store.all_current()
        except OperationalError:
            # A database below the onboarding schema: readiness reports the schema, and nothing
            # onboarded is offered until the process starts on a migrated database.
            logger.warning("collect.onboarded_sites_unreadable")
            return
        with self._lock:
            self._onboarded = {r.supplier_key: self._entry(r) for r in records}
            self._view = self._compose()
        for key, entry in self._onboarded.items():
            for problem in entry.problems:
                logger.warning(
                    "collect.onboarded_site_not_bound",
                    extra={"supplier": key, "problem": problem},
                )

    def rebind(self, supplier_key: str) -> None:
        """Rebuild one onboarded supplier from its current record and publish the new view."""
        if self._store is None:
            return
        record = self._store.current(supplier_key)
        with self._lock:
            entry = self._entry(record)
            self._onboarded = {**self._onboarded, supplier_key: entry}
            self._view = self._compose()
        for problem in entry.problems:
            logger.warning(
                "collect.onboarded_site_not_bound",
                extra={"supplier": supplier_key, "problem": problem},
            )

    def _entry(self, record: OnboardingRecord) -> OnboardedEntry:
        key = record.supplier_key
        if record.state is OnboardingState.DISABLED:
            return OnboardedEntry(record, None)
        if key in self._definitions:
            return OnboardedEntry(record, None, ("the key is a repository supplier's",))
        if record.storefront_host in self._repository_hosts:
            return OnboardedEntry(record, None, ("the storefront is a repository supplier's",))
        template = self._templates.get(record.template)
        if template is None:
            return OnboardedEntry(record, None, (f"no template {record.template!r}",))
        try:
            site = onboarded_site(record, template)
        except ValueError as exc:
            return OnboardedEntry(record, None, (str(exc),))
        bound, problems = bind_site(site, template, self._platforms)
        return OnboardedEntry(record, bound, problems)

    def _compose(self) -> _View:
        definitions = dict(self._definitions)
        collections = dict(self._collections)
        policies = dict(self._policies)
        for key, entry in sorted(self._onboarded.items()):
            bound = entry.bound
            if bound is None:
                continue
            definitions[key] = bound.definition
            collections[key] = RegisteredCollection(
                collection=bound.collection,
                extractor_revision=bound.extractor_revision,
                extractor_fingerprint=bound.extractor_fingerprint,
            )
            try:
                policies[key] = capture_policy_bytes(bound, self._platforms)
            except (OSError, ValueError):
                logger.warning("collect.onboarded_capture_policy_absent", extra={"supplier": key})
        return _View(
            definitions=MappingProxyType(definitions),
            collections=MappingProxyType(collections),
            capture_policies=MappingProxyType(policies),
            names=MappingProxyType(
                {key: d.profile.display_name for key, d in sorted(definitions.items())}
            ),
            onboarded=MappingProxyType(dict(self._onboarded)),
        )
