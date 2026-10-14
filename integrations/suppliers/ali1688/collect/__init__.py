"""1688's collection knowledge (ADR-0040 §3, §4; ADR-0010 §3 and §12).

Site knowledge only: pure functions over a page the user captured, its embedded-data member and the
description document the server read for it. Nothing here can make a request, open a session or
reach a transport. The package's modules are the hashed set of the supplier's extraction identity,
so a change to what these rules mean is visible in ``extraction_identity.py``.
"""

from integrations.suppliers.ali1688.collect.description import classify as classify_description
from integrations.suppliers.ali1688.collect.description import locate as locate_description
from integrations.suppliers.ali1688.collect.facts import parse_fields
from integrations.suppliers.ali1688.collect.identity import (
    IdentityResult,
    SourceIdentity,
    UnresolvedIdentity,
    path_number,
    resolve,
)
from integrations.suppliers.ali1688.collect.images import IMAGE_HOSTS, IMAGE_ROLES, classify_images
from integrations.suppliers.ali1688.collect.revision import EXTRACTION_REVISION

__all__ = [
    "EXTRACTION_REVISION",
    "IMAGE_HOSTS",
    "IMAGE_ROLES",
    "IdentityResult",
    "SourceIdentity",
    "UnresolvedIdentity",
    "classify_description",
    "classify_images",
    "locate_description",
    "parse_fields",
    "path_number",
    "resolve",
]
