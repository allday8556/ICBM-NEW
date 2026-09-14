"""Seams between marketplace capability truth and its evidence sources (M2 instructions §5.1, §6.7).

- ``ApplicationIdentitySource``: the configured provider application, from which the permission
  evidence fingerprint is derived — never typed by hand. PR-A owns the SmartStore credential
  bundle and implements it; until then nothing does in production, so no attestation can be
  recorded and there is no temporary value.
- ``PermissionEvidenceSource``: the write_scope that current permission evidence supports, which
  capability truth converges on (PR-C implements it from A0 attestations), together with why the
  evidence supports no more, so the convergence audit can say so (CAPABILITY_MAPPING S7).

The endpoint-mapping revision arrives the same way, through ``revision.py``.
"""

from dataclasses import dataclass
from typing import Protocol

from app.connect.marketplace.attestation import ApplicationIdentity
from app.connect.marketplace.capability import WriteScope


class ApplicationIdentitySource(Protocol):
    def current_identity(self) -> ApplicationIdentity | None:
        """The configured application, or None while none is configured."""
        ...


@dataclass(frozen=True)
class PermissionEvidence:
    """What current permission evidence supports right now."""

    write_scope: WriteScope
    # Why the evidence is not current (for example ``EXPIRED``); empty while it is.
    invalidations: tuple[str, ...] = ()


class PermissionEvidenceSource(Protocol):
    def current_permission(self, marketplace_key: str) -> PermissionEvidence | None:
        """What current evidence supports, or None when no evidence is on record."""
        ...
