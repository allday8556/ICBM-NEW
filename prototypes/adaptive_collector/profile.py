"""`ExtractionProfileRevision` and `PageTemplateRevision`: strict, immutable, content-addressed
(ADR-0017 §3, §5.2).

A profile says **where** a source states a fact, never **what** it is. The schema is closed
(``extra="forbid"``), so a host, a URL, a limit, a credential, a new field or a status cannot be
written into one. Revisions are stored by digest in an append-only store; the digest is recomputed
on every load and a mismatch refuses the bundle.
"""

import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.collect.facts import SUPPLIED_FIELDS
from prototypes.adaptive_collector.locators import parse_selector

SCHEMA_VERSION = "icbm-profile/v1"
DIGEST_SCHEME = "icbm-profile/v1"
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
SelectorText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# The fields a PTR's text-like rules may address; stock, options and images have their own rules.
RULE_FIELDS = frozenset(SUPPLIED_FIELDS) - {"stock", "options"}


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def _selector(text: str) -> str:
    parse_selector(text)
    return text


class TextRule(Strict):
    kind: Literal["TEXT"]
    selector: SelectorText

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _selector(self.selector)
        return self


class AttributeRule(Strict):
    kind: Literal["ATTRIBUTE"]
    selector: SelectorText
    attribute: Name

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _selector(self.selector)
        return self


class LabelRowRule(Strict):
    """Rows of ``container`` whose first cell is a label in the EPR vocabulary ``vocabulary``."""

    kind: Literal["LABEL_ROW"]
    container: SelectorText
    vocabulary: Name

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        _selector(self.container)
        return self


class EmbeddedRule(Strict):
    """A path into admissible embedded data: a JSON-LD block of ``ld_type``, or an assignment."""

    kind: Literal["EMBEDDED"]
    block: Literal["JSON_LD", "ASSIGNMENT"]
    ld_type: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None
    assignment: Annotated[str, StringConstraints(min_length=1, max_length=60)] | None = None
    path: tuple[Annotated[str, StringConstraints(min_length=1, max_length=40)], ...] = Field(
        min_length=1, max_length=6
    )

    @model_validator(mode="after")
    def _addressed(self) -> Self:
        if (self.block == "JSON_LD") != (self.ld_type is not None):
            raise ValueError("a JSON_LD rule names its @type, and only it does")
        if (self.block == "ASSIGNMENT") != (self.assignment is not None):
            raise ValueError("an ASSIGNMENT rule names its assignment, and only it does")
        return self


Rule = Annotated[
    TextRule | AttributeRule | LabelRowRule | EmbeddedRule, Field(discriminator="kind")
]


class FieldRule(Strict):
    primary: Rule
    alternatives: tuple[Rule, ...] = Field(default=(), max_length=3)
    cardinality: Literal["ONE", "MANY"] = "ONE"
    # The rule's own absence condition: when this anchor is present and no rule hits, the source
    # proves the fact is not stated (ABSENT). Without it, a miss is never ABSENT.
    absent_when_present: SelectorText | None = None

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.absent_when_present is not None:
            _selector(self.absent_when_present)
        return self


class StockRule(Strict):
    scope: SelectorText


class OptionsRule(Strict):
    container: SelectorText


class ImageRegion(Strict):
    name: Name
    selector: SelectorText


class Signature(Strict):
    required: tuple[SelectorText, ...] = Field(min_length=1, max_length=8)
    forbidden: tuple[SelectorText, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        for text in (*self.required, *self.forbidden):
            _selector(text)
        return self


class PageTemplateRevision(Strict):
    schema_version: Literal["icbm-profile/v1"]
    kind: Literal["PAGE_TEMPLATE"]
    supplier_key: Name
    template_key: Name
    signature: Signature
    fields: dict[str, FieldRule]
    stock: StockRule
    options: OptionsRule
    image_regions: tuple[ImageRegion, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def _known_fields(self) -> Self:
        if unknown := set(self.fields) - RULE_FIELDS:
            raise ValueError(
                f"a template addresses only registry fields; unknown: {sorted(unknown)}"
            )
        names = [region.name for region in self.image_regions]
        if len(set(names)) != len(names):
            raise ValueError("image region names are distinct")
        return self


class HookPoint(StrEnum):
    """Closed (ADR-0017 §6.1). A new hook point needs an amendment of the ADR."""

    IDENTITY_DECODE = "identity_decode"
    VALUE_PARSE = "value_parse"
    OPTION_DECODE = "option_decode"
    EMBEDDED_DECODE = "embedded_decode"


class HookBinding(Strict):
    hook_point: HookPoint
    target: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    format_class: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    hook_name: Name
    hook_revision: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class StockVocabulary(Strict):
    purchase_controls: tuple[SelectorText, ...] = Field(min_length=1, max_length=6)
    sold_out_words: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        for text in self.purchase_controls:
            _selector(text)
        return self


class IdentityRule(Strict):
    sources: tuple[Rule, ...] = Field(min_length=1, max_length=3)
    agreement: Literal["ALL_AGREE"] = "ALL_AGREE"


class ImageRoleRule(Strict):
    region: Name
    role: Literal["REPRESENTATIVE", "DETAIL"]
    take: Literal["FIRST", "ALL"]


class ExtractionProfileRevision(Strict):
    schema_version: Literal["icbm-profile/v1"]
    kind: Literal["EXTRACTION_PROFILE"]
    supplier_key: Name
    identity: IdentityRule
    vocabularies: dict[Name, tuple[Annotated[str, StringConstraints(min_length=1)], ...]]
    stock: StockVocabulary
    image_roles: tuple[ImageRoleRule, ...] = Field(max_length=6)
    templates: tuple[Digest, ...] = Field(min_length=1, max_length=8)
    hooks: tuple[HookBinding, ...] = Field(default=(), max_length=8)
    # Statements the bundle explicitly disposes of for the V3a conflict scan (ADR-0017 §7.2).
    dispositions: tuple[SelectorText, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if len(set(self.templates)) != len(self.templates):
            raise ValueError("an EPR pins each template once")
        for text in self.dispositions:
            _selector(text)
        return self


Profile = ExtractionProfileRevision | PageTemplateRevision


# ---------------------------------------------------------------- digests


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def profile_digest(profile: Profile) -> str:
    payload = {"scheme": DIGEST_SCHEME, "profile": profile.model_dump(mode="json")}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _lp(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">I", len(data)) + data


def semantic_tuple(engine_revision: str, epr_digest: str) -> tuple[str, str, str, str]:
    """ADR-0017 §5.2: semantic parts only; no implementation fingerprint of any kind."""
    return ("ADAPTIVE", engine_revision, SCHEMA_VERSION, epr_digest)


def extraction_semantics_id(semantics: tuple[str, str, str, str]) -> str:
    """The stored, collision-resistant digest of the tuple: domain-separated, length-prefixed."""
    body = b"icbm-extraction-semantics/v1" + b"".join(_lp(part) for part in semantics)
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------- the store


class BundleRefused(ValueError):
    """A bundle that cannot be trusted is refused whole; nothing is extracted with it."""


@dataclass(frozen=True)
class Bundle:
    epr: ExtractionProfileRevision
    epr_digest: str
    templates: Mapping[str, PageTemplateRevision]  # by digest, in the EPR's pinned order


@dataclass
class ProfileStore:
    """Append-only, content-addressed. There is no update and no delete method."""

    _rows: dict[str, str] = field(default_factory=dict)

    def put(self, raw: str | Mapping[str, Any]) -> str:
        document = raw if isinstance(raw, str) else json.dumps(raw)
        kind = json.loads(document).get("kind")
        model: Profile
        if kind == "EXTRACTION_PROFILE":
            model = ExtractionProfileRevision.model_validate_json(document)
        elif kind == "PAGE_TEMPLATE":
            model = PageTemplateRevision.model_validate_json(document)
        else:
            raise ValueError("a profile is an EXTRACTION_PROFILE or a PAGE_TEMPLATE")
        digest = profile_digest(model)
        stored = canonical(model.model_dump(mode="json"))
        # Same digest means same content: an existing row is never replaced.
        self._rows.setdefault(digest, stored)
        return digest

    def get(self, digest: str) -> Profile:
        stored = self._rows.get(digest)
        if stored is None:
            raise BundleRefused(f"no profile revision {digest[:12]}")
        data = json.loads(stored)
        model: Profile = (
            ExtractionProfileRevision.model_validate_json(stored)
            if data.get("kind") == "EXTRACTION_PROFILE"
            else PageTemplateRevision.model_validate_json(stored)
        )
        if profile_digest(model) != digest:
            raise BundleRefused(f"stored content does not recompute to {digest[:12]}")
        return model

    def bundle(self, epr_digest: str) -> Bundle:
        epr = self.get(epr_digest)
        if not isinstance(epr, ExtractionProfileRevision):
            raise BundleRefused("a bundle is loaded through its EPR")
        templates: dict[str, PageTemplateRevision] = {}
        for digest in epr.templates:
            template = self.get(digest)
            if not isinstance(template, PageTemplateRevision):
                raise BundleRefused("an EPR pins only page templates")
            if template.supplier_key != epr.supplier_key:
                raise BundleRefused("a template is never shared across suppliers")
            templates[digest] = template
        return Bundle(epr, epr_digest, templates)
