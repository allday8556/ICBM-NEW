"""Platform templates: one parser per storefront platform, shared by every site on it (ADR-0030).

A template is site knowledge in the same sense as a supplier package (ADR-0010 §3): pure functions
over a captured document, no network, browser or logging import, and an extraction identity pinned
by its own ``extraction_identity.py``. What it adds is the closed list of what a site
configuration may vary — label slots, region slots, product-path forms and limits — and a
``bind`` that turns one validated site into the CONNECT definition and the collection definition
the rest of the application already knows.

A site never adds code. A page a template cannot read is fixed in the template, for every site on
the platform, or waits for a template of its own (ADR-0030 §1, §9).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.site_config import SiteConfig, SiteRegion


@dataclass(frozen=True)
class PathForm:
    """One product-path form a platform writes: the path pattern and the query keys it needs."""

    pattern: str
    query_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlatformDetection:
    """How a storefront on this platform is recognised before it is onboarded (ADR-0036 §4 step 2).

    Markers only: literal strings whose presence in a public page names the platform. A marker's
    name is all that leaves recognition; the page itself is never kept and no fact is read from it.
    The paths are the template's own: its login page (where CONNECT signs in), its policy
    documents, and the product path prefix a robots rule is compared with.
    """

    # (marker name, literal) found in the storefront's home page.
    home_markers: tuple[tuple[str, str], ...]
    login_path: str
    # (marker name, literal) found in the template's login page: its login form.
    login_markers: tuple[tuple[str, str], ...]
    robots_path: str
    terms_path: str
    product_path_prefix: str


def markers_in(body: str, markers: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """The names of the markers whose literal ``body`` contains, in declaration order."""
    return tuple(name for name, literal in markers if literal in body)


@dataclass(frozen=True)
class SiteBinding:
    definition: SupplierDefinition
    collection: SupplierCollection


@dataclass(frozen=True)
class PlatformTemplate:
    platform: str
    # The template's EXTRACTOR_REVISION; its manifest is the template package's own.
    revision: str
    label_slots: Mapping[str, tuple[str, ...]]
    region_slots: Mapping[str, SiteRegion]
    path_forms: Mapping[str, PathForm]
    default_limits: Mapping[str, float]
    # The hosts every site on the platform needs for CONNECT, such as a shared secure-login host.
    platform_egress_hosts: frozenset[str]
    # The capture policy's template revision (ADR-0019); a site's policy is derived from it.
    capture_revision: str
    # (site, the site's extraction revision) -> its CONNECT and COLLECT definitions.
    bind: Callable[[SiteConfig, str], SiteBinding]
    # Every image-role rule name the template can assign, with its role: what the image
    # auto-selection reads for a site on this template (Issue #219).
    role_table: tuple[tuple[str, str], ...] = ()
    # ADR-0036 §4 step 2: how a storefront on this platform is recognised before onboarding.
    detection: PlatformDetection | None = None
