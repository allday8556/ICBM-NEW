"""KM통상's image-role knowledge (Issue #52 comments 5696110694, 5696172833, 5696242775).

The fixture reproduces the containers the two retained `m3-recon-02` product captures actually
use — including the description block's unclosed tags, which is what made document order and
outermost-container matching unusable. No captured page content is copied here.
"""

from urllib.parse import urljoin

import pytest

from app.collect.facts import LocatorForm
from integrations.suppliers.collection import ImageRole, plan_image_sample
from integrations.suppliers.kmretail import IMAGE_ROLES
from integrations.suppliers.kmretail.collect.images import VOID_ELEMENTS, classify_images

PRODUCT_URL = "https://shop.invalid/product/item/1/category/2/display/1/"
STORE = "shop.invalid"
ASSETS = "assets.invalid"  # the third-party host that serves both layout and detail images

PAGE = f"""<html><head>
<meta property="og:image" content="https://{STORE}/web/product/big/1/key.jpg">
</head><body id="main">
<div class="storyTeller"><img src="https://{ASSETS}/xYz"></div>
<div class="promotionBanner"><a class="bannerLink"><img src="https://{ASSETS}/aBc"></a>
  <a class="btnClose"><img src="/SkinImg/img/btn_close.png"></a></div>
<div class="clearfix"><h1 class="xans-element- xans-layout xans-layout-logotop">
  <a class="opacity"><img src="https://{ASSETS}/dEf"></a></h1></div>
<div id="top_menu"><div id="category-lnb" class="xans-element- xans-layout xans-layout-category">
  <ul class="category_img"><li><a><img src="https://{ASSETS}/gHi"></a></li>
  <li><img src="/SkinImg/img/topsub1.jpg"></li></ul></div></div>
<div id="contents"><div class="xans-element- xans-product xans-product-detail">
  <div class="detailArea">
    <div class="xans-element- xans-product xans-product-image">
      <div class="keyImg"><a>
        <img class="BigImage" src="//{STORE}/web/product/big/1/key.jpg"></a></div>
      <div class="xans-element- xans-product xans-product-addimage">
        <ul><li class="xans-record-">
          <img class="ThumbImage" src="//{STORE}/web/product/small/1/t.jpg"></li></ul>
      </div>
    </div>
    <div class="infoArea">
      <span class="icon"><img src="//icons.invalid/icon/product/global/icon_global_1.gif"></span>
      <p class="displaynone"><img src="//icons.invalid/skin/base_ko_KR/product/txt_naver.gif"></p>
      <table><tbody class="xans-element- xans-product xans-product-option"><tr class="displaynone">
        <td class="selectButton"><a><img src="//icons.invalid/skin/btn_manual_select.gif"></a></td>
      </tr></tbody></table>
      <table><tbody><tr><td><span class="quantity">
        <a><img class="QuantityUp up" src="//icons.invalid/skin/btn_count_up.gif"></a>
      </span></td></tr></table>
      <div class="xans-element- xans-product xans-product-action"><div>
        <div class="xans-element- xans-photoslide2 xans-photoslide2-slide-1">
          <img src="https://{ASSETS}/jKl"></div></div></div>
    </div>
  </div>
  <div class="xans-element- xans-product xans-product-additional"><div id="prdDetail">
    <div class="cont"><center>
      <img ec-data-src="//{ASSETS}/d01"><br>
      <img ec-data-src="//{ASSETS}/d02"><br>
      <img ec-data-src="https://{ASSETS}/d03"><br>
      <img ec-data-src="https://{ASSETS}/d04"><br>
      <img ec-data-src="https://{ASSETS}/d05"><br>
      <img ec-data-src="//{ASSETS}/d06">
  <div id="prdReview"><div class="board"><p class="btnArea">
    <a><img src="/SkinImg/img/d_write.gif"></a>
    <a><img src="/SkinImg/img/d_all.gif"></a></p></div></div>
  <div id="prdQnA"><div class="board"><p class="btnArea">
    <a><img src="/SkinImg/img/d_write.gif"></a>
    <a><img src="/SkinImg/img/d_all.gif"></a></p></div></div>
  <div id="addr"><div><div class="fic"><a><img src="https://{ASSETS}/mNo"></a></div></div></div>
  <div id="progressPaybar"><div id="progressPaybarView"><div class="box"><p class="graph">
    <span><img src="//popup.invalid/images/ec_hosting/popup/layer_guide/img_loading_bar.gif"></span>
  </p></div></div></div>
  </div></div></div></div></div>
</body></html>"""


def roles() -> list[tuple[ImageRole, str, str]]:
    return [(c.role, c.rule, c.host) for c in classify_images(PAGE, PRODUCT_URL)]


def test_the_product_module_marks_its_own_images() -> None:
    found = {(role, rule) for role, rule, _ in roles()}
    assert (ImageRole.PRIMARY, "km.primary.key_image") in found
    assert (ImageRole.PRIMARY, "km.primary.og_image") in found
    assert (ImageRole.THUMBNAIL, "km.thumbnail.additional") in found
    assert (ImageRole.PRODUCT_AUX, "km.aux.photoslide") in found
    assert sum(1 for role, _, _ in roles() if role is ImageRole.DETAIL) == 6


def test_the_description_block_does_not_swallow_what_follows_it() -> None:
    # The block is written with unclosed tags, so the boards, the footer and the hosting popup are
    # nested inside it as a parser sees the document. The innermost container still decides.
    by_rule = {rule: role for role, rule, _ in roles()}
    assert by_rule["km.ui.community_board"] is ImageRole.UI_COMMON
    assert by_rule["km.ui.footer"] is ImageRole.UI_COMMON
    assert by_rule["km.ui.hosting_popup"] is ImageRole.UI_COMMON
    details = [c for c in classify_images(PAGE, PRODUCT_URL) if c.role is ImageRole.DETAIL]
    assert {c.host for c in details} == {ASSETS}, "only the description sequence is detail"


def test_a_container_no_rule_knows_is_unknown_not_a_product_image() -> None:
    unknown = [c for c in classify_images(PAGE, PRODUCT_URL) if c.role is ImageRole.UNKNOWN]
    assert [c.rule for c in unknown] == ["km.unknown"]
    assert unknown[0].host == ASSETS, "a product host is still not evidence of a product role"


def test_a_role_is_never_read_from_a_host_or_a_path() -> None:
    # The same third-party host serves the header logo, the navigation, the footer and the whole
    # description sequence; the storefront host serves the representative image and board buttons.
    by_host: dict[str, set[ImageRole]] = {}
    for role, _, host in roles():
        by_host.setdefault(host, set()).add(role)
    assert by_host[ASSETS] >= {ImageRole.DETAIL, ImageRole.UI_COMMON, ImageRole.UNKNOWN}
    assert by_host[STORE] >= {ImageRole.PRIMARY, ImageRole.UI_COMMON}


def test_the_sample_takes_the_representative_image_and_the_detail_sequence_only() -> None:
    # Comment 5696172833 §6 with the eligible host set: the six slots carry product evidence and
    # not one common-layout asset.
    hosts = {STORE, ASSETS}
    eligible = [c for c in classify_images(PAGE, PRODUCT_URL) if c.host in hosts]
    plan = plan_image_sample(eligible, 6, rules=IMAGE_ROLES.identity)
    assert [c.role for c in plan.selected] == [ImageRole.PRIMARY, *[ImageRole.DETAIL] * 5]
    assert plan.selected[0].host == STORE
    assert {c.host for c in plan.selected[1:]} == {ASSETS}
    assert not [c for c in plan.selected if c.role in (ImageRole.UI_COMMON, ImageRole.UNKNOWN)]
    # The page declares its representative image twice; both writings name one asset and one slot.
    assert len({c.identity for c in plan.selected}) == 6
    audit = plan.audit()
    assert audit["rules"] == IMAGE_ROLES.identity
    assert {entry["reason"] for entry in audit["not_selected"]} >= {
        "UI_COMMON",
        "duplicate normalized URL",
        "lower sampling priority",
    }


def test_an_absent_product_module_yields_no_product_sample() -> None:
    bare = '<html><body><div class="promotionBanner"><img src="/a.png"></div></body></html>'
    plan = plan_image_sample(classify_images(bare, PRODUCT_URL), 6, rules=IMAGE_ROLES.identity)
    assert plan.selected == (), "a page without product imagery is sampled zero times"


# ---------------------------------------------------------------- the ancestry a role is read from
# Review 5222192371: a void element has no end tag, and a page's closes are not to be trusted. Both
# used to shift the stack, leaving the description block standing over the rest of the document so
# that an unrelated image was promoted to DETAIL and could consume a Phase-B request.

OUTSIDE = f'<div class="storyTeller"><img src="https://{ASSETS}/outside"></div>'


def outside_role(markup: str) -> ImageRole:
    """The role of the one image that sits outside every product container."""
    page = f"<html><body>{markup}{OUTSIDE}</body></html>"
    outside = [c for c in classify_images(page, PRODUCT_URL) if c.url.endswith("/outside")]
    assert len(outside) == 1
    return outside[0].role


@pytest.mark.parametrize(
    "void",
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    ],
)
def test_no_void_element_ever_becomes_ancestry(void: str) -> None:
    # One of each, inside the description block, then the block closes normally.
    inside = f'<div id="prdDetail"><div class="cont"><{void}></div></div>'
    assert outside_role(inside) is ImageRole.UNKNOWN


def test_a_void_element_is_not_an_ancestor_of_what_follows_it() -> None:
    # The sharpest form of the drift: a void element that carries a product marker. Kept on the
    # stack it would stand over its siblings, and the next reference would inherit its role.
    page = (
        '<html><body><div class="xans-element- xans-product xans-product-image">'
        f'<img class="keyImg" src="https://{STORE}/web/product/big/1/key.jpg">'
        f'<img src="https://{ASSETS}/after">'
        "</div></body></html>"
    )
    found = classify_images(page, PRODUCT_URL)
    assert [c.role for c in found] == [ImageRole.PRIMARY, ImageRole.UNKNOWN]
    assert found[1].rule == "km.unknown", "a sibling never inherits a void element's role"


def test_a_run_of_void_elements_does_not_shift_the_stack() -> None:
    every = "".join(f"<{void}>" for void in sorted(VOID_ELEMENTS))
    assert outside_role(f'<div id="prdDetail"><div class="cont">{every}</div></div>') is (
        ImageRole.UNKNOWN
    )


def test_self_closing_and_plain_void_markup_classify_the_same() -> None:
    plain = f'<div id="prdDetail"><img src="https://{ASSETS}/d"><br></div>'
    closed = f'<div id="prdDetail"><img src="https://{ASSETS}/d"/><br/></div>'
    assert outside_role(plain) is outside_role(closed) is ImageRole.UNKNOWN
    inside_plain = classify_images(f"<html><body>{plain}</body></html>", PRODUCT_URL)
    inside_closed = classify_images(f"<html><body>{closed}</body></html>", PRODUCT_URL)
    assert [c.role for c in inside_plain] == [c.role for c in inside_closed] == [ImageRole.DETAIL]


def test_a_close_tag_closes_the_nearest_element_of_its_own_name() -> None:
    # The page closes a <div> while a <span> and a <p> are still open. That close reaches the div
    # and takes the unclosed elements above it; a blind pop would have removed only the <p>, and
    # the description block would then have ended the document unclosed and proved nothing.
    closed = (
        f'<html><body><div id="prdDetail"><span><p>'
        f'<img src="https://{ASSETS}/inside"></div></body></html>'
    )
    assert [c.role for c in classify_images(closed, PRODUCT_URL)] == [ImageRole.DETAIL]
    # Matching is by name and nearest-first, so a close only ever reaches its own element: here the
    # inner div closes and the description block is left open, which proves nothing at all.
    inner = (
        f'<html><body><div id="prdDetail"><div class="cont">'
        f'<img src="https://{ASSETS}/inside"></div></body></html>'
    )
    assert [c.role for c in classify_images(inner, PRODUCT_URL)] == [ImageRole.UNKNOWN]


def test_a_close_with_nothing_open_to_match_is_ignored() -> None:
    # A stray </section> must not pop the container that proves the representative image.
    page = (
        '<html><body><div class="xans-element- xans-product xans-product-image">'
        '<div class="keyImg"></section>'
        f'<a><img class="BigImage" src="https://{STORE}/web/product/big/1/key.jpg"></a>'
        "</div></div></body></html>"
    )
    found = classify_images(page, PRODUCT_URL)
    assert [(c.role, c.rule) for c in found] == [(ImageRole.PRIMARY, "km.primary.key_image")]


def test_broken_nesting_cannot_promote_a_later_image_into_the_sample() -> None:
    # The description block is never closed and carries void elements; the image that follows it
    # in its own container stays UNKNOWN, and UNKNOWN cannot consume a slot.
    broken = (
        '<div id="prdDetail"><div class="cont"><center>'
        f'<img ec-data-src="https://{ASSETS}/d01"><br>'
        f'<img ec-data-src="https://{ASSETS}/d02"><br>'
        "</center></div></div></div></div>"  # more closes than the page ever opened
    )
    page = f"<html><body>{broken}{OUTSIDE}</body></html>"
    found = classify_images(page, PRODUCT_URL)
    assert [c.role for c in found] == [ImageRole.DETAIL, ImageRole.DETAIL, ImageRole.UNKNOWN]
    plan = plan_image_sample(found, 6, rules=IMAGE_ROLES.identity)
    assert [c.role for c in plan.selected] == [ImageRole.DETAIL, ImageRole.DETAIL]
    assert not [c for c in plan.selected if c.url.endswith("/outside")]


def test_recovery_only_ever_narrows_what_is_called_a_product_image() -> None:
    # The same references, once in well-formed markup and once in markup whose closes are wrong.
    inner = (
        '<div class="xans-element- xans-product xans-product-image"><div class="keyImg"><a>'
        f'<img class="BigImage" src="https://{STORE}/web/product/big/1/key.jpg"></a></div></div>'
    )
    tidy = classify_images(f"<html><body>{inner}</body></html>", PRODUCT_URL)
    torn = classify_images(f"<html><body></div></p>{inner}</span></body></html>", PRODUCT_URL)
    product = {ImageRole.PRIMARY, ImageRole.DETAIL, ImageRole.THUMBNAIL, ImageRole.PRODUCT_AUX}
    tidy_products = sum(1 for c in tidy if c.role in product)
    torn_products = sum(1 for c in torn if c.role in product)
    assert torn_products <= tidy_products == 1


# ---------------------------------------------------------------- what an unclosed scope proves
# Review 5222613374: a scope only proves what it contains once the page closes it. One that runs to
# the end of the document open stands over everything after it, so every sample-eligible role it
# proved is withdrawn before anything is sampled — the real references inside it included.

# The review's own shape: nothing closes #prdDetail, and no surplus close stands in for it.
NEVER_CLOSED = (
    f'<div id="prdDetail">\n  <img src="https://{ASSETS}/detail1.jpg">\n'
    f'<div class="new-unrecognised-section">\n  <img src="https://{ASSETS}/outside.jpg">\n'
)
PROPERLY_CLOSED = (
    f'<div id="prdDetail">\n  <img src="https://{ASSETS}/detail1.jpg">\n</div>\n'
    f'<div class="new-unrecognised-section">\n  <img src="https://{ASSETS}/outside.jpg">\n</div>\n'
)
REPRESENTATIVE = f'<meta property="og:image" content="https://{STORE}/web/product/big/1/key.jpg">'


def classified(markup: str, head: str = "") -> list[tuple[ImageRole, str]]:
    page = f"<html><head>{head}</head><body>{markup}</body></html>"
    return [(c.role, c.rule) for c in classify_images(page, PRODUCT_URL)]


def test_a_scope_the_page_never_closed_proves_nothing() -> None:
    roles = classified(NEVER_CLOSED)
    assert [role for role, _ in roles] == [ImageRole.UNKNOWN, ImageRole.UNKNOWN]
    # Both the genuine reference inside the block and the later one that inherited it are withdrawn,
    # and the audit still names the role the parse had provisionally reached.
    assert all(rule.startswith("km.unclosed_scope:km.detail.prd_detail") for _, rule in roles)


def test_an_unclosed_scope_cannot_consume_a_sample_slot() -> None:
    found = classify_images(f"<html><body>{NEVER_CLOSED}</body></html>", PRODUCT_URL)
    plan = plan_image_sample(found, 6, rules=IMAGE_ROLES.identity)
    assert plan.selected == (), "no network request is authorised by an unclosed scope"
    assert {reason for _, reason in plan.excluded} == {"UNKNOWN"}


def test_being_unwound_by_an_ancestor_is_not_the_page_closing_the_scope() -> None:
    # </body> removes #prdDetail with it, but the page never said where the block ended.
    assert [role for role, _ in classified(NEVER_CLOSED)] == [ImageRole.UNKNOWN] * 2


def test_the_same_document_with_the_block_closed_keeps_its_detail_sequence() -> None:
    roles = classified(PROPERLY_CLOSED)
    assert roles == [
        (ImageRole.DETAIL, "km.detail.prd_detail"),
        (ImageRole.UNKNOWN, "km.unknown"),
    ]
    found = classify_images(f"<html><body>{PROPERLY_CLOSED}</body></html>", PRODUCT_URL)
    assert [c.role for c in plan_image_sample(found, 6, rules="r").selected] == [ImageRole.DETAIL]


def test_an_independently_proven_primary_survives_an_unclosed_block() -> None:
    # The page's own declaration of its representative image is proved by the element that carries
    # it, not by a container, so no missing close can withdraw it (review 5222613374 §4).
    roles = classified(NEVER_CLOSED, head=REPRESENTATIVE)
    assert roles[0] == (ImageRole.PRIMARY, "km.primary.og_image")
    assert [role for role, _ in roles[1:]] == [ImageRole.UNKNOWN, ImageRole.UNKNOWN]
    found = classify_images(
        f"<html><head>{REPRESENTATIVE}</head><body>{NEVER_CLOSED}</body></html>", PRODUCT_URL
    )
    plan = plan_image_sample(found, 6, rules=IMAGE_ROLES.identity)
    assert [c.role for c in plan.selected] == [ImageRole.PRIMARY], "one proven image, nothing else"


def test_a_safe_exclusion_is_never_promoted_while_recovering() -> None:
    # UI_COMMON needs no rescue and must not become sampleable because a scope went unclosed.
    markup = f'<div class="promotionBanner"><img src="https://{ASSETS}/banner">{NEVER_CLOSED}'
    roles = classified(markup)
    assert roles[0] == (ImageRole.UI_COMMON, "km.ui.promotion_banner")
    assert not [role for role, _ in roles if role in (ImageRole.DETAIL, ImageRole.PRIMARY)]


# ---------------------------------------------------------------- what the page wrote (PR-1)


def test_every_reference_keeps_what_the_page_wrote_beside_what_it_resolves_to() -> None:
    # Issue #52 ruling 5716978033: the written reference is reported, and resolving it is the only
    # thing the parser does to it — trimming and joining against the page, nothing repaired.
    for candidate in classify_images(PAGE, PRODUCT_URL):
        assert candidate.source is not None
        assert candidate.url == urljoin(PRODUCT_URL, candidate.source.strip())


def test_a_written_form_is_reported_as_written_and_never_repaired() -> None:
    written = (
        f" //{ASSETS}/w1 ",
        f"http://{ASSETS}/w2",
        "/rel/w3.jpg",
        f"https://{ASSETS}/w4#frame",
        f"https://{ASSETS}/w 5",
    )
    markup = (
        '<div id="prdDetail"><div class="cont">'
        + "".join(f'<img ec-data-src="{value}">' for value in written)
        + "</div></div>"
    )
    found = classify_images(markup, PRODUCT_URL)
    assert [c.source for c in found] == list(written)
    assert [c.role for c in found] == [ImageRole.DETAIL] * len(written)
    assert [c.url for c in found] == [
        f"https://{ASSETS}/w1",
        f"http://{ASSETS}/w2",  # still http: the parser never upgrades a scheme
        f"https://{STORE}/rel/w3.jpg",
        f"https://{ASSETS}/w4#frame",  # the fragment stays for the target check to judge
        f"https://{ASSETS}/w 5",
    ]
    assert [c.source_form for c in found] == [
        (LocatorForm.PROTOCOL_RELATIVE, True),
        (LocatorForm.ABSOLUTE, False),
        (LocatorForm.RELATIVE, False),
        (LocatorForm.ABSOLUTE, False),
        (LocatorForm.ABSOLUTE, False),
    ]
