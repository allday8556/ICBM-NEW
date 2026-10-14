"""The running supplier registry: repository suppliers merged with onboarded sites (ADR-0036 §4).

``integrations.suppliers.registry`` binds the repository suppliers once, at import, exactly as
before (OB-07). ``SupplierCatalog`` holds them unchanged and adds every onboarded site, bound from
its database record through the same template ``bind`` (``site.onboarded_site``). The container
creates one catalog per process, and the services read the supplier set through its live views,
so a rebind is seen by CONNECT, COLLECT, the extension transport and the source gates at once.

**Rebinding.** The onboarding store tells the catalog the key of every supplier it changed; the
catalog rebuilds that one supplier and publishes a new merged view in one reference swap. No
restart is needed. A request already holding the previous definition finishes with it.

**Fail closed.** A rebind first withdraws the supplier from every view — its definition, its
collection, its capture policy and so its egress grant — and only then rebuilds it from the
committed record. So a change that narrows what a site may do (``RECON``, ``DISABLED``, a host
withdrawn) narrows it even when the rebuild fails. A rebuild that fails for any reason (an
unreadable record, a template refusal, a binder or capture-policy error, any exception) leaves the
supplier unbound, never on its previous binding; the reason is kept in ``problems`` and logged, and
nothing is raised into the store's write path, whose write is already committed. Start-up applies
the same rule to every onboarded site.

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
registration preparation's PT-06 gate for both kinds. ``active_origins`` lists the same ``ACTIVE``
sites as the exact origins the extension may ask Chrome for (ADR-0036 §8, OB-08).
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
    """One onboarded supplier as the catalog holds it. It is offered only when ``bound`` is set,
    and ``bound`` is set only together with its registered collection and capture policy."""

    # ``None`` when the committed record could not be read at all.
    record: OnboardingRecord | None
    bound: BoundSite | None
    problems: tuple[str, ...] = ()
    registered: RegisteredCollection | None = None
    capture_policy: bytes | None = None

    @property
    def offered(self) -> bool:
        return (
            self.bound is not None
            and self.registered is not None
            and self.capture_policy is not None
        )

    @property
    def active(self) -> bool:
        return (
            self.offered and self.record is not None and self.record.state is OnboardingState.ACTIVE
        )


@dataclass(frozen=True)
class OnboardedOrigin:
    """An ``ACTIVE`` onboarded site the extension may ask Chrome for (ADR-0036 §8, OB-08): its
    exact storefront origin, never a pattern, with its key and the name the operator sees."""

    supplier_key: str
    display_name: str
    origin: str


@dataclass(frozen=True)
class _View:
    definitions: Mapping[str, SupplierDefinition]
    collections: Mapping[str, RegisteredCollection]
    capture_policies: Mapping[str, bytes]
    names: Mapping[str, str]
    onboarded: Mapping[str, OnboardedEntry] = field(default_factory=dict)


def _log_problems(key: str, entry: OnboardedEntry) -> None:
    for problem in entry.problems:
        logger.warning(
            "collect.onboarded_site_not_bound", extra={"supplier": key, "problem": problem}
        )


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

    def bind_words(
        self, record: OnboardingRecord, words: Mapping[str, tuple[str, ...]]
    ) -> tuple[BoundSite | None, tuple[str, ...]]:
        """``record``'s site as it would bind under ``words``, with the same checks and identity
        rule as a real binding, and nothing registered: what the words editor previews (§6)."""
        template = self._templates.get(record.template)
        if template is None:
            return None, (f"no template {record.template!r}",)
        site = onboarded_site(record, template, words=words, require_storefront=False)
        return bind_site(site, template, self._platforms)

    def active_origins(self) -> tuple[OnboardedOrigin, ...]:
        """Every onboarded site that is ``ACTIVE`` and offered, by key: the only origins the
        extension may ask Chrome for, one at a time on the operator's click (ADR-0036 §8). A
        repository supplier is never listed; its host is in the manifest already."""
        listed = []
        for key, entry in sorted(self._view.onboarded.items()):
            if not entry.active or entry.bound is None or entry.registered is None:
                continue
            listed.append(
                OnboardedOrigin(
                    supplier_key=key,
                    display_name=entry.bound.definition.profile.display_name,
                    origin=f"https://{entry.registered.collection.profile.storefront_host}",
                )
            )
        return tuple(listed)

    def problems(self) -> Mapping[str, tuple[str, ...]]:
        return {key: e.problems for key, e in self._view.onboarded.items() if e.problems}

    def site_active(self, supplier_key: str) -> bool | None:
        """PT-06: True for an ACTIVE site, False for a site that is not, None for a key that is no
        site (the KM package). An onboarded site is ACTIVE only while it is also bound."""
        entry = self._view.onboarded.get(supplier_key)
        if entry is None:
            return self._repository_active(supplier_key)
        return entry.active

    def collectable(self, supplier_key: str) -> bool:
        """OB-10: an onboarded site collects only while ACTIVE; every repository supplier keeps
        its own rule (a RECON repository site collects, ADR-0030 §7)."""
        entry = self._view.onboarded.get(supplier_key)
        if entry is None:
            return True
        return entry.active

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
        except Exception:
            # Nothing onboarded is offered; a site that cannot be read is never assumed bound.
            logger.exception("collect.onboarded_sites_unreadable")
            return
        entries = {r.supplier_key: self._entry(r) for r in records}
        with self._lock:
            self._onboarded = entries
            self._view = self._compose()
        for key, entry in entries.items():
            _log_problems(key, entry)

    def rebind(self, supplier_key: str) -> None:
        """Withdraw one onboarded supplier, then rebuild it from its committed record.

        It never raises: the store's write is already committed, and a supplier whose rebuild
        fails stays withdrawn (fail closed)."""
        if self._store is None:
            return
        previous = self._view.onboarded.get(supplier_key)
        # 1. Narrow first: nothing of the previous binding outlives the committed change.
        self._publish(
            supplier_key,
            OnboardedEntry(
                previous.record if previous is not None else None,
                None,
                ("rebinding after a committed change",),
            ),
        )
        # 2. Rebuild from the committed record; any failure leaves the supplier withdrawn.
        try:
            record = self._store.current(supplier_key)
        except Exception as exc:
            logger.exception("collect.onboarded_site_unreadable", extra={"supplier": supplier_key})
            entry = OnboardedEntry(
                previous.record if previous is not None else None,
                None,
                (f"the committed record could not be read ({type(exc).__name__})",),
            )
        else:
            entry = self._entry(record)
        self._publish(supplier_key, entry)
        _log_problems(supplier_key, entry)

    def _publish(self, supplier_key: str, entry: OnboardedEntry) -> None:
        with self._lock:
            self._onboarded = {**self._onboarded, supplier_key: entry}
            self._view = self._compose()

    def _entry(self, record: OnboardingRecord) -> OnboardedEntry:
        """The supplier bound from ``record``, or unbound with why. It never raises."""
        try:
            return self._bind(record)
        except Exception as exc:
            logger.exception(
                "collect.onboarded_site_bind_failed", extra={"supplier": record.supplier_key}
            )
            return OnboardedEntry(
                record, None, (f"the site could not be bound ({type(exc).__name__})",)
            )

    def _bind(self, record: OnboardingRecord) -> OnboardedEntry:
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
        if bound is None:
            return OnboardedEntry(record, None, problems or ("the template refused the site",))
        registered = RegisteredCollection(
            collection=bound.collection,
            extractor_revision=bound.extractor_revision,
            extractor_fingerprint=bound.extractor_fingerprint,
        )
        policy = capture_policy_bytes(bound, self._platforms)
        return OnboardedEntry(record, bound, problems, registered, policy)

    def _compose(self) -> _View:
        definitions = dict(self._definitions)
        collections = dict(self._collections)
        policies = dict(self._policies)
        for key, entry in sorted(self._onboarded.items()):
            # Offered whole or not at all: a definition never stands without its collection and
            # its capture policy, nor they without it.
            if (
                not entry.offered
                or entry.bound is None
                or entry.registered is None
                or entry.capture_policy is None
            ):
                continue
            definitions[key] = entry.bound.definition
            collections[key] = entry.registered
            policies[key] = entry.capture_policy
        return _View(
            definitions=MappingProxyType(definitions),
            collections=MappingProxyType(collections),
            capture_policies=MappingProxyType(policies),
            names=MappingProxyType(
                {key: d.profile.display_name for key, d in sorted(definitions.items())}
            ),
            onboarded=MappingProxyType(dict(self._onboarded)),
        )
