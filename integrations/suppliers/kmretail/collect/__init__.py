"""KM통상's collection knowledge (ADR-0010 §3, §12).

Site knowledge only: pure functions over a captured document. Nothing here can make a request,
open a session or reach a transport — the common COLLECT gateway does all of that and hands this
package nothing but text. The package's modules are the hashed set of the supplier's extraction
identity, so a change to what these rules mean is visible in ``extraction_identity.py``.
"""

from integrations.suppliers.kmretail.collect.facts import parse_fields
from integrations.suppliers.kmretail.collect.identity import (
    IdentityResult,
    SourceIdentity,
    UnresolvedIdentity,
    resolve,
)
from integrations.suppliers.kmretail.collect.images import IMAGE_ROLES, classify_images
from integrations.suppliers.kmretail.collect.revision import EXTRACTION_REVISION

__all__ = [
    "EXTRACTION_REVISION",
    "IMAGE_ROLES",
    "IdentityResult",
    "SourceIdentity",
    "UnresolvedIdentity",
    "classify_images",
    "parse_fields",
    "resolve",
]
