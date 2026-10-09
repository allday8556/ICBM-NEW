"""The Cafe24 platform template (ADR-0030 §4).

Derived from KM통상's package, which was a Cafe24 reader in all but name: the CONNECT predicates
below are KM통상's, and the collection rules are ``collect/``. A site on Cafe24 supplies only its
configuration (``integrations/suppliers/sites/<key>.json``); :func:`bind` turns it into the
definitions the application already knows.

Login state on Cafe24 is rendered server-side by the ``xans-layout-statelogon`` /
``xans-layout-statelogoff`` modules. An unauthenticated GET of the member page can answer HTTP 200
with the logged-off module and a script that sends the browser to the login page, so neither the
status nor the page skeleton proves authentication: the authenticated predicate requires the
logged-on module and the member logout action together (observed on KM통상, 2026-09-13).

This package describes; it cannot make a request. The common CONNECT and COLLECT layers fetch, log
in and hand these functions nothing but immutable views.
"""

from collections.abc import Mapping
from types import MappingProxyType

from app.stages.collect.facts import FieldFact
from integrations.suppliers.base import (
    LoginFormSpec,
    ProbeResponse,
    ProtectedReadProbe,
    RequestPolicy,
    SupplierDefinition,
    SupplierProfile,
    Verdict,
)
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    ImageCandidate,
    ImageRoleRules,
    QueueLimits,
    SourceIdentity,
    SourceIdentityResult,
    SupplierCollection,
    UnresolvedIdentity,
)
from integrations.suppliers.platforms import PathForm, PlatformTemplate, SiteBinding
from integrations.suppliers.platforms.cafe24.collect import (
    DEFAULT_VOCABULARY,
    EXTRACTION_REVISION,
    Vocabulary,
    classify_images,
    parse_fields,
    resolve,
)
from integrations.suppliers.platforms.cafe24.collect.identity import (
    SourceIdentity as ParsedIdentity,
)
from integrations.suppliers.platforms.cafe24.collect.identity import _path_number
from integrations.suppliers.site_config import SiteConfig, SiteRegion

__all__ = ["TEMPLATE", "authenticated", "bind", "login_required"]

LOGIN_PATH = "/member/login.html"
# 마이쇼핑: exists only for a signed-in member. Its contents are never read or kept.
PROTECTED_TARGET = "/myshop/index.html"
ROBOTS_PATH = "/robots.txt"
TERMS_PATH = "/member/agreement.html"
# Cafe24's secure-login host, which encrypts the login form for every shop on the platform.
SECURE_LOGIN_HOST = "login2.cafe24ssl.com"

_LOGIN_FORM = 'form[action="/exec/front/Member/login/"]'
_STATE_LOGOFF = "xans-layout-statelogoff"
_STATE_LOGON = "xans-layout-statelogon"
_LOGIN_CHECK_REDIRECT = "toMoveLoginCheckModule"
_LOGOUT_ACTION = "/exec/front/Member/logout/"
# Informational only (never decide the verdict): recorded as signal names, never page content.
_INFORMATIONAL = {"로그아웃": "logout_text", "/member/modify.html": "member_modify_link"}

# The product path form Cafe24's search-friendly URLs use, with the listing segments an operator's
# URL may carry after it. A site names a form; it never writes a pattern (ADR-0030 §3).
PATH_FORMS: Mapping[str, PathForm] = MappingProxyType(
    {"seo": PathForm(r"/product/[^/]+/\d+(?:/category/\d+)?(?:/display/\d+)?/?")}
)
# KM통상's frozen limits are the template defaults, with the owner's 5 MiB per image for every
# supplier (Issue #219 6086299406). A site may lower them; raising one needs the owner's recorded
# decision (ADR-0030 §3, PT-12).
DEFAULT_LIMITS: Mapping[str, float] = MappingProxyType(
    {
        "max_image_refs": 30,
        "max_image_bytes": 5 * 1024 * 1024,
        "max_image_requests_per_run": 30,
        "max_new_image_bytes_per_run": 24 * 1024 * 1024,
        "same_product_interval_s": 60.0,
        "max_discovered_links": 1000,
        "max_queue_products": 500,
        "min_queue_interval_s": 10.0,
        "issue_ttl_s": 120.0,
    }
)
REQUEST_POLICY = RequestPolicy(
    max_concurrency=1,
    minimum_request_interval_s=2.0,
    auth_retry_limit=3,
    request_timeout_s=20.0,
)
# The words a site may extend, by slot, with the template's defaults.
LABEL_SLOTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "name": DEFAULT_VOCABULARY.name,
        "price": DEFAULT_VOCABULARY.price,
        "minimum_price": DEFAULT_VOCABULARY.minimum_price,
        "shipping_method": DEFAULT_VOCABULARY.shipping_method,
        "shipping_fee": DEFAULT_VOCABULARY.shipping_fee,
        "brand": DEFAULT_VOCABULARY.brand,
        "manufacturer": DEFAULT_VOCABULARY.manufacturer,
        "origin": DEFAULT_VOCABULARY.origin,
        "sold_out": DEFAULT_VOCABULARY.sold_out,
    }
)
# The regions a site may relocate, by slot, with the template's defaults.
REGION_SLOTS: Mapping[str, SiteRegion] = MappingProxyType(
    {
        "detail": SiteRegion("id", DEFAULT_VOCABULARY.detail_container),
        "option": SiteRegion("class", DEFAULT_VOCABULARY.option_container),
        "detail_menu": SiteRegion("class", DEFAULT_VOCABULARY.detail_menu),
    }
)
CAPTURE_REVISION = "cafe24-capture-1"


def login_required(response: ProbeResponse) -> Verdict:
    """The target demands a login: redirect to it, denial, or the logged-off markers."""
    signals: list[str] = []
    if response.location and response.location.startswith("/member/login"):
        signals.append("redirect_to_login")
    if response.status in (401, 403):
        signals.append("access_denied")
    if _STATE_LOGOFF in response.body:
        signals.append("state_logoff")
    if _LOGIN_CHECK_REDIRECT in response.body:
        signals.append("login_check_redirect")
    return bool(signals), tuple(signals)


def authenticated(response: ProbeResponse) -> Verdict:
    """Only a signed-in member's page carries the logged-on module and the logout action."""
    signals: list[str] = []
    if _STATE_LOGON in response.body:
        signals.append("state_logon")
    if _LOGOUT_ACTION in response.body:
        signals.append("logout_action")
    proven = response.status == 200 and len(signals) == 2
    signals.extend(name for marker, name in _INFORMATIONAL.items() if marker in response.body)
    return proven, tuple(signals)


def vocabulary(site: SiteConfig) -> Vocabulary:
    """The template's words with the site's extra words appended, and its relocated regions."""

    def words(slot: str) -> tuple[str, ...]:
        defaults = LABEL_SLOTS[slot]
        extra = site.label_overrides.get(slot, ())
        return defaults + tuple(word for word in extra if word not in defaults)

    def region(slot: str) -> str:
        return site.region_overrides.get(slot, REGION_SLOTS[slot]).token

    return Vocabulary(
        name=words("name"),
        price=words("price"),
        minimum_price=words("minimum_price"),
        shipping_method=words("shipping_method"),
        shipping_fee=words("shipping_fee"),
        brand=words("brand"),
        manufacturer=words("manufacturer"),
        origin=words("origin"),
        sold_out=words("sold_out"),
        option_container=region("option"),
        detail_container=region("detail"),
        detail_menu=region("detail_menu"),
    )


def _identity(document: DocumentView, source_url: str) -> SourceIdentityResult:
    outcome = resolve(document, source_url)
    if isinstance(outcome, ParsedIdentity):
        return SourceIdentity(outcome.source_product_id, outcome.agreed)
    return UnresolvedIdentity(outcome.reason, seen=outcome.seen, missing=outcome.missing)


def bind(site: SiteConfig, extraction_revision: str) -> SiteBinding:
    """One site's CONNECT and COLLECT definitions. The site was validated against this template's
    slots, forms and limits before it gets here (``integrations/suppliers/sites.py``)."""
    words = vocabulary(site)
    limits = {**DEFAULT_LIMITS, **site.limits}
    form = PATH_FORMS[site.product_path_form]
    profile = SupplierProfile(
        supplier_key=site.supplier_key,
        display_name=site.display_name,
        base_url=site.base_url,
        auth_required=True,
        egress_hosts=frozenset({site.storefront_host, SECURE_LOGIN_HOST}) | site.extra_egress_hosts,
        request_policy=REQUEST_POLICY,
    )
    definition = SupplierDefinition(
        profile=profile,
        probe=ProtectedReadProbe(
            target=PROTECTED_TARGET,
            unauthenticated_expectation=login_required,
            authenticated_predicate=authenticated,
        ),
        login=LoginFormSpec(
            path=LOGIN_PATH,
            username_selector=f'{_LOGIN_FORM} input[name="member_id"]',
            password_selector=f'{_LOGIN_FORM} input[name="member_passwd"]',
            submit_selector=f"{_LOGIN_FORM} a.btnLogin",
        ),
    )

    def fields(document: DocumentView) -> Mapping[str, FieldFact]:
        return parse_fields(document, words)

    def classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
        return classify_images(body, product_url, words)

    collection = SupplierCollection(
        profile=CollectionProfile(
            supplier=profile,
            product_path=form.pattern,
            policy_paths=frozenset({ROBOTS_PATH, TERMS_PATH}),
            image_hosts=site.image_hosts,
            safe_query_keys=({site.storefront_host: form.query_keys} if form.query_keys else {}),
            limits=CollectionLimits(
                max_image_refs=int(limits["max_image_refs"]),
                max_image_bytes=int(limits["max_image_bytes"]),
                max_image_requests_per_run=int(limits["max_image_requests_per_run"]),
                max_new_image_bytes_per_run=int(limits["max_new_image_bytes_per_run"]),
                same_product_interval_s=float(limits["same_product_interval_s"]),
            ),
            queue_limits=QueueLimits(
                max_discovered_links=int(limits["max_discovered_links"]),
                max_queue_products=int(limits["max_queue_products"]),
                min_queue_interval_s=float(limits["min_queue_interval_s"]),
                issue_ttl_s=float(limits["issue_ttl_s"]),
            ),
        ),
        roles=ImageRoleRules(identity=extraction_revision, classify=classify),
        identity=_identity,
        fields=fields,
        url_product_hint=_path_number,
    )
    return SiteBinding(definition=definition, collection=collection)


TEMPLATE = PlatformTemplate(
    platform="cafe24",
    revision=EXTRACTION_REVISION,
    label_slots=LABEL_SLOTS,
    region_slots=REGION_SLOTS,
    path_forms=PATH_FORMS,
    default_limits=DEFAULT_LIMITS,
    platform_egress_hosts=frozenset({SECURE_LOGIN_HOST}),
    capture_revision=CAPTURE_REVISION,
    bind=bind,
)
