"""The Godomall platform template (ADR-0030 §4, ADR-0034).

Written from 건강산's reconnaissance (Issue #219 `6086058056`). A site on Godomall supplies only its
configuration (``integrations/suppliers/sites/<key>.json``); :func:`bind` turns it into the
definitions the application already knows.

**CONNECT.**
- The login form is ``form#formLogin``, with ``loginId`` and ``loginPwd`` and a submit button in
  ``.login_input_sec``. It posts to ``/member/login_ps.php``.
- Signed out, the member page ``/mypage/index.php`` sends the reader to ``/member/login.php``.
- Signed in, the page carries the member logout action (``/member/logout.php``) and the member's
  own order-list link, and no login form.
- A status alone is never proof (ADR-0007 §4).

This package describes; it cannot make a request.
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
from integrations.suppliers.platforms import PlatformTemplate, SiteBinding
from integrations.suppliers.platforms.godomall.collect import (
    DEFAULT_VOCABULARY,
    EXTRACTION_REVISION,
    OG_IMAGE_RULE,
    ROLE_RULES,
    Vocabulary,
    classify_images,
    goods_number,
    parse_fields,
    resolve,
)
from integrations.suppliers.platforms.godomall.collect.identity import (
    SourceIdentity as ParsedIdentity,
)
from integrations.suppliers.platforms.godomall.collect.profile import (
    DEFAULT_LIMITS,
    PATH_FORMS,
    ROBOTS_PATH,
    TERMS_PATH,
)
from integrations.suppliers.site_config import SiteConfig, SiteRegion

__all__ = ["TEMPLATE", "authenticated", "bind", "login_required", "vocabulary"]

# The page the browser opens to sign in (``LoginFormSpec.path``). The common browser transport
# fills the form there and clicks its submit control; the form itself posts to
# ``/member/login_ps.php`` and the browser follows it, exactly as the Cafe24 template opens
# ``/member/login.html`` while its form posts elsewhere (``transport/gateway.py``).
LOGIN_PATH = "/member/login.php"
# The member page: it exists only for a signed-in member. Its contents are never read or kept.
PROTECTED_TARGET = "/mypage/index.php"

_LOGIN_FORM = "form#formLogin"
_LOGIN_FORM_MARKER = 'id="formLogin"'
_LOGOUT_ACTION = "/member/logout.php"
_ORDER_LIST = "/mypage/order_list.php"

REQUEST_POLICY = RequestPolicy(
    max_concurrency=1,
    minimum_request_interval_s=2.0,
    auth_retry_limit=3,
    request_timeout_s=20.0,
)
LABEL_SLOTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "price": DEFAULT_VOCABULARY.price,
        "purchase_price": DEFAULT_VOCABULARY.purchase_price,
        "list_price": DEFAULT_VOCABULARY.list_price,
        "minimum_price": DEFAULT_VOCABULARY.minimum_price,
        "minimum_price_phrase": DEFAULT_VOCABULARY.minimum_price_phrase,
        "shipping_fee": DEFAULT_VOCABULARY.shipping_fee,
        "brand": DEFAULT_VOCABULARY.brand,
        "manufacturer": DEFAULT_VOCABULARY.manufacturer,
        "origin": DEFAULT_VOCABULARY.origin,
        "sold_out": DEFAULT_VOCABULARY.sold_out,
        "sales_channel_row": DEFAULT_VOCABULARY.sales_channel_row,
        "channel_all_allowed": DEFAULT_VOCABULARY.channel_all_allowed,
        "channel_closed_only": DEFAULT_VOCABULARY.channel_closed_only,
        "channel_forbid_coupang": DEFAULT_VOCABULARY.channel_forbid_coupang,
        "channel_forbid_smartstore": DEFAULT_VOCABULARY.channel_forbid_smartstore,
    }
)
REGION_SLOTS: Mapping[str, SiteRegion] = MappingProxyType(
    {
        "detail": SiteRegion("id", DEFAULT_VOCABULARY.detail_container),
        "detail_text": SiteRegion("class", DEFAULT_VOCABULARY.detail_text),
    }
)
REGION_FIELDS: Mapping[str, str] = MappingProxyType(
    {"detail": "detail_container", "detail_text": "detail_text"}
)
CAPTURE_REVISION = "godomall-capture-1"


def login_required(response: ProbeResponse) -> Verdict:
    """The target demands a login: a redirect to the login page, or the login form itself.

    A status alone is never proof (ADR-0007 §4); a denial is recorded and decides nothing.
    """
    signals: list[str] = []
    if response.location and response.location.startswith("/member/login"):
        signals.append("redirect_to_login")
    if _LOGIN_FORM_MARKER in response.body:
        signals.append("login_form")
    proven = bool(signals)
    if response.status in (401, 403):
        signals.append("access_denied")
    return proven, tuple(signals)


def authenticated(response: ProbeResponse) -> Verdict:
    """Only a signed-in member's page carries the logout action and the member's order list, and
    no login form."""
    signals: list[str] = []
    if _LOGOUT_ACTION in response.body:
        signals.append("logout_action")
    if _ORDER_LIST in response.body:
        signals.append("member_order_list")
    proven = (
        response.status == 200 and len(signals) == 2 and _LOGIN_FORM_MARKER not in response.body
    )
    return proven, tuple(signals)


def vocabulary(site: SiteConfig) -> Vocabulary:
    """The template's words with the site's extra words appended, and its relocated regions."""

    def words(slot: str) -> tuple[str, ...]:
        defaults = LABEL_SLOTS[slot]
        extra = site.label_overrides.get(slot, ())
        return defaults + tuple(word for word in extra if word not in defaults)

    def region(slot: str) -> str:
        chosen = site.region_overrides.get(slot)
        if chosen is None:
            return str(getattr(DEFAULT_VOCABULARY, REGION_FIELDS[slot]))
        return chosen.token

    return Vocabulary(
        price=words("price"),
        purchase_price=words("purchase_price"),
        list_price=words("list_price"),
        minimum_price=words("minimum_price"),
        minimum_price_phrase=words("minimum_price_phrase"),
        shipping_fee=words("shipping_fee"),
        brand=words("brand"),
        manufacturer=words("manufacturer"),
        origin=words("origin"),
        sold_out=words("sold_out"),
        sales_channel_row=words("sales_channel_row"),
        channel_all_allowed=words("channel_all_allowed"),
        channel_closed_only=words("channel_closed_only"),
        channel_forbid_coupang=words("channel_forbid_coupang"),
        channel_forbid_smartstore=words("channel_forbid_smartstore"),
        detail_container=region("detail"),
        detail_text=region("detail_text"),
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
        egress_hosts=frozenset({site.storefront_host}) | site.extra_egress_hosts,
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
            username_selector=f'{_LOGIN_FORM} input[name="loginId"]',
            password_selector=f'{_LOGIN_FORM} input[name="loginPwd"]',
            submit_selector=f'{_LOGIN_FORM} .login_input_sec button[type="submit"]',
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
        url_product_hint=goods_number,
    )
    return SiteBinding(definition=definition, collection=collection)


TEMPLATE = PlatformTemplate(
    platform="godomall",
    revision=EXTRACTION_REVISION,
    label_slots=LABEL_SLOTS,
    region_slots=REGION_SLOTS,
    path_forms=PATH_FORMS,
    default_limits=DEFAULT_LIMITS,
    platform_egress_hosts=frozenset(),
    capture_revision=CAPTURE_REVISION,
    bind=bind,
    role_table=tuple((rule.rule_id, rule.role.value) for rule in (*ROLE_RULES, OG_IMAGE_RULE)),
)
