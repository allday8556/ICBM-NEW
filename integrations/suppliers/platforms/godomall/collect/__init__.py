"""The Godomall template's collection knowledge (ADR-0010 §3, ADR-0030 §4, ADR-0034).

Site knowledge only: pure functions over a captured document. The package's modules are the hashed
set of the template's extraction identity.
"""

from integrations.suppliers.platforms.godomall.collect.facts import (
    DEFAULT_VOCABULARY,
    Vocabulary,
    parse_fields,
)
from integrations.suppliers.platforms.godomall.collect.identity import (
    IdentityResult,
    SourceIdentity,
    UnresolvedIdentity,
    goods_number,
    resolve,
)
from integrations.suppliers.platforms.godomall.collect.images import (
    OG_IMAGE_RULE,
    ROLE_RULES,
    classify_images,
    role_rules,
)
from integrations.suppliers.platforms.godomall.collect.revision import EXTRACTION_REVISION

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
    "goods_number",
    "parse_fields",
    "resolve",
    "role_rules",
]
