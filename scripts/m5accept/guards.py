"""What one M5 acceptance run may never load (Issue #89 PR-F §A).

The guard machinery is the accepted M4 one — the process egress guard, the import finder and the
path audit hook — armed here with **M5's own forbidden surface**. The difference is deliberate and
narrow: M5's production owners legitimately import the SmartStore adapter modules (the endpoint
registry, the wire projection, the read-back normalizer and the execution seams) and the CONNECT
capability owner, which hold no transport. What stays forbidden is everything that could actually
reach a provider or an AI: every HTTP client, the browser automation, the SmartStore **caller**,
the CONNECT credential/session/service path, the supplier transports and the AI/OCR runtimes.

The run therefore proves provider-zero the same way M4 does: not by intention, but because the
code that could reach a provider cannot even be imported while it runs.
"""

from collections.abc import Sequence
from typing import Final

from scripts.m4accept.guards import FORBIDDEN_MODULES as M4_FORBIDDEN
from scripts.m4accept.guards import Guarded, GuardEvidence, forbidden, offline

# The adapters M5's own owners import. They hold no transport: the registry is a frozen contract
# table, the projection and normalizer are pure, and the execution seams refuse locally while
# CREATE, image upload and product search are NOT_ADOPTED.
ALLOWED_FOR_M5: Final = (
    "integrations.marketplaces",
    "app.connect.marketplace.service",
    "app.connect.marketplace.sources",
    "app.connect.marketplace.revision",
    "app.connect.marketplace.attestation_service",
)
# The one SmartStore module that does hold a transport, and must stay unloadable.
SMARTSTORE_CALLER: Final = "integrations.marketplaces.smartstore.caller"

FORBIDDEN_MODULES: Final[tuple[str, ...]] = tuple(
    sorted({*(m for m in M4_FORBIDDEN if m not in ALLOWED_FOR_M5), SMARTSTORE_CALLER})
)


def forbidden_for_m5(name: str, modules: Sequence[str] = FORBIDDEN_MODULES) -> bool:
    """Whether an M5 acceptance run refuses to load ``name``."""
    return forbidden(name, modules)


__all__ = [
    "ALLOWED_FOR_M5",
    "FORBIDDEN_MODULES",
    "SMARTSTORE_CALLER",
    "GuardEvidence",
    "Guarded",
    "forbidden_for_m5",
    "offline",
]
