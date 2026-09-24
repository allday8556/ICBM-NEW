"""`ExtractionProfileRevision` (EPR) and `PageTemplateRevision` (PTR) — ADR-0017 §3, §5, §6.

Both are strict, frozen value models: ``extra="forbid"``, no coercion, every locator compiled by the
bounded grammar. A profile says **where** a source states a fact, never **what** it is, so it has
no place for a host, a URL, a limit, a credential, a status, a value or a new field.

Identity is content: a revision's digest is SHA-256 over its canonical serialization under the
``icbm-profile/v1`` scheme. An EPR pins its PTRs by digest, so its own digest covers the whole
bundle. ``resolve_bundle`` recomputes every digest it is given and refuses the bundle on any
mismatch; P1 has no store, so nothing here persists a revision.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.collect.adaptive.canonical import canonical_json, digest, length_prefixed, parse_json
from app.collect.adaptive.locator import compile_locator
from app.collect.facts import SUPPLIED_FIELDS

SCHEMA_VERSION = "icbm-profile/v1"
PROFILE_DIGEST_SCHEME = "icbm-profile/v1"
SEMANTICS_SCHEME = b"icbm-extraction-semantics/v1"

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
LocatorText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PathStep = Annotated[str, StringConstraints(min_length=1, max_length=40)]
# Fields a template's rules address. Stock, options and images have dedicated rules.
RULED_FIELDS = frozenset(SUPPLIED_FIELDS) - {"stock", "options"}


class HookPoint(StrEnum):
    """Closed (ADR-0017 §6.1); a new point needs an amendment of the ADR."""

    IDENTITY_DECODE = "identity_decode"
    VALUE_PARSE = "value_parse"
    OPTION_DECODE = "option_decode"
    EMBEDDED_DECODE = "embedded_decode"


HOOK_TARGETS: Mapping[HookPoint, frozenset[str]] = {
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


class _Frozen(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def _compiled(*texts: str | None) -> None:
    for text in texts:
        if text is not None:
            compile_locator(text)


# ---------------------------------------------------------------- where a fact is stated


class TextLocation(_Frozen):
    kind: Literal["TEXT"]
    locator: LocatorText

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(self.locator)
        return self


class AttributeLocation(_Frozen):
    kind: Literal["ATTRIBUTE"]
    locator: LocatorText
    attribute: Key

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(self.locator)
        return self


class LabelledRowLocation(_Frozen):
    """Table rows inside ``container`` whose first cell is one of the EPR ``vocabulary`` labels."""

    kind: Literal["LABELLED_ROW"]
    container: LocatorText
    vocabulary: Key

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(self.container)
        return self


class EmbeddedLocation(_Frozen):
    """A path into admissible embedded data: a JSON-LD object of one ``@type``, or one named
    pure-literal assignment."""

    kind: Literal["EMBEDDED"]
    source: Literal["JSON_LD", "ASSIGNMENT"]
    json_ld_type: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None
    assignment: Annotated[str, StringConstraints(min_length=1, max_length=60)] | None = None
    path: tuple[PathStep, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _addressed(self) -> Self:
        if (self.source == "JSON_LD") != (self.json_ld_type is not None):
            raise ValueError("a JSON_LD location names its @type, and only it does")
        if (self.source == "ASSIGNMENT") != (self.assignment is not None):
            raise ValueError("an ASSIGNMENT location names its assignment, and only it does")
        return self


Location = Annotated[
    TextLocation | AttributeLocation | LabelledRowLocation | EmbeddedLocation,
    Field(discriminator="kind"),
]


class FieldRule(_Frozen):
    primary: Location
    alternatives: tuple[Location, ...] = Field(default=(), max_length=3)
    cardinality: Literal["ONE", "MANY"] = "ONE"
    # The rule's own absence condition: this anchor present and no location hitting proves the
    # source does not state the fact (ABSENT). Without it, a miss is never ABSENT (ADR-0017 §8.2).
    absent_when: LocatorText | None = None

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(self.absent_when)
        return self


class Signature(_Frozen):
    required: tuple[LocatorText, ...] = Field(min_length=1, max_length=8)
    forbidden: tuple[LocatorText, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(*self.required, *self.forbidden)
        return self


class ImageRegion(_Frozen):
    name: Key
    locator: LocatorText

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _compiled(self.locator)
        return self


class PageTemplateRevision(_Frozen):
    schema_version: Literal["icbm-profile/v1"]
    kind: Literal["PAGE_TEMPLATE"]
    supplier_key: Key
    template_key: Key
    signature: Signature
    fields: dict[str, FieldRule]
    stock_scope: LocatorText
    options_container: LocatorText
    image_regions: tuple[ImageRegion, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if unknown := sorted(set(self.fields) - RULED_FIELDS):
            raise ValueError(f"a template rules only registry fields; unknown {unknown}")
        _compiled(self.stock_scope, self.options_container)
        names = [region.name for region in self.image_regions]
        if len(names) != len(set(names)):
            raise ValueError("image region names are distinct")
        return self


# ---------------------------------------------------------------- the supplier's interpretation


class HookBinding(_Frozen):
    hook_point: HookPoint
    target: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    format_class: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    hook_name: Key
    hook_revision: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if self.target not in HOOK_TARGETS[self.hook_point]:
            raise ValueError(f"{self.hook_point.value} cannot target {self.target!r}")
        if self.format_class not in FORMAT_CLASSES[self.hook_point]:
            raise ValueError(f"{self.format_class!r} is not a {self.hook_point.value} format class")
        return self


class IdentityRule(_Frozen):
    sources: tuple[Location, ...] = Field(min_length=1, max_length=3)
    agreement: Literal["ALL_AGREE"] = "ALL_AGREE"


class ImageRoleRule(_Frozen):
    region: Key
    role: Literal["REPRESENTATIVE", "DETAIL"]
    take: Literal["FIRST", "ALL"]


class ExtractionProfileRevision(_Frozen):
    schema_version: Literal["icbm-profile/v1"]
    kind: Literal["EXTRACTION_PROFILE"]
    supplier_key: Key
    identity: IdentityRule
    vocabularies: dict[Key, tuple[Label, ...]]
    purchase_controls: tuple[LocatorText, ...] = Field(min_length=1, max_length=6)
    sold_out_words: tuple[Label, ...] = Field(min_length=1, max_length=12)
    image_roles: tuple[ImageRoleRule, ...] = Field(min_length=1, max_length=6)
    templates: tuple[Digest, ...] = Field(min_length=1, max_length=8)
    hooks: tuple[HookBinding, ...] = Field(default=(), max_length=8)
    # Statements the bundle explicitly disposes of for the V3a conflict scan (ADR-0017 §7.2).
    dispositions: tuple[LocatorText, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if len(set(self.templates)) != len(self.templates):
            raise ValueError("an EPR pins each template once")
        points = [(b.hook_point, b.target) for b in self.hooks]
        if len(set(points)) != len(points):
            raise ValueError("one binding per (hook_point, target)")
        _compiled(*self.purchase_controls, *self.dispositions)
        return self


Profile = ExtractionProfileRevision | PageTemplateRevision


def profile_digest(profile: Profile) -> str:
    return digest(PROFILE_DIGEST_SCHEME, profile.model_dump(mode="json"))


def profile_document(profile: Profile) -> str:
    """The canonical text a revision is exchanged and digested as."""
    return canonical_json(profile.model_dump(mode="json"))


# ---------------------------------------------------------------- semantic identity (§5.2)


def semantic_tuple(extractor_revision: str, epr_digest: str) -> tuple[str, str, str, str]:
    """Semantic parts only: no implementation fingerprint of the engine or of any hook."""
    return ("ADAPTIVE", extractor_revision, SCHEMA_VERSION, epr_digest)


def extraction_semantics_id(semantics: tuple[str, ...]) -> str:
    """The stored, collision-resistant form of the tuple: domain-separated and length-prefixed.
    It is never the comparability decision by itself (ADR-0017 §5.3)."""
    return hashlib.sha256(SEMANTICS_SCHEME + length_prefixed(semantics)).hexdigest()


# ---------------------------------------------------------------- a bundle


class BundleRefused(ValueError):
    """A bundle that cannot be trusted is refused whole; nothing is extracted with it."""


@dataclass(frozen=True)
class Bundle:
    epr: ExtractionProfileRevision
    epr_digest: str
    templates: tuple[tuple[str, PageTemplateRevision], ...]  # (digest, template), pinned order


def _parse(document: str, model: type[Profile]) -> Profile:
    parse_json(document)  # refuses non-finite numbers before the model sees the text
    return model.model_validate_json(document)


def resolve_bundle(epr_document: str, template_documents: Mapping[str, str]) -> Bundle:
    """Parse an EPR and the PTRs it pins, recomputing every digest. Any missing template, digest
    mismatch, foreign supplier or unknown vocabulary refuses the bundle."""
    epr = _parse(epr_document, ExtractionProfileRevision)
    assert isinstance(epr, ExtractionProfileRevision)
    templates: list[tuple[str, PageTemplateRevision]] = []
    for pinned in epr.templates:
        text = template_documents.get(pinned)
        if text is None:
            raise BundleRefused(f"the EPR pins a template that is not supplied: {pinned[:12]}")
        template = _parse(text, PageTemplateRevision)
        assert isinstance(template, PageTemplateRevision)
        if profile_digest(template) != pinned:
            raise BundleRefused(f"a template does not recompute to its pinned digest {pinned[:12]}")
        if template.supplier_key != epr.supplier_key:
            raise BundleRefused("a template is never shared across suppliers")
        for rule in template.fields.values():
            for location in (rule.primary, *rule.alternatives):
                if (
                    isinstance(location, LabelledRowLocation)
                    and location.vocabulary not in epr.vocabularies
                ):
                    raise BundleRefused(f"unknown vocabulary {location.vocabulary!r}")
        templates.append((pinned, template))
    return Bundle(epr, profile_digest(epr), tuple(templates))
