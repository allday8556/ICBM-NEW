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
class PageRow:
    """One label/value row of a product page's information list, as the template scans it."""

    label: str
    value: str


@dataclass(frozen=True)
class PlatformAnalysis:
    """What a trial report reads from a page besides the facts (ADR-0036 §5, slice O3).

    Site knowledge only, like the rest of a template: pure functions over a captured document. It
    reads rows and structure; it never reads a fact, and nothing here decides a field.

    - ``rows`` are the information rows exactly as the template's own row scanner pairs them, so
      a row that no slot's words name is a row the template saw and did not read.
    - ``row_slots`` are the label slots whose words name a row; ``channel_slots`` the slots whose
      phrases read a sales-channel row. Every other slot names a phrase, a mark or a control.
    - ``reads_label`` says whether the template reads a row by its own fixed words, outside any
      slot (a stock row, a notice, a quantity tier): such a row is read, never unread.
    - ``structure`` names the problems in the page's structure that words cannot fix, by the
      template's own rules (ADR-0035): a duplicated order list, for one.
    - ``capture_regions`` are the regions of one site's product page whose text and content the
      template reads (its own, and the site's relocated ones), as ``marks`` tokens. A sample
      capture keeps text only inside them (ADR-0036 §5 step 3), so they name every region the
      template's facts, identity, images and rows read.
    """

    rows: Callable[[str], tuple[PageRow, ...]]
    row_slots: tuple[str, ...]
    channel_slots: tuple[str, ...]
    reads_label: Callable[[str], bool]
    structure: Callable[[str], tuple[str, ...]]
    capture_regions: Callable[[SiteConfig], tuple[str, ...]]


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
    # ADR-0036 §5: how a trial report reads a page's rows and structure (slice O3).
    analysis: PlatformAnalysis | None = None
