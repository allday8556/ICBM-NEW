"""The Cafe24 template's collection knowledge (ADR-0010 §3, ADR-0030 §4).

Site knowledge only: pure functions over a captured document. Nothing here can make a request,
open a session or reach a transport. The package's modules are the hashed set of the template's
extraction identity, so a change to what these rules mean is visible in ``extraction_identity.py``.
"""

from integrations.suppliers.platforms.cafe24.collect.facts import (
    DEFAULT_VOCABULARY,
    Vocabulary,
    parse_fields,
)
from integrations.suppliers.platforms.cafe24.collect.identity import (
    IdentityResult,
    SourceIdentity,
    UnresolvedIdentity,
    resolve,
)
from integrations.suppliers.platforms.cafe24.collect.images import (
    OG_IMAGE_RULE,
    ROLE_RULES,
    classify_images,
    role_rules,
)
from integrations.suppliers.platforms.cafe24.collect.revision import EXTRACTION_REVISION

__all__ = [
    "DEFAULT_VOCABULARY",
    "EXTRACTION_REVISION",
    "OG_IMAGE_RULE",
    "ROLE_RULES",
    "IdentityResult",
    "SourceIdentity",
    "UnresolvedIdentity",
    "Vocabulary",
    "classify_images",
    "parse_fields",
    "resolve",
    "role_rules",
]
