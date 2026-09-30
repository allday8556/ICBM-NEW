"""Duplicate-lookup evidence for SmartStore — fail-closed in PR-D (ADR-0014 §13; kickoff §6).

``POST /v1/products/search`` is adopted, but **for positive-only reconcile only** (ADR-0014
§28.2; the SEARCH adoption slice). It is never a duplicate lookup: the provider's seller-code
match is similar, partial or exact, its codes are not unique, and no result is complete or fresh
enough to be absence (ADR-0014 §13, §17.2). So this adapter still produces **no evidence at
all**.

That is deliberate. PR-C treats absent evidence as ``DUPLICATE_EVIDENCE_MISSING`` and refuses
READY wherever the target requires duplicate proof, which is exactly the fail-closed outcome the
kickoff asks for. Fabricating ``NO_MATCH`` from an unproven lookup would turn an unknown into a
pass — the one thing §7.3 of CLAUDE.md forbids. An operator's ``DuplicateOverride`` still covers a
known duplicate, and still never releases an UNKNOWN CREATE conflict.
"""

from typing import Final

from app.platform.core.errors import AppError
from integrations.marketplaces.smartstore.registry import EndpointId

LOOKUP_CONTRACT_VERSION: Final = "smartstore-duplicate-lookup/unproven"


class DuplicateLookupUnavailableError(AppError):
    """No duplicate lookup can be performed, so no evidence exists. Never a NO_MATCH."""

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_DUPLICATE_LOOKUP_NOT_ADOPTED",
            "SmartStore duplicate lookup is not adopted: product search is adopted for"
            " positive-only reconcile only and is never duplicate-absence evidence"
            " (ADR-0014 §13, §17.2, §28.2)",
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_SEARCH.value},
        )


class SmartStoreDuplicateLookup:
    """The SmartStore duplicate-lookup source. It holds no transport, because it can issue no
    request: every call raises, and no caller can mistake that for an answer."""

    marketplace_key: Final = "smartstore"

    def evidence(
        self, *, marketplace_account_id: str, listing_identity: str
    ) -> None:  # pragma: no cover - the signature exists so PR-E wires the real shape
        raise DuplicateLookupUnavailableError()

    def available(self) -> bool:
        """Whether a lookup can be performed at all. Always ``False`` under this packet."""
        return False
