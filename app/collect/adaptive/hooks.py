"""Site adapter hooks: the closed binding model, the promotion key and G5–G7 (ADR-0017 §6).

A hook is a pure function a supplier package holds. It is handed one located text and returns a
**candidate** or ``CANNOT_PARSE``; it never returns a status or evidence. The engine validates every
candidate against the field's own value model and decides the status itself.

A supplier's hook manifest carries a semantic ``HOOK_REVISION`` (bound by the EPR, part of the
semantic identity) and an implementation ``HOOK_FINGERPRINT`` (provenance and validation freshness
only). A binding whose revision is not the running one refuses the bundle.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.collect.adaptive.profiles import (
    BundleRefused,
    ExtractionProfileRevision,
    HookBinding,
    HookPoint,
)

# G6 (ADR-0017 §6.4): more than this many distinct (hook_point, target) bindings per EPR blocks
# VALIDATED until an architecture review is recorded.
BINDING_CAP = 2


class _CannotParse:
    __slots__ = ()

    def __repr__(self) -> str:
        return "CANNOT_PARSE"


CANNOT_PARSE: Final = _CannotParse()
Hook = Callable[[str], Any]


@dataclass(frozen=True)
class HookManifest:
    supplier_key: str
    hook_revision: str
    hook_fingerprint: str
    hooks: Mapping[str, Hook]


def bind_hooks(
    epr: ExtractionProfileRevision, manifest: HookManifest | None
) -> dict[tuple[HookPoint, str], Hook]:
    """Resolve every binding against the running manifest, or refuse the bundle."""
    bound: dict[tuple[HookPoint, str], Hook] = {}
    for binding in epr.hooks:
        if manifest is None or manifest.supplier_key != epr.supplier_key:
            raise BundleRefused("a bound hook needs its own supplier's running hook manifest")
        if manifest.hook_revision != binding.hook_revision:
            raise BundleRefused("the bound HOOK_REVISION is not the running one; a new EPR is due")
        hook = manifest.hooks.get(binding.hook_name)
        if hook is None:
            raise BundleRefused(f"the manifest has no hook {binding.hook_name!r}")
        bound[(binding.hook_point, binding.target)] = hook
    return bound


def promotion_key(binding: HookBinding) -> tuple[str, str]:
    """The G5 trigger and the G6 counting unit: derived from what the binding does."""
    return (binding.hook_point.value, binding.target)


def promotion_group(binding: HookBinding) -> tuple[str, str, str]:
    """Reported beside the key for the promotion decision; it never suppresses the key."""
    return (binding.hook_point.value, binding.target, binding.format_class)


def exceeds_binding_cap(epr: ExtractionProfileRevision) -> bool:
    return len({promotion_key(binding) for binding in epr.hooks}) > BINDING_CAP


def shared_promotion_keys(
    eprs: Iterable[ExtractionProfileRevision],
) -> dict[tuple[str, str], frozenset[str]]:
    """G5: every promotion key bound by two or more suppliers, with those suppliers."""
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for epr in eprs:
        for binding in epr.hooks:
            owners[promotion_key(binding)].add(epr.supplier_key)
    return {key: frozenset(found) for key, found in owners.items() if len(found) > 1}
