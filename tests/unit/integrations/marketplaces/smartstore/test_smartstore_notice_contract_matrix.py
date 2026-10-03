"""Notice coverage S4: every SmartStore notice child, and one contract matrix over all of them.

For each of the 36 documented children a minimal notice (only what the schema requires) and a
maximal one (every field ICBM may send) are built from the provider notice schema, and the maximal
child's wire form is pinned in ``notice_children.golden.json``. The matrix then applies the same
mutations to every type and every field — a required field removed, a field left to the product
detail, a value of another type, over its bound or in the wrong form, a field that is never sent —
and proves the preflight (metadata rules) and the CREATE projection (wire) agree on every one.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.stages.products.model import ReadinessStatus
from app.stages.register.preparation import FieldValue
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.notice_schema import (
    NoticeFieldSchema,
    NoticePresence,
    NoticeValueType,
    emittable,
    load_notice_schema,
    notice_policy,
)
from tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter import payload
from tests.unit.register.test_m5_preflight_rules import (
    LISTING,
    METADATA,
    candidate,
    request,
    resolved,
)

SCHEMA = load_notice_schema()
DOCUMENTED = sorted(SCHEMA.types)
UNDOCUMENTED = sorted(set(SCHEMA.notice_types) - set(SCHEMA.types))
GOLDEN = Path(__file__).with_name("notice_children.golden.json")
PROVENANCE = "OPERATOR_CONFIRMED"


def _fields(notice_type: str) -> dict[str, NoticeFieldSchema]:
    return {f.name: f for f in SCHEMA.types[notice_type].fields if emittable(f)}


def sample(field: NoticeFieldSchema) -> str | bool | int:
    """A deterministic value of the field's own type, form and bound."""
    value_type = field.value_type
    if value_type is NoticeValueType.YEAR_MONTH:
        return "2026-09"
    if value_type is NoticeValueType.DATE:
        return "2026-09-30"
    if value_type is NoticeValueType.BOOLEAN:
        return False
    if value_type in (NoticeValueType.INTEGER, NoticeValueType.LONG):
        return 1
    text = f"고시 {field.name}"
    return text[: field.max_length] if field.max_length else text


def minimal(notice_type: str) -> dict[str, str | bool | int]:
    """Only what the schema requires: the required fields, the alternative of a conditional pair
    when its other half is absent, and one field of a one-of group."""
    fields = _fields(notice_type)
    chosen = {n: sample(f) for n, f in fields.items() if f.presence is NoticePresence.REQUIRED}
    for name, member in fields.items():
        if member.presence is NoticePresence.REQUIRED_WITHOUT and not any(
            other in chosen for other in member.required_without
        ):
            chosen[name] = sample(member)
    for member in fields.values():
        group = [n for n in member.one_of if n in fields]
        if group and not any(n in chosen for n in group):
            chosen[min(group)] = sample(fields[min(group)])
    return chosen


def maximal(notice_type: str) -> dict[str, str | bool | int]:
    return {name: sample(member) for name, member in _fields(notice_type).items()}


def _snapshot_fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            {"detail_page_reference": True, "provenance": PROVENANCE}
            if value is DETAIL
            else {"value": value, "provenance": PROVENANCE}
        )
        for key, value in values.items()
    }


DETAIL = object()  # a field left to the product detail


def wire_accepts(notice_type: str, values: dict[str, Any]) -> bool:
    try:
        projected = product.project(
            payload(notice={"notice_type": notice_type, "fields": _snapshot_fields(values)})
        )
    except product.WireContractError:
        return False
    return projected.sendable


def preflight_accepts(notice_type: str, values: dict[str, Any]) -> bool:
    notices = {
        key: (
            FieldValue(detail_page_reference=True) if value is DETAIL else FieldValue(value)  # type: ignore[arg-type]
        )
        for key, value in values.items()
    }
    metadata = replace(METADATA, notice=notice_policy(notice_type))
    result = candidate(
        request(listing=replace(LISTING, notices=notices)), resolved(metadata=metadata)
    )
    return result.status is ReadinessStatus.READY


def _agree(notice_type: str, values: dict[str, Any], expected: bool, case: str) -> None:
    assert preflight_accepts(notice_type, values) is expected, (notice_type, case, "preflight")
    assert wire_accepts(notice_type, values) is expected, (notice_type, case, "wire")


# ---------------------------------------------------------------- every child


def test_the_fixture_covers_every_documented_child_and_only_those() -> None:
    assert len(DOCUMENTED) == 36
    assert UNDOCUMENTED == ["AIRLINE_TICKET", "LODGMENT_RESERVATION", "RENT_CAR", "TRAVEL_PACKAGE"]
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == DOCUMENTED


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_the_minimal_and_the_maximal_notice_are_accepted_by_both_layers(notice_type: str) -> None:
    _agree(notice_type, minimal(notice_type), True, "minimal")
    _agree(notice_type, maximal(notice_type), True, "maximal")


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_the_maximal_child_is_the_pinned_wire_form(notice_type: str) -> None:
    projected = product.project(
        payload(
            notice={"notice_type": notice_type, "fields": _snapshot_fields(maximal(notice_type))}
        )
    )
    notice = projected.document.mapping()["originProduct"]["detailAttribute"][
        "productInfoProvidedNotice"
    ]
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert notice == golden[notice_type]


@pytest.mark.parametrize("notice_type", UNDOCUMENTED)
def test_an_undocumented_type_is_blocked_and_stays_a_gap(notice_type: str) -> None:
    metadata = replace(METADATA, notice=notice_policy(notice_type))
    result = candidate(request(listing=replace(LISTING, notices={})), resolved(metadata=metadata))
    assert result.status is ReadinessStatus.BLOCKED
    etc = {"notice_type": notice_type, "fields": _snapshot_fields(minimal("ETC"))}
    projected = product.project(payload(notice=etc))
    assert projected.gaps == (product.GAP_NOTICE_TYPE_CHILD,)


# ---------------------------------------------------------------- the common contract matrix


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_removing_any_field_is_refused_exactly_when_the_schema_requires_it(
    notice_type: str,
) -> None:
    base = minimal(notice_type)
    fields = _fields(notice_type)
    for name in base:
        values = {k: v for k, v in base.items() if k != name}
        member = fields[name]
        # Removal is fine only where the provider fills the field or nothing requires it.
        still_required = member.presence in (
            NoticePresence.REQUIRED,
            NoticePresence.REQUIRED_WITHOUT,
            NoticePresence.ONE_OF,
        )
        _agree(notice_type, values, not still_required, f"remove {name}")


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_a_detail_reference_is_accepted_only_where_the_provider_fills_it(notice_type: str) -> None:
    base = minimal(notice_type)
    for name, member in _fields(notice_type).items():
        provider_fills = member.presence is NoticePresence.DETAIL_REFERENCE_DEFAULT
        if not provider_fills and name not in base:
            continue
        values = {**base, name: DETAIL}
        _agree(notice_type, values, provider_fills, f"detail {name}")


def _wrong_type(member: NoticeFieldSchema) -> object:
    if member.value_type is NoticeValueType.BOOLEAN:
        return "false"
    if member.value_type in (NoticeValueType.INTEGER, NoticeValueType.LONG):
        return "1"
    return True


def _wrong_form(member: NoticeFieldSchema) -> object | None:
    if member.value_type is NoticeValueType.YEAR_MONTH:
        return "2026-9"
    if member.value_type is NoticeValueType.DATE:
        return "2026-02-30"
    if member.value_type is NoticeValueType.TEXT and member.max_length:
        return "x" * (member.max_length + 1)
    if member.value_type is NoticeValueType.INTEGER:
        return 2**31
    if member.value_type is NoticeValueType.LONG:
        return 2**63
    return None


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_a_value_of_another_type_or_outside_its_form_is_refused_by_both(notice_type: str) -> None:
    base = maximal(notice_type)
    for name, member in _fields(notice_type).items():
        _agree(notice_type, {**base, name: _wrong_type(member)}, False, f"type {name}")
        wrong = _wrong_form(member)
        if wrong is not None:
            _agree(notice_type, {**base, name: wrong}, False, f"form {name}")


@pytest.mark.parametrize("notice_type", DOCUMENTED)
def test_a_field_that_is_never_sent_is_refused_by_both(notice_type: str) -> None:
    base = minimal(notice_type)
    never = [f for f in SCHEMA.types[notice_type].fields if not emittable(f)]
    for member in never:
        _agree(notice_type, {**base, member.name: "2026-09"}, False, f"never {member.name}")
    _agree(notice_type, {**base, "notAField": "값"}, False, "unknown field")


def test_another_types_child_is_never_sent() -> None:
    for notice_type in DOCUMENTED:
        other = "ETC" if notice_type != "ETC" else "WEAR"
        foreign = {k: v for k, v in maximal(other).items() if k not in _fields(notice_type)}
        if foreign:
            values = {**minimal(notice_type), **foreign}
            _agree(notice_type, values, False, f"{other} fields")
