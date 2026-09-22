"""The durable registration target policy (Gate 1 G1-A; ADR-0015 §2, ADR-0014 §21).

One policy per ``marketplace_key × marketplace_account_id``. It is the Settings/platform policy the
REGISTER preflight reads on every evaluation, and this module is its only owner:

- **Revisions are append-only and server-created.** A Settings save appends one revision: the
  server creates its identity (the ``TargetPolicy.policy_revision``), the fingerprint of its
  canonical sanitized content and its audit record, and moves the one current pointer to it in the
  same unit of work. A client never supplies a revision identity or a fingerprint, and history is
  never overwritten or deleted.
- **A revision is validated whole, or not written.** Every field is required, nothing is defaulted
  by the server, and an invalid, unsafe or scope-crossing value refuses the whole save. No partial
  revision exists.
- **It owns only the ADR-0015 §2 inputs**: the taxonomy revision, the M4 pricing context, the
  account's template identities, the sanitizer profile, the asset policy, the duplicate-proof
  policy and the authoring revision references. No Product, price, readiness, Snapshot, capability
  or provider truth, and no category metadata.
- **M5 never prices.** The pricing context is an M4 ``PricingContextInput`` for this policy's own
  marketplace, whose ``account_id`` discriminator is this policy's canonical account or ``None``
  (account-invariant); the M4 pricing owner prices under it.

:class:`DurableRegistrationPolicy` is the production ``RegistrationPolicySource``: it materializes
the existing ``TargetPolicy`` contract from the current revision on every read, so a later revision
changes the next evaluation's dependency fingerprint and never an earlier Snapshot.
"""

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.accounts import AccountBinding, MarketplaceAccountStore, binding_state
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.products.pricing import PricingContextInput, PricingError, Rounding
from app.register import sanitize
from app.register.model import RegistrationConflictError, canonical_json, sanitized_digest
from app.register.policy import AssetPolicy, DuplicateKeyKind, TargetPolicy
from app.register.target_policy_models import (
    RegistrationTargetPolicy,
    RegistrationTargetPolicyCurrent,
    RegistrationTargetPolicyRevision,
)

CONTENT_VERSION: Final = "registration-target-policy/v1"
MAX_TEMPLATES: Final = 32

TARGET_POLICY_INVALID: Final = "TARGET_POLICY_INVALID"
TARGET_POLICY_PRICING_CONTEXT_INVALID: Final = "TARGET_POLICY_PRICING_CONTEXT_INVALID"
TARGET_POLICY_UNSAFE_CONTENT: Final = "TARGET_POLICY_UNSAFE_CONTENT"
TARGET_POLICY_ACCOUNT_UNKNOWN: Final = "TARGET_POLICY_ACCOUNT_UNKNOWN"
TARGET_POLICY_CURRENT_MOVED: Final = "TARGET_POLICY_CURRENT_MOVED"
TARGET_POLICY_UNCHANGED: Final = "TARGET_POLICY_UNCHANGED"


class EditableSurface(StrEnum):
    """A Settings surface the server accepts a save for. Nothing else in Settings is editable."""

    REGISTRATION_TARGET_POLICY = "REGISTRATION_TARGET_POLICY"


# ------------------------------------------------------------------ the application contract


class _Strict(BaseModel):
    """Every field required, no unknown field, no coercion of a scalar: a client-invented
    ``policy_revision`` or fingerprint, a float rate or a missing value is refused, never
    repaired. Enumerations are read from their declared string values."""

    model_config = ConfigDict(extra="forbid")


class PricingContextInputView(_Strict):
    """The M4 ``PricingContextInput`` fields, as authored (ADR-0013 §7)."""

    marketplace_key: StrictStr
    account_id: StrictStr | None
    fee_table_version: StrictStr
    pricing_policy_version: StrictStr
    fee_rate: StrictStr
    fee_fixed_krw: StrictInt
    other_cost_rate: StrictStr
    other_cost_fixed_krw: StrictInt
    cost_rounding: Rounding
    price_rounding: Rounding


class AssetPolicyView(_Strict):
    profile: StrictStr
    min_images: StrictInt
    max_images: StrictInt
    requires_representative: StrictBool
    provider_asset_identity_required: StrictBool


class TargetPolicyInputsView(_Strict):
    """The supported target-policy surface (ADR-0015 §2). Optional revisions are explicit nulls."""

    taxonomy_revision: StrictStr
    pricing_context: PricingContextInputView
    sanitizer_profile_version: StrictStr
    asset_policy: AssetPolicyView
    templates: dict[StrictStr, StrictStr]
    duplicate_proof_required: StrictBool
    duplicate_lookup_keys: list[DuplicateKeyKind]
    category_mapping_revision: StrictStr | None
    detail_composition_revision: StrictStr | None


class AppendRevisionRequest(_Strict):
    """One Settings save. ``expected_current_revision`` is the revision the operator edited from
    (``null`` for the first one); a save made against a moved policy is refused, never merged."""

    actor: StrictStr = Field(min_length=2, max_length=64)
    expected_current_revision: StrictStr | None
    inputs: TargetPolicyInputsView


class TargetPolicyRevisionView(BaseModel):
    policy_revision: str
    revision_no: int
    content_fingerprint: str
    authored_by: str
    authored_at: datetime
    current: bool


class TargetPolicyView(BaseModel):
    """One account's target policy as the server holds it. ``editable`` is the server's answer."""

    marketplace_key: str
    marketplace_account_id: str
    surface: EditableSurface
    editable: bool
    current: TargetPolicyRevisionView | None
    inputs: TargetPolicyInputsView | None
    history: list[TargetPolicyRevisionView]


class TargetPolicyAccountsView(BaseModel):
    marketplace_key: str
    surface: EditableSurface
    accounts: list[TargetPolicyView]


# ------------------------------------------------------------------ content


def _invalid(code: str, message: str, field: str) -> InputValidationError:
    return InputValidationError(code, message, details={"field": field})


def _label(value: str | None, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if value is None or not sanitize.safe_label(value):
        raise _invalid(
            TARGET_POLICY_INVALID, "a plain version or identity label is required", field
        )
    return value


def encode_content(
    marketplace_key: str, marketplace_account_id: str, inputs: TargetPolicyInputsView
) -> dict[str, Any]:
    """The canonical sanitized content of one revision, or a refusal of the whole save."""
    pricing = inputs.pricing_context
    if pricing.marketplace_key != marketplace_key or pricing.account_id not in (
        None,
        marketplace_account_id,
    ):
        raise _invalid(
            TARGET_POLICY_PRICING_CONTEXT_INVALID,
            "the pricing context is for this policy's own marketplace, and its account is this"
            " policy's canonical account or explicitly none",
            "pricing_context",
        )
    try:
        PricingContextInput(**pricing.model_dump())
    except PricingError as refused:
        raise _invalid(
            TARGET_POLICY_PRICING_CONTEXT_INVALID, str(refused), "pricing_context"
        ) from refused
    asset = inputs.asset_policy
    if asset.min_images < 0 or asset.max_images < 1 or asset.min_images > asset.max_images:
        raise _invalid(
            TARGET_POLICY_INVALID,
            "the image bounds are non-negative, at least one image is allowed and min <= max",
            "asset_policy",
        )
    if len(inputs.templates) > MAX_TEMPLATES:
        raise _invalid(TARGET_POLICY_INVALID, "too many templates", "templates")
    templates = {
        str(_label(kind, "templates")): str(_label(identity, f"templates.{kind}"))
        for kind, identity in inputs.templates.items()
    }
    keys = [key.value for key in inputs.duplicate_lookup_keys]
    if len(set(keys)) != len(keys):
        raise _invalid(
            TARGET_POLICY_INVALID, "a lookup key is listed twice", "duplicate_lookup_keys"
        )
    content: dict[str, Any] = {
        "content_version": CONTENT_VERSION,
        "marketplace_key": marketplace_key,
        "marketplace_account_id": marketplace_account_id,
        "taxonomy_revision": _label(inputs.taxonomy_revision, "taxonomy_revision"),
        "pricing_context": pricing.model_dump(mode="json"),
        "sanitizer_profile_version": _label(
            inputs.sanitizer_profile_version, "sanitizer_profile_version"
        ),
        "asset_policy": {
            "profile": _label(asset.profile, "asset_policy.profile"),
            "min_images": asset.min_images,
            "max_images": asset.max_images,
            "requires_representative": asset.requires_representative,
            "provider_asset_identity_required": asset.provider_asset_identity_required,
        },
        "templates": dict(sorted(templates.items())),
        "duplicate_proof_required": inputs.duplicate_proof_required,
        "duplicate_lookup_keys": sorted(keys),
        "category_mapping_revision": _label(
            inputs.category_mapping_revision, "category_mapping_revision", optional=True
        ),
        "detail_composition_revision": _label(
            inputs.detail_composition_revision, "detail_composition_revision", optional=True
        ),
    }
    try:
        sanitize.require_clean(content, "target_policy")
    except sanitize.PayloadSanitationError as refused:
        raise InputValidationError(
            TARGET_POLICY_UNSAFE_CONTENT,
            "a target policy never holds a URL, a credential or session material",
            details={"found": [list(item) for item in refused.found]},
        ) from refused
    return content


def inputs_of(content: Mapping[str, Any]) -> TargetPolicyInputsView:
    """The authored inputs a stored revision holds."""
    return TargetPolicyInputsView.model_validate(
        {
            key: content[key]
            for key in TargetPolicyInputsView.model_fields
            if key != "duplicate_lookup_keys"
        }
        | {"duplicate_lookup_keys": [DuplicateKeyKind(k) for k in content["duplicate_lookup_keys"]]}
        | {
            "pricing_context": {
                **content["pricing_context"],
                "cost_rounding": Rounding(content["pricing_context"]["cost_rounding"]),
                "price_rounding": Rounding(content["pricing_context"]["price_rounding"]),
            }
        }
    )


def target_policy_of(policy_revision: str, content: Mapping[str, Any]) -> TargetPolicy:
    """The existing ``TargetPolicy`` contract, materialized from one stored revision."""
    inputs = inputs_of(content)
    pricing = inputs.pricing_context
    asset = inputs.asset_policy
    return TargetPolicy(
        marketplace_key=str(content["marketplace_key"]),
        marketplace_account_id=str(content["marketplace_account_id"]),
        policy_revision=policy_revision,
        taxonomy_revision=inputs.taxonomy_revision,
        pricing_context=PricingContextInput(**pricing.model_dump()),
        sanitizer_profile_version=inputs.sanitizer_profile_version,
        asset_policy=AssetPolicy(
            profile=asset.profile,
            min_images=asset.min_images,
            max_images=asset.max_images,
            requires_representative=asset.requires_representative,
            provider_asset_identity_required=asset.provider_asset_identity_required,
        ),
        category_mapping_revision=inputs.category_mapping_revision,
        detail_composition_revision=inputs.detail_composition_revision,
        templates=dict(inputs.templates),
        duplicate_proof_required=inputs.duplicate_proof_required,
        duplicate_lookup_keys=frozenset(inputs.duplicate_lookup_keys),
    )


# ------------------------------------------------------------------ the store


@dataclass(frozen=True)
class PolicyRevisionRecord:
    policy_revision: str
    revision_no: int
    content: Mapping[str, Any]
    content_fingerprint: str
    authored_by: str
    authored_at: datetime


@dataclass(frozen=True)
class PolicyRecord:
    marketplace_key: str
    marketplace_account_id: str
    current: PolicyRevisionRecord | None
    history: tuple[PolicyRevisionRecord, ...]


class TargetPolicyStore:
    """The only production writer of the three target-policy tables."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    # -------------------------------------------------------------- reads

    def policy(self, marketplace_key: str, marketplace_account_id: str) -> PolicyRecord:
        with self._reading() as session:
            row = _policy_row(session, marketplace_key, marketplace_account_id)
            if row is None:
                return PolicyRecord(marketplace_key, marketplace_account_id, None, ())
            history = tuple(
                _revision_record(r)
                for r in session.scalars(
                    select(RegistrationTargetPolicyRevision)
                    .where(RegistrationTargetPolicyRevision.policy_id == row.policy_id)
                    .order_by(RegistrationTargetPolicyRevision.revision_no.desc())
                ).all()
            )
            return PolicyRecord(
                marketplace_key,
                marketplace_account_id,
                _current_record(session, row.policy_id),
                history,
            )

    def current(
        self, marketplace_key: str, marketplace_account_id: str
    ) -> PolicyRevisionRecord | None:
        with self._reading() as session:
            row = _policy_row(session, marketplace_key, marketplace_account_id)
            return None if row is None else _current_record(session, row.policy_id)

    # -------------------------------------------------------------- the one write

    def append(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        content: Mapping[str, Any],
        *,
        expected_current_revision: str | None,
        authored_by: str,
        correlation_id: str,
    ) -> PolicyRevisionRecord:
        """Append one revision and make it current, in one unit of work, or change nothing."""
        fingerprint = sanitized_digest(content)
        now = self._clock.now()
        with self._db.write() as session:
            if (
                binding_state(session, marketplace_key, marketplace_account_id)
                is AccountBinding.UNKNOWN_ACCOUNT
            ):
                raise NotFoundError(
                    TARGET_POLICY_ACCOUNT_UNKNOWN,
                    "a target policy belongs to a canonical account of its marketplace",
                )
            policy = _policy_row(session, marketplace_key, marketplace_account_id)
            current = None if policy is None else _current_row(session, policy.policy_id)
            if (None if current is None else current.policy_revision_id) != (
                expected_current_revision
            ):
                raise RegistrationConflictError(
                    TARGET_POLICY_CURRENT_MOVED,
                    "the policy changed since it was read; reload it before saving",
                    details={
                        "current_revision": None if current is None else current.policy_revision_id
                    },
                )
            if current is not None:
                previous = session.get(RegistrationTargetPolicyRevision, current.policy_revision_id)
                if previous is not None and previous.content_fingerprint == fingerprint:
                    raise RegistrationConflictError(
                        TARGET_POLICY_UNCHANGED,
                        "the saved policy is identical to the current revision",
                    )
            if policy is None:
                policy = RegistrationTargetPolicy(
                    policy_id=str(uuid.uuid4()),
                    marketplace_key=marketplace_key,
                    marketplace_account_id=marketplace_account_id,
                    created_by=authored_by,
                    correlation_id=correlation_id,
                    created_at=now,
                )
                session.add(policy)
                session.flush()
            number = 1 + int(
                session.scalar(
                    select(
                        func.coalesce(func.max(RegistrationTargetPolicyRevision.revision_no), 0)
                    ).where(RegistrationTargetPolicyRevision.policy_id == policy.policy_id)
                )
                or 0
            )
            revision = RegistrationTargetPolicyRevision(
                policy_revision_id=str(uuid.uuid4()),
                policy_id=policy.policy_id,
                revision_no=number,
                content_json=canonical_json(dict(content)),
                content_fingerprint=fingerprint,
                authored_by=authored_by,
                correlation_id=correlation_id,
                authored_at=now,
            )
            session.add(revision)
            session.flush()
            previous_id = None if current is None else current.policy_revision_id
            if current is None:
                session.add(
                    RegistrationTargetPolicyCurrent(
                        policy_id=policy.policy_id,
                        policy_revision_id=revision.policy_revision_id,
                        moved_by=authored_by,
                        correlation_id=correlation_id,
                        moved_at=now,
                    )
                )
            else:
                current.policy_revision_id = revision.policy_revision_id
                current.moved_by = authored_by
                current.correlation_id = correlation_id
                current.moved_at = now
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.REGISTRATION_TARGET_POLICY_REVISED,
                    action="TARGET_POLICY_REVISION_APPENDED",
                    actor=authored_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=policy.policy_id,
                    before=None if previous_id is None else {"policy_revision": previous_id},
                    after={
                        "policy_revision": revision.policy_revision_id,
                        "revision_no": number,
                        "content_fingerprint": fingerprint,
                    },
                    details={
                        "marketplace_key": marketplace_key,
                        "marketplace_account_id": marketplace_account_id,
                        "content_version": CONTENT_VERSION,
                    },
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _revision_record(revision)

    @contextmanager
    def _reading(self) -> Iterator[Session]:
        with self._db.read() as session:
            yield session


def _policy_row(
    session: Session, marketplace_key: str, marketplace_account_id: str
) -> RegistrationTargetPolicy | None:
    return session.scalars(
        select(RegistrationTargetPolicy).where(
            RegistrationTargetPolicy.marketplace_key == marketplace_key,
            RegistrationTargetPolicy.marketplace_account_id == marketplace_account_id,
        )
    ).one_or_none()


def _current_row(session: Session, policy_id: str) -> RegistrationTargetPolicyCurrent | None:
    return session.get(RegistrationTargetPolicyCurrent, policy_id)


def _current_record(session: Session, policy_id: str) -> PolicyRevisionRecord | None:
    pointer = _current_row(session, policy_id)
    if pointer is None:
        return None
    revision = session.get(RegistrationTargetPolicyRevision, pointer.policy_revision_id)
    return None if revision is None else _revision_record(revision)


def _revision_record(row: RegistrationTargetPolicyRevision) -> PolicyRevisionRecord:
    return PolicyRevisionRecord(
        policy_revision=row.policy_revision_id,
        revision_no=row.revision_no,
        content=json.loads(row.content_json),
        content_fingerprint=row.content_fingerprint,
        authored_by=row.authored_by,
        authored_at=row.authored_at,
    )


# ------------------------------------------------------------------ the production policy source


class DurableRegistrationPolicy:
    """The production ``RegistrationPolicySource``: the current revision, read on every call."""

    def __init__(self, store: TargetPolicyStore) -> None:
        self._store = store

    def target(self, marketplace_key: str, marketplace_account_id: str) -> TargetPolicy | None:
        current = self._store.current(marketplace_key, marketplace_account_id)
        if current is None:
            return None
        return target_policy_of(current.policy_revision, current.content)


# ------------------------------------------------------------------ the Settings application owner


class TargetPolicyService:
    """What Settings reads and saves for the target-policy surface, and nothing else."""

    def __init__(self, store: TargetPolicyStore, accounts: MarketplaceAccountStore) -> None:
        self._store = store
        self._accounts = accounts

    def accounts(self, marketplace_key: str) -> TargetPolicyAccountsView:
        return TargetPolicyAccountsView(
            marketplace_key=marketplace_key,
            surface=EditableSurface.REGISTRATION_TARGET_POLICY,
            accounts=[
                self._view(self._store.policy(marketplace_key, record.marketplace_account_id))
                for record in self._accounts.accounts(marketplace_key)
            ],
        )

    def policy(self, marketplace_key: str, marketplace_account_id: str) -> TargetPolicyView:
        self._require_account(marketplace_key, marketplace_account_id)
        return self._view(self._store.policy(marketplace_key, marketplace_account_id))

    def append(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        request: AppendRevisionRequest,
        *,
        correlation_id: str,
    ) -> TargetPolicyView:
        self._require_account(marketplace_key, marketplace_account_id)
        content = encode_content(marketplace_key, marketplace_account_id, request.inputs)
        self._store.append(
            marketplace_key,
            marketplace_account_id,
            content,
            expected_current_revision=request.expected_current_revision,
            authored_by=request.actor,
            correlation_id=correlation_id,
        )
        return self._view(self._store.policy(marketplace_key, marketplace_account_id))

    def _require_account(self, marketplace_key: str, marketplace_account_id: str) -> None:
        account = self._accounts.account(marketplace_account_id)
        if account is None or account.marketplace_key != marketplace_key:
            raise NotFoundError(
                TARGET_POLICY_ACCOUNT_UNKNOWN,
                "a target policy belongs to a canonical account of its marketplace",
            )

    @staticmethod
    def _view(record: PolicyRecord) -> TargetPolicyView:
        current = record.current
        return TargetPolicyView(
            marketplace_key=record.marketplace_key,
            marketplace_account_id=record.marketplace_account_id,
            surface=EditableSurface.REGISTRATION_TARGET_POLICY,
            editable=True,
            current=None if current is None else _revision_view(current, current=True),
            inputs=None if current is None else inputs_of(current.content),
            history=[
                _revision_view(
                    revision,
                    current=current is not None
                    and revision.policy_revision == current.policy_revision,
                )
                for revision in record.history
            ],
        )


def _revision_view(record: PolicyRevisionRecord, *, current: bool) -> TargetPolicyRevisionView:
    return TargetPolicyRevisionView(
        policy_revision=record.policy_revision,
        revision_no=record.revision_no,
        content_fingerprint=record.content_fingerprint,
        authored_by=record.authored_by,
        authored_at=record.authored_at,
        current=current,
    )


def editable_surfaces() -> Sequence[EditableSurface]:
    """The Settings surfaces this application accepts a save for (ADR-0015 §2)."""
    return (EditableSurface.REGISTRATION_TARGET_POLICY,)
