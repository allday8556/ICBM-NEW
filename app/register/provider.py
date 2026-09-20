"""The provider seams REGISTER owns and an adapter implements (ADR-0014 §25; M5 PR-D).

REGISTER never imports a marketplace adapter, an HTTP client or a CONNECT service — the contract
test ``test_no_register_module_reaches_a_provider`` enforces it — so every provider capability
reaches the domain as one of the Protocols below. PR-D implements them for SmartStore under
``integrations/marketplaces/smartstore/``; PR-E wires them into execution.

Each seam is written so that *not having* the capability is expressible without lying:

* a duplicate lookup that is not adopted raises instead of returning ``NO_MATCH``, and PR-C then
  sees absent evidence — ``DUPLICATE_EVIDENCE_MISSING``, never READY where proof is required;
* an upload whose outcome is ambiguous returns no ``PreparedAsset``, so no provider asset identity
  is invented and the final preflight stays short of READY;
* a read-back that cannot be normalized is ``UNREADABLE``, which is never a confirmation.
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.register.preparation import DuplicateEvidence, PreparedAsset


@runtime_checkable
class DuplicateLookupSource(Protocol):
    """A provider duplicate lookup, scoped to one canonical account and listing identity."""

    def available(self) -> bool:
        """Whether a lookup contract is adopted at all. ``False`` keeps the caller fail-closed."""
        ...

    def evidence(
        self, *, marketplace_account_id: str, listing_identity: str
    ) -> DuplicateEvidence | None:
        """The evidence of one lookup, or a raised error. Never a fabricated ``NO_MATCH``."""
        ...


@runtime_checkable
class ProviderAssetSource(Protocol):
    """Promotion of one upload outcome to the provider asset identity of one exact artifact."""

    def promote(
        self,
        retained: Mapping[str, Any],
        *,
        asset_kind: Any,
        sha256: str,
        derivation_id: str | None,
        asset_profile: str,
        candidate_fingerprint: str,
    ) -> Any:
        """An outcome whose ``asset`` is a :class:`PreparedAsset` only when unambiguous."""
        ...


@runtime_checkable
class ReadbackComparator(Protocol):
    """Normalization of a provider read-back and its comparison with a frozen Snapshot."""

    def compare(self, snapshot_payload: Mapping[str, Any], retained: Mapping[str, Any]) -> Any:
        """A verdict plus its sanitized evidence, compared against the Snapshot only."""
        ...


__all__ = [
    "DuplicateEvidence",
    "DuplicateLookupSource",
    "PreparedAsset",
    "ProviderAssetSource",
    "ReadbackComparator",
]
