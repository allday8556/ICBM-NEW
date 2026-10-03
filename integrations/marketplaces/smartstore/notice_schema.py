"""The SmartStore provider notice schema as data (notice coverage S1, owner directive 2026-10-03).

``notice_schema.json`` beside this module is the 상품정보제공고시 contract of every notice type: the
child that carries it and, for every field of that child, its value type, presence rule, length
bound and deprecation. It is not written by hand. :func:`derive_notice_schema` derives it,
mechanically and only, from two retained evidence files (``documents/evidence/marketplace-apis/``,
inventory and every disagreement in ``NOTICE_SMARTSTORE_S0.md``):

* the current 2.90.0 ``원상품 정보 구조체`` reference
  (``smartstore-notice-schema-2.90.0-2026-10-03.json``). It owns the type → child mapping, the
  membership of every child, the wire type and form, the ``required`` badge, ``deprecated``, the
  length bound and the documented omit semantics;
* the live notice capture (``smartstore-notice-capture-2026-10-03.json``). Every field it lists must
  agree with the reference on value type and length bound. A value-type disagreement leaves that
  field's type unconfirmed instead of choosing a side; a length disagreement refuses the derivation.

``required`` is the provider's badge and is not "the operator must type a value". The presence rule
keeps the two apart: a required field the provider fills with "상품상세 참조" when it is omitted is
:attr:`NoticePresence.DETAIL_REFERENCE_DEFAULT`, never :attr:`NoticePresence.REQUIRED`.

Nothing here is guessed. A notice type without a documented child is absent from :attr:`types`, a
field without a confirmed value type carries ``value_type=None`` and the reason, and nothing falls
back to another type. This module is pure: :func:`load_notice_schema` reads the packaged file, and
neither function touches a provider. The CREATE projection does not consume it yet (S3).
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

NOTICE_SCHEMA_FILE: Final = "notice_schema.json"
NOTICE_SCHEMA_DATA_REVISION: Final = "smartstore-notice-schema/2.90.0-r1"
NOTICE_SCHEMA_API_VERSION: Final = "2.90.0"


class NoticeValueType(StrEnum):
    """The value type of one notice field, from the reference's wire type and form."""

    TEXT = "TEXT"
    YEAR_MONTH = "YEAR_MONTH"  # string, the documented 'yyyy-MM' form
    DATE = "DATE"  # string<date>, 'yyyy-MM-dd'
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"  # integer<int32>
    LONG = "LONG"  # integer<int64>


class NoticePresence(StrEnum):
    """When a notice field must be sent: exactly one rule per field, read from the reference."""

    # Required, and the provider has no value of its own for it.
    REQUIRED = "REQUIRED"
    # Required, but "미입력 시 상품상세 참조로 입력됩니다": omitting it is the provider's own
    # default.
    DETAIL_REFERENCE_DEFAULT = "DETAIL_REFERENCE_DEFAULT"
    OPTIONAL = "OPTIONAL"
    # "미입력 시 <value>로 설정됩니다": omitting it is the provider's stated default value.
    PROVIDER_DEFAULT = "PROVIDER_DEFAULT"
    # "<other>를 입력하지 않은 경우에는 필수": required when every field it names is absent.
    REQUIRED_WITHOUT = "REQUIRED_WITHOUT"
    # "<a>, <b>, <c> 셋 중 하나는 필수": at least one field of the group is sent.
    ONE_OF = "ONE_OF"
    # "해당 사항이 없으면 이 요소를 삭제하고 전송합니다": sent when it applies, omitted when not.
    IF_APPLICABLE = "IF_APPLICABLE"


# The presence rules a field carries exactly when the reference badges it ``required``.
PROVIDER_REQUIRED_PRESENCE: Final = frozenset(
    {NoticePresence.REQUIRED, NoticePresence.DETAIL_REFERENCE_DEFAULT}
)


class NoticeSchemaError(ValueError):
    """The notice schema data, or the evidence it is derived from, is not self-consistent."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class NoticeFieldSchema:
    """One field of one notice child.

    ``provider_required`` is the reference's own ``required`` badge; ``presence`` is what it means
    for sending. ``value_type`` is ``None`` only when the sources disagree, and ``unconfirmed`` then
    says why: such a field has no confirmed wire form.
    """

    name: str
    label: str
    value_type: NoticeValueType | None
    presence: NoticePresence
    provider_required: bool
    max_length: int | None
    deprecated: bool
    listed_live: bool
    required_without: tuple[str, ...] = ()
    one_of: tuple[str, ...] = ()
    provider_default: str | None = None
    unconfirmed: str | None = None


@dataclass(frozen=True)
class NoticeTypeSchema:
    """One notice type and the documented child that carries it."""

    notice_type: str
    name: str
    child: str
    listed_live: bool
    fields: tuple[NoticeFieldSchema, ...]

    def field(self, name: str) -> NoticeFieldSchema | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True)
class ProviderNoticeSchema:
    """The whole provider notice contract: the full type enumeration and every documented child."""

    revision: str
    api_version: str
    notice_types: tuple[str, ...]
    types: Mapping[str, NoticeTypeSchema]


# ----------------------------------------------------------------- derivation from the evidence

_DETAIL_REFERENCE: Final = "미입력 시 상품상세 참조로 입력됩니다"
_IF_APPLICABLE: Final = "해당 사항이 없으면 이 요소를 삭제하고 전송합니다"
_PROVIDER_DEFAULT: Final = re.compile(r"미입력 시 ([A-Za-z0-9_]+)로 설정됩니다")
_REQUIRED_WITHOUT: Final = re.compile(
    r"([A-Za-z]+(?:, [A-Za-z]+)*)[를을] 입력하지 않은 경우(?:에는)? 필수"
)
_ONE_OF: Final = re.compile(r"([A-Za-z]+(?:, [A-Za-z]+)+) 셋 중 하나는 필수")
_YEAR_MONTH_FORM: Final = "'yyyy-MM' 형식 입력"
_LIVE_VALUE_TYPES: Final[Mapping[str, NoticeValueType]] = MappingProxyType(
    {
        "String": NoticeValueType.TEXT,
        "YearMonth": NoticeValueType.YEAR_MONTH,
        "LocalDate": NoticeValueType.DATE,
        "Boolean": NoticeValueType.BOOLEAN,
        "Integer": NoticeValueType.INTEGER,
        "Long": NoticeValueType.LONG,
    }
)


def _reference_value_type(field: Mapping[str, Any]) -> NoticeValueType | None:
    wire, form = field["wire_type"], field.get("type_note")
    if wire == "string":
        if form == "date":
            return NoticeValueType.DATE
        return NoticeValueType.YEAR_MONTH if form == _YEAR_MONTH_FORM else NoticeValueType.TEXT
    if wire == "boolean":
        return NoticeValueType.BOOLEAN
    if wire == "integer" and form in ("int32", "int64"):
        return NoticeValueType.INTEGER if form == "int32" else NoticeValueType.LONG
    return None


def _names(group: str) -> tuple[str, ...]:
    return tuple(group.split(", "))


def _derive_type(
    notice_type: str,
    child: str,
    fields: Sequence[Mapping[str, Any]],
    live: Mapping[str, Any] | None,
) -> dict[str, Any]:
    listed = {f["fieldName"]: f for f in (live or {}).get("productInfoProvidedNoticeContents", [])}
    names = [f["name"] for f in fields]
    unknown = sorted(set(listed) - set(names))
    if unknown:
        raise NoticeSchemaError("NOTICE_LIVE_FIELD_UNDOCUMENTED", f"{notice_type}: {unknown}")
    # A one-of statement names its whole group; every member of it is in the group, including a
    # member whose own description does not repeat the statement.
    groups: dict[str, tuple[str, ...]] = {}
    for f in fields:
        text = " ".join(x for x in (f.get("type_note"), f.get("description")) if x)
        match = _ONE_OF.search(text)
        if match:
            for member in _names(match.group(1)):
                groups[member] = _names(match.group(1))
    out = []
    for f in fields:
        text = " ".join(x for x in (f.get("type_note"), f.get("description")) if x)
        record: dict[str, Any] = {"name": f["name"], "label": f.get("label") or f["name"]}
        rules: list[NoticePresence] = []
        if _DETAIL_REFERENCE in text:
            rules.append(NoticePresence.DETAIL_REFERENCE_DEFAULT)
        default = _PROVIDER_DEFAULT.search(text)
        if default:
            rules.append(NoticePresence.PROVIDER_DEFAULT)
            record["provider_default"] = default.group(1)
        without = _REQUIRED_WITHOUT.search(text)
        if without:
            rules.append(NoticePresence.REQUIRED_WITHOUT)
            record["required_without"] = list(_names(without.group(1)))
        if f["name"] in groups:
            rules.append(NoticePresence.ONE_OF)
            record["one_of"] = list(groups[f["name"]])
        if _IF_APPLICABLE in text:
            rules.append(NoticePresence.IF_APPLICABLE)
        if not rules:
            rules.append(NoticePresence.REQUIRED if f["required"] else NoticePresence.OPTIONAL)
        if len(rules) != 1:
            raise NoticeSchemaError("NOTICE_PRESENCE_AMBIGUOUS", f"{notice_type}.{f['name']}")
        value_type = _reference_value_type(f)
        unconfirmed = None
        live_field = listed.get(f["name"])
        if live_field is not None:
            live_type = _LIVE_VALUE_TYPES.get(live_field["fieldType"])
            if live_field.get("fieldMaxLength") != f.get("max_length"):
                raise NoticeSchemaError("NOTICE_LENGTH_DISAGREES", f"{notice_type}.{f['name']}")
            if value_type is None or live_type != value_type:
                unconfirmed = (
                    f"the reference documents {f['wire_type']}"
                    f"{'<' + f['type_note'] + '>' if f.get('type_note') else ''}"
                    f" while the live capture lists {live_field['fieldType']}"
                )
                value_type = None
        elif value_type is None:
            unconfirmed = f"the reference documents {f['wire_type']} and no live field lists it"
        record.update(
            {
                "value_type": value_type.value if value_type else None,
                "presence": rules[0].value,
                "provider_required": bool(f["required"]),
                "max_length": f.get("max_length"),
                "deprecated": bool(f["deprecated"]),
                "listed_live": live_field is not None,
            }
        )
        if unconfirmed:
            record["unconfirmed"] = unconfirmed
        out.append(record)
    return {
        "name": (live or {}).get("productInfoProvidedNoticeTypeName") or child,
        "child": child,
        "listed_live": live is not None,
        "fields": out,
    }


def derive_notice_schema(
    reference: Mapping[str, Any], live_capture: Mapping[str, Any]
) -> dict[str, Any]:
    """The notice schema document of the two evidence files. Pure and deterministic."""
    source = reference["source"]
    if source.get("api_version") != NOTICE_SCHEMA_API_VERSION:
        raise NoticeSchemaError("NOTICE_REFERENCE_VERSION", str(source.get("api_version")))
    live_types = live_capture["types"]
    listed = [item["productInfoProvidedNoticeType"] for item in live_capture["list"]["items"]]
    if set(listed) != set(live_types):
        raise NoticeSchemaError("NOTICE_LIVE_CAPTURE_INCOMPLETE", "list and types differ")
    mapping = reference["type_to_child"]
    enum = list(reference["notice_types"])
    stray = sorted((set(mapping) | set(listed)) - set(enum))
    if stray:
        raise NoticeSchemaError("NOTICE_TYPE_NOT_ENUMERATED", str(stray))
    undocumented = sorted(set(listed) - set(mapping))
    if undocumented:
        raise NoticeSchemaError("NOTICE_LIVE_TYPE_UNDOCUMENTED", str(undocumented))
    types = {}
    for notice_type in enum:
        child = mapping.get(notice_type)
        if child is None:
            continue
        fields = reference["children"][child]["fields"]
        live = live_types.get(notice_type)
        if live is not None:
            live = {
                **live,
                "productInfoProvidedNoticeTypeName": _type_name(live_capture, notice_type),
            }
        types[notice_type] = _derive_type(notice_type, child, fields, live)
    return {
        "revision": NOTICE_SCHEMA_DATA_REVISION,
        "api_version": NOTICE_SCHEMA_API_VERSION,
        "notice_types": enum,
        "types": types,
    }


def render_notice_schema(document: Mapping[str, Any]) -> str:
    """The canonical text of a notice schema document: one line per field, so a change of the
    contract is one readable diff line."""

    def line(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))

    out = ["{"]
    for key in ("revision", "api_version", "notice_types"):
        out.append(f" {line(key)}: {line(document[key])},")
    out.append(' "types": {')
    entries = list(document["types"].items())
    for index, (notice_type, raw) in enumerate(entries):
        out.append(f"  {line(notice_type)}: {{")
        for key in ("name", "child", "listed_live"):
            out.append(f"   {line(key)}: {line(raw[key])},")
        out.append('   "fields": [')
        fields = raw["fields"]
        for position, field in enumerate(fields):
            comma = "," if position < len(fields) - 1 else ""
            out.append(f"    {line(field)}{comma}")
        out.append("   ]")
        out.append("  }" + ("," if index < len(entries) - 1 else ""))
    out.append(" }")
    out.append("}")
    return "\n".join(out) + "\n"


def _type_name(live_capture: Mapping[str, Any], notice_type: str) -> str | None:
    for item in live_capture["list"]["items"]:
        if item["productInfoProvidedNoticeType"] == notice_type:
            name = item.get("productInfoProvidedNoticeTypeName")
            return name if isinstance(name, str) else None
    return None


# ----------------------------------------------------------------- the packaged data


def _field(notice_type: str, raw: Mapping[str, Any], names: frozenset[str]) -> NoticeFieldSchema:
    where = f"{notice_type}.{raw.get('name')}"
    try:
        presence = NoticePresence(raw["presence"])
        value_type = None if raw["value_type"] is None else NoticeValueType(raw["value_type"])
    except (KeyError, ValueError) as exc:
        raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", where) from exc
    item = NoticeFieldSchema(
        name=str(raw["name"]),
        label=str(raw["label"]),
        value_type=value_type,
        presence=presence,
        provider_required=raw["provider_required"] is True,
        max_length=raw.get("max_length"),
        deprecated=raw["deprecated"] is True,
        listed_live=raw["listed_live"] is True,
        required_without=tuple(raw.get("required_without", ())),
        one_of=tuple(raw.get("one_of", ())),
        provider_default=raw.get("provider_default"),
        unconfirmed=raw.get("unconfirmed"),
    )
    if item.provider_required != (presence in PROVIDER_REQUIRED_PRESENCE):
        raise NoticeSchemaError("NOTICE_SCHEMA_PRESENCE_MISMATCH", where)
    if bool(item.required_without) != (presence is NoticePresence.REQUIRED_WITHOUT):
        raise NoticeSchemaError("NOTICE_SCHEMA_PRESENCE_MISMATCH", where)
    if bool(item.one_of) != (presence is NoticePresence.ONE_OF):
        raise NoticeSchemaError("NOTICE_SCHEMA_PRESENCE_MISMATCH", where)
    if (item.provider_default is not None) != (presence is NoticePresence.PROVIDER_DEFAULT):
        raise NoticeSchemaError("NOTICE_SCHEMA_PRESENCE_MISMATCH", where)
    if (item.value_type is None) != (item.unconfirmed is not None):
        raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", f"{where}: unconfirmed type")
    if not set(item.required_without) <= names or not set(item.one_of) <= names:
        raise NoticeSchemaError("NOTICE_SCHEMA_DANGLING_REFERENCE", where)
    if item.one_of and item.name not in item.one_of:
        raise NoticeSchemaError("NOTICE_SCHEMA_DANGLING_REFERENCE", where)
    if item.max_length is not None and (
        not isinstance(item.max_length, int) or item.max_length < 1
    ):
        raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", f"{where}: max_length")
    return item


def parse_notice_schema(document: Mapping[str, Any]) -> ProviderNoticeSchema:
    """The typed schema of one notice schema document, refused unless it is self-consistent."""
    if document.get("revision") != NOTICE_SCHEMA_DATA_REVISION:
        raise NoticeSchemaError("NOTICE_SCHEMA_REVISION", str(document.get("revision")))
    enum = tuple(document["notice_types"])
    if len(set(enum)) != len(enum):
        raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", "a notice type repeats")
    types: dict[str, NoticeTypeSchema] = {}
    children: set[str] = set()
    for notice_type, raw in document["types"].items():
        if notice_type not in enum:
            raise NoticeSchemaError("NOTICE_TYPE_NOT_ENUMERATED", notice_type)
        child = str(raw["child"])
        if child in children:
            raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", f"child {child} repeats")
        children.add(child)
        names = frozenset(str(f["name"]) for f in raw["fields"])
        if len(names) != len(raw["fields"]):
            raise NoticeSchemaError("NOTICE_SCHEMA_MALFORMED", f"{notice_type}: a field repeats")
        types[notice_type] = NoticeTypeSchema(
            notice_type=notice_type,
            name=str(raw["name"]),
            child=child,
            listed_live=raw["listed_live"] is True,
            fields=tuple(_field(notice_type, f, names) for f in raw["fields"]),
        )
    return ProviderNoticeSchema(
        revision=NOTICE_SCHEMA_DATA_REVISION,
        api_version=str(document["api_version"]),
        notice_types=enum,
        types=MappingProxyType(types),
    )


@cache
def load_notice_schema() -> ProviderNoticeSchema:
    """The packaged provider notice schema. The file is immutable package data."""
    path = Path(__file__).with_name(NOTICE_SCHEMA_FILE)
    return parse_notice_schema(json.loads(path.read_text(encoding="utf-8")))
