"""The M4 image vocabulary and its canonical manifests (Issue #80 PR-E, ADR-0013 §9).

Pure: no database, no clock, no I/O. Everything here is an identifier, an enum or a bounded code.
No page text, OCR text, prompt body, secret or URL can pass the validators: a transformation spec
or an operation provenance holding ``://`` is refused, and every free-form code is a short upper
snake-case token.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.collect.facts import ImageRole

DERIVATION_VERSION: Final = "image-derivation/v1"
SELECTION_VERSION: Final = "image-selection/v1"
QA_INPUT_VERSION: Final = "image-qa-input/v1"
CURRENT_SELECTION_RULE_VERSION: Final = "current-image-selection-rule/v1"
# The QA rule in force. A different version recorded on a result makes it stale for readiness.
DEFAULT_QA_RULE_VERSION: Final = "image-qa/v1"

MAX_INPUTS: Final = 16
MAX_OPERATIONS: Final = 16
MAX_FINDINGS: Final = 16
MAX_SPEC_BYTES: Final = 4096
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ImageError(ValueError):
    """An image input that cannot be recorded as stated."""


class ImageAssetKind(StrEnum):
    """What an image identity names: an immutable source asset (ADR-0010 §9) or a derived
    artifact of this module. A derived artifact is never source truth."""

    SOURCE_ASSET = "SOURCE_ASSET"
    DERIVED_ARTIFACT = "DERIVED_ARTIFACT"


class ExecutionClass(StrEnum):
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


class OperationOutcome(StrEnum):
    """Only a COMPLETED operation yields a durable artifact (ADR-0013 §9, Issue #56 §6)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class SourceDecisionKind(StrEnum):
    """What the operator decided for one CONFIRMED source image reference."""

    USE_SOURCE = "USE_SOURCE"
    USE_DERIVED = "USE_DERIVED"
    EXCLUDE = "EXCLUDE"


class DecisionOrigin(StrEnum):
    """A selection is an operator decision, never a derived default."""

    OPERATOR = "OPERATOR"


class SelectionMoveReason(StrEnum):
    INITIAL = "INITIAL"
    RESELECTED = "RESELECTED"


class QaVerdict(StrEnum):
    PASS = "PASS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAIL = "FAIL"


def digest(structure: object) -> str:
    text = json.dumps(structure, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def canonical_json(structure: object) -> str:
    return json.dumps(structure, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ImageError(f"{name} is a lowercase SHA-256 hex digest")
    return value


def _code(name: str, value: object) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ImageError(f"{name} is an upper snake-case code")
    return value


def _label(name: str, value: object) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ImageError(f"{name} is a version or identity label")
    return value


# ---------------------------------------------------------------- a completed derivation


@dataclass(frozen=True)
class DerivationInput:
    """One input: a source asset by its SHA, or a parent artifact with the derivation that
    produced it."""

    kind: ImageAssetKind
    sha256: str
    parent_derivation_id: str | None = None

    def canonical(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "sha256": self.sha256,
            "parent_derivation_id": self.parent_derivation_id,
        }


@dataclass(frozen=True)
class OperationRecord:
    """Bounded provenance of one operation: codes, digests and a time, never content. A
    deterministic LOCAL operation names no provider; a CLOUD one names its provider and model."""

    capability: str
    execution_class: ExecutionClass
    outcome: OperationOutcome
    input_digest: str
    output_digest: str
    executed_at: datetime
    provider: str | None = None
    model: str | None = None

    def canonical(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "execution_class": self.execution_class.value,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "executed_at": self.executed_at.isoformat(),
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True)
class CompletedDerivation:
    """What a caller hands over: the finished output bytes and how they were made. The output SHA
    is computed from the bytes by the store; any digest here is only checked against it."""

    output: bytes
    validated_source_revision_id: str
    inputs: tuple[DerivationInput, ...]
    transformation_spec: Mapping[str, object]
    transformation_version: str
    policy_version: str | None
    operations: tuple[OperationRecord, ...]
    produced_at: datetime


def _bounded(value: object, depth: int = 0) -> None:
    """A transformation spec is data about the recipe: scalars, lists and maps of them. Never a
    URL, never page text of any length, never a float that could carry a hidden decision."""
    if depth > 4:
        raise ImageError("a transformation spec nests at most four levels")
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        if "://" in value or len(value) > 128:
            raise ImageError("a transformation spec holds short codes, never a URL or text")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _bounded(item, depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not _CODE.fullmatch(key.upper()):
                raise ImageError("a transformation spec key is a short identifier")
            _bounded(item, depth + 1)
        return
    raise ImageError("a transformation spec holds only strings, integers, booleans and nulls")


def validate_completed(derivation: CompletedDerivation) -> None:
    """Refuse anything that is not one completed derivation, before a byte is written."""
    if not derivation.operations or len(derivation.operations) > MAX_OPERATIONS:
        raise ImageError(f"a derivation records 1 to {MAX_OPERATIONS} operations")
    for operation in derivation.operations:
        if operation.outcome is not OperationOutcome.COMPLETED:
            raise ImageError("only a completed operation yields a durable artifact")
        _code("capability", operation.capability)
        if not isinstance(operation.execution_class, ExecutionClass):
            raise ImageError("execution_class is LOCAL or CLOUD")
        _sha("input_digest", operation.input_digest)
        _sha("output_digest", operation.output_digest)
        if operation.executed_at.tzinfo is None:
            raise ImageError("executed_at is timezone-aware")
        if operation.execution_class is ExecutionClass.CLOUD:
            _label("provider", operation.provider)
            _label("model", operation.model)
        for name in ("provider", "model"):
            value = getattr(operation, name)
            if value is not None:
                _label(name, value)
    if not derivation.inputs or len(derivation.inputs) > MAX_INPUTS:
        raise ImageError(f"a derivation consumes 1 to {MAX_INPUTS} inputs")
    for item in derivation.inputs:
        if not isinstance(item.kind, ImageAssetKind):
            raise ImageError("an input is a SOURCE_ASSET or a DERIVED_ARTIFACT")
        _sha("input sha256", item.sha256)
        derived = item.kind is ImageAssetKind.DERIVED_ARTIFACT
        if derived != (item.parent_derivation_id is not None):
            raise ImageError("a derived input names its parent derivation, and only it does")
    _label("transformation_version", derivation.transformation_version)
    if derivation.policy_version is not None:
        _label("policy_version", derivation.policy_version)
    if not isinstance(derivation.transformation_spec, Mapping):
        raise ImageError("a transformation spec is a mapping")
    _bounded(derivation.transformation_spec)
    if len(canonical_json(derivation.transformation_spec)) > MAX_SPEC_BYTES:
        raise ImageError(f"a transformation spec is at most {MAX_SPEC_BYTES} bytes")
    if derivation.produced_at.tzinfo is None:
        raise ImageError("produced_at is timezone-aware")


def input_manifest(inputs: Sequence[DerivationInput]) -> list[dict[str, object]]:
    return [item.canonical() for item in inputs]


def derivation_fingerprint(
    *,
    validated_source_revision_id: str,
    inputs: Sequence[DerivationInput],
    transformation_spec: Mapping[str, object],
    transformation_version: str,
    policy_version: str | None,
    operations: Sequence[OperationRecord],
    produced_at: datetime,
    artifact_sha256: str,
) -> str:
    """The deterministic identity of one completed derivation: its recipe, its canonical
    execution provenance and its output (PR #85 review 5254146288).

    Retrying the same completed derivation — the same recipe, the same operations with the same
    capability, execution class, digests, times and provider/model, the same output — is one
    derivation. Another completed execution is another derivation even when the recipe and the
    output bytes are identical: provenance is never lost because an artifact dedupes. Two such
    derivations share one artifact.
    """
    return digest(
        {
            "version": DERIVATION_VERSION,
            "validated_source_revision_id": validated_source_revision_id,
            "inputs": input_manifest(inputs),
            "transformation_spec": transformation_spec,
            "transformation_version": transformation_version,
            "policy_version": policy_version,
            "operations": [operation.canonical() for operation in operations],
            "produced_at": produced_at.isoformat(),
            "artifact_sha256": artifact_sha256,
        }
    )


def operation_from_canonical(value: Mapping[str, object]) -> OperationRecord:
    """Read back one recorded operation. Only completed operations are ever recorded."""
    provider = value.get("provider")
    model = value.get("model")
    return OperationRecord(
        capability=str(value["capability"]),
        execution_class=ExecutionClass(str(value["execution_class"])),
        outcome=OperationOutcome.COMPLETED,
        input_digest=str(value["input_digest"]),
        output_digest=str(value["output_digest"]),
        executed_at=datetime.fromisoformat(str(value["executed_at"])),
        provider=None if provider is None else str(provider),
        model=None if model is None else str(model),
    )


# ---------------------------------------------------------------- the operator selection


@dataclass(frozen=True)
class SourceRef:
    """One CONFIRMED source image reference of a revision, by its identity in that revision."""

    role: ImageRole
    ordinal: int
    sha256: str

    def canonical(self) -> list[object]:
        return [self.role.value, self.ordinal, self.sha256]


def source_decision_fingerprint(refs: Sequence[SourceRef]) -> str:
    """What the operator had to account for: every CONFIRMED reference, in source order."""
    ordered = sorted(refs, key=lambda ref: (ref.role != ImageRole.REPRESENTATIVE, ref.ordinal))
    return digest({"version": SELECTION_VERSION, "refs": [ref.canonical() for ref in ordered]})


@dataclass(frozen=True)
class SourceDecision:
    """The operator's decision for one CONFIRMED source reference. USE_DERIVED names the
    derivation whose artifact replaces it; the others name none."""

    role: ImageRole
    ordinal: int
    sha256: str
    decision: SourceDecisionKind
    derivation_id: str | None = None


@dataclass(frozen=True)
class SelectedOutput:
    """One image the Item uses now, in order: the decision it comes from and the role it plays.
    Its exact binary follows from that decision, so it cannot disagree with it."""

    role: ImageRole
    source_role: ImageRole
    source_ordinal: int


def qa_input_fingerprint(
    *,
    asset_kind: ImageAssetKind,
    sha256: str,
    derivation_id: str | None,
    validated_source_revision_id: str,
    qa_rule_version: str,
) -> str:
    """Everything a QA verdict was computed against. A change to any of it is a new input."""
    return digest(
        {
            "version": QA_INPUT_VERSION,
            "asset_kind": asset_kind.value,
            "sha256": sha256,
            "derivation_id": derivation_id,
            "validated_source_revision_id": validated_source_revision_id,
            "qa_rule_version": qa_rule_version,
        }
    )


def validate_findings(findings: Sequence[str]) -> tuple[str, ...]:
    """QA findings are bounded codes only: no OCR text, no page content."""
    if len(findings) > MAX_FINDINGS:
        raise ImageError(f"a QA result records at most {MAX_FINDINGS} findings")
    codes = tuple(_code("finding", finding) for finding in findings)
    if len(set(codes)) != len(codes):
        raise ImageError("each finding code is recorded once")
    return codes
