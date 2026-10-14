"""Super Delivery's collection knowledge (ADR-0040 §2, ADR-0010 §3 and §12).

Site knowledge only: pure functions over a page the user captured. Nothing here can make a request,
open a session or reach a transport. The package's modules are the hashed set of the supplier's
extraction identity, so a change to what these rules mean is visible in ``extraction_identity.py``.
"""

from integrations.suppliers.superdelivery.collect.facts import parse_fields
from integrations.suppliers.superdelivery.collect.identity import (
    IdentityResult,
    SourceIdentity,
    UnresolvedIdentity,
    path_number,
    resolve,
)
from integrations.suppliers.superdelivery.collect.images import IMAGE_ROLES, classify_images
from integrations.suppliers.superdelivery.collect.revision import EXTRACTION_REVISION

__all__ = [
    "EXTRACTION_REVISION",
    "IMAGE_ROLES",
    "IdentityResult",
    "SourceIdentity",
    "UnresolvedIdentity",
    "classify_images",
    "parse_fields",
    "path_number",
    "resolve",
]
