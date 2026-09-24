"""Closed hook points, the binding model and the promotion key (ADR-0017 §6).

A hook is a pure function a supplier package would hold. It returns a *candidate* or
``CANNOT_PARSE``; it never emits a status or evidence. The engine validates every candidate
against the field's value model and decides the status itself.
"""

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.collect.facts import SUPPLIED_FIELDS
from prototypes.adaptive_collector.profile import (
    ExtractionProfileRevision,
    HookBinding,
    HookPoint,
)


class _CannotParse:
    def __repr__(self) -> str:
        return "CANNOT_PARSE"


CANNOT_PARSE: Final = _CannotParse()
HookResult = Any  # a candidate mapping/str, or CANNOT_PARSE
Hook = Callable[[str], HookResult]

TARGETS: Mapping[HookPoint, frozenset[str]] = {
    HookPoint.IDENTITY_DECODE: frozenset({"identity"}),
    HookPoint.VALUE_PARSE: frozenset(SUPPLIED_FIELDS),
    HookPoint.OPTION_DECODE: frozenset({"options"}),
    HookPoint.EMBEDDED_DECODE: frozenset({"embedded"}),
}
FORMAT_CLASSES: Mapping[HookPoint, frozenset[str]] = {
    HookPoint.IDENTITY_DECODE: frozenset({"PATH_CODE", "ENCODED_TOKEN", "COMPOSITE_CODE"}),
    HookPoint.VALUE_PARSE: frozenset(
        {"MONEY_TEXT", "QUANTITY_TEXT", "CONDITIONAL_POLICY_TEXT", "LABELLED_TEXT"}
    ),
    HookPoint.OPTION_DECODE: frozenset({"SELECT_CONTROL", "BUTTON_GROUP", "SCRIPT_MATRIX"}),
    HookPoint.EMBEDDED_DECODE: frozenset({"KEY_VALUE_BLOCK", "SCRIPT_ASSIGNMENT"}),
}
# G6: more than this many distinct (hook_point, target) bindings blocks VALIDATED.
G6_BINDING_CAP = 2


class HookRefused(ValueError):
    pass


@dataclass(frozen=True)
class HookManifest:
    """A supplier's hook manifest: semantic ``HOOK_REVISION`` and implementation fingerprint."""

    supplier_key: str
    hook_revision: str
    hook_fingerprint: str
    hooks: Mapping[str, Hook]


def fingerprint_files(paths: Iterable[Path], root: Path) -> str:
    """The ADR-0010 §12 encoding: path, NUL, hex SHA-256 of CRLF→LF bytes, newline; sorted."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        data = path.read_bytes().replace(b"\r\n", b"\n")
        name = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(name + b"\0" + hashlib.sha256(data).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def check_binding(binding: HookBinding) -> None:
    if binding.target not in TARGETS[binding.hook_point]:
        raise HookRefused(f"{binding.hook_point.value} cannot target {binding.target!r}")
    if binding.format_class not in FORMAT_CLASSES[binding.hook_point]:
        raise HookRefused(f"{binding.format_class!r} is not a format class of {binding.hook_point}")


def resolve_hooks(
    epr: ExtractionProfileRevision, manifest: HookManifest | None
) -> dict[tuple[HookPoint, str], Hook]:
    """Every binding must resolve to a running manifest with the bound ``HOOK_REVISION``."""
    resolved: dict[tuple[HookPoint, str], Hook] = {}
    for binding in epr.hooks:
        check_binding(binding)
        if manifest is None or manifest.supplier_key != epr.supplier_key:
            raise HookRefused("a bound hook needs its supplier's running hook manifest")
        if manifest.hook_revision != binding.hook_revision:
            raise HookRefused("the bound HOOK_REVISION is not the running one; a new EPR is needed")
        hook = manifest.hooks.get(binding.hook_name)
        if hook is None:
            raise HookRefused(f"no hook {binding.hook_name!r} in the manifest")
        key = (binding.hook_point, binding.target)
        if key in resolved:
            raise HookRefused("one binding per (hook_point, target)")
        resolved[key] = hook
    return resolved


def promotion_key(binding: HookBinding) -> tuple[str, str]:
    return (binding.hook_point.value, binding.target)


def promotion_group(binding: HookBinding) -> tuple[str, str, str]:
    return (binding.hook_point.value, binding.target, binding.format_class)


def g6_exceeded(epr: ExtractionProfileRevision) -> bool:
    return len({promotion_key(b) for b in epr.hooks}) > G6_BINDING_CAP


def promotion_review(eprs: Iterable[ExtractionProfileRevision]) -> dict[tuple[str, str], set[str]]:
    """G5: every promotion key shared by bindings of two or more suppliers. Derived, never
    cleared by an operator; ``format_class`` groups the evidence but never suppresses a key."""
    suppliers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for epr in eprs:
        for binding in epr.hooks:
            suppliers[promotion_key(binding)].add(epr.supplier_key)
    return {key: owners for key, owners in suppliers.items() if len(owners) > 1}
