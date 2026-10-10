# 건강산 — reconnaissance record (ADR-0030 §8.1)

- **Site:** 건강산, `https://www.ggsan.com` (storefront host `www.ggsan.com`). Platform: Godomall.
- **Owner go-ahead:** Issue #219 `6086058056`. The owner approved the reconnaissance in chat ("정찰진행도 승인할께"), signed in when asked and replied "로그인완료" (2026-10-10).
- **Date:** 2026-10-10.
- **Who signed in:** the owner, in the desktop app's built-in browser. The agent typed no credential and read no credential store.
- **Fixtures:** `tests/fixtures/suppliers/ggsan/1000004918.html` and `1000002412.html`. They are reduced to the product's top section (`.goods_view_top`) and its description section (`#detail`), with the `og:` head elements the template reads. Scripts and the review and inquiry sections were removed. Every `input` keeps only its `id`, `class` and `style`, and no element keeps a `name` or `value`, exactly as the browser capture keeps them (ADR-0019 §6.1). No member-identifying text was present or kept. No order or member page was kept.

## 1. What was read

| read | count |
| --- | --- |
| `robots.txt` | 1 |
| storefront main page (listing links only) | 1 |
| login page (form structure only, nothing typed) | 1 |
| product pages: 1000004918 (on sale), 1000002412 (sold out), 1000000218 (on sale) | 3 |
| `/service/agreement.php` (terms) | 1 |
| search result pages (listing links only) | 2 |

No image was fetched. The ADR-0030 §8.1 bound is six product pages; three were read.

## 2. Observations

- **`robots.txt`:**
  - It is the platform's default list: it names AI and search crawlers, with `Crawl-delay: 10` for the bots it names, and has no `*` group.
  - ICBM reads a product page only when the owner opens it in their own signed-in browser and captures it (ADR-0019). The queue paces captures at 10 s or more (`min_queue_interval_s`).
- **Terms:** §22 is the standard clause on reuse of the mall's copyrighted information. Images are re-hosted through ICBM's own upload; the owner already re-hosts 건강산's images for Naver (Issue #219 `6086199255`).
- **CONNECT:**
  - The login form is `form#formLogin`, with `loginId` and `loginPwd` and a submit button inside `.login_input_sec`.
  - Signed out, `/mypage/index.php` sends the reader to `/member/login.php`.
  - Signed in, the page carries the logout action `/member/logout.php` and the member's order-list link.
- **Product path:**
  - `/goods/goods_view.php?goodsNo=<10 digits>`.
  - Listing links add a tracking query, `mtn=…`.
- **Identity:** the `og:url`'s `goodsNo`, the `상품코드` row, the URL's `goodsNo`, and the `goodsNo[]` form inputs, which appear only when the product is purchasable. All agree. There is no canonical link and no `product:*` meta.
- **Name:** the `og:title` and the `.item_detail_tit h3` heading state the same name.
- **Information rows** (`.item_detail_list`, `dl` > `dt`/`dd`):

  | row | value |
  | --- | --- |
  | `정가` | struck through, `33,000원` |
  | `판매가` | `dl.item_price`, the member price, `15,000원` |
  | `구매제한` | `옵션당 최소 1개` |
  | `배송비` | `dl.item_delivery`: `3,000원 / 주문시결제(선결제)`, with a `금액별배송비` layer: `0원 이상 ~ 200,000원 미만 3,000원`, `200,000원 이상 0원` |
  | `상품코드` | the `goodsNo` |
  | `제조사`, `원산지` | |
  | `상품재고` | sold out only, `0개` |

- **Hidden product state:** the product form `#frmView` mirrors the sale price, the list price, the stock and an option flag in hidden inputs. The browser capture keeps no input value (ADR-0019 §6.1), so the template reads none of them.
- **Order list:** an on-sale product without options has its one order line written in advance (`.item_choice_list` with `tbody#option_display_item_0`). The sold-out capture has no order list.
- **Stock:**
  - On sale: `button.btn_add_cart` and `button.btn_add_order` inside `.btn_choice_box`.
  - Sold out: `.btn_choice_box.btn_restock_box` holds a disabled `button.btn_add_soldout` reading `구매 불가`.
- **Images:**
  - The representative image is the `img` of `a#mainImage`.
  - `#testZoom` holds a hidden magnifier copy.
  - The additional images are the slide items of `.item_photo_slide`.
  - Description images are inside `#detail .txt-manual`.
  - Hosts:
    - `godomall-storage.cdn-nhncommerce.com` serves product images;
    - `cdn-saas-web-216-59.cdn-nhncommerce.com` serves description images, and the skin's icons too. The role rules, not the host, tell them apart.
- **Description** (`#detail .detail_explain_box .txt-manual`):
  - The minimum price is free text after the phrase `판매가격절대준수`: `1개 21,000원 이상`, or `4,400원 이상`.
  - The same block also states a two-unit bundle amount (`2개 묶음 40,900원`) and a shipping-inclusive amount (`배송비 포함 24,000원 이상`).
  - A channel restriction is stated as `★ 쿠팡판매 불가 상품 ★`. A product without one states no restriction.
- **Notice:** a `상품필수 정보` table (`소비기한`).
- **Options:** none of the three products had an option axis (`optionFl` `n`, no option control).

## 3. What does not fit the Cafe24 template, and its resolution

| finding | resolution |
| --- | --- |
| a different platform | the `godomall` template (ADR-0030 §4, S4), `godomall-1` |
| the minimum price is a description sentence | ADR-0034 §1: the site phrase `판매가격절대준수`; a per-unit amount only, bundle and shipping-inclusive amounts excluded; several different amounts go to review |
| a free-over shipping policy | ADR-0034 §2: recorded as `CONDITIONAL` with its base fee and threshold, priced at the base fee (Issue #219 `6088081139`) |
| `판매가` beside `정가` | ADR-0032: 건강산 declares `판매가` as `PURCHASE` and `정가` as `LIST` (Issue #219 `6086421199`) |
| `쿠팡판매 불가` | ADR-0031: the site's `channel_forbid_coupang` phrase |
| the product form's hidden inputs | not read: the capture keeps no input value (ADR-0019 §6.1). "No options" (ADR-0013 ruling B) is read from what the page shows: an order list with exactly one line written in advance and no option control of any kind. A sold-out page has no order list, so its options stay under review |
| the `지역별추가배송비` layer | the captures show the layer empty. An empty layer states no surcharge amount, and its words stay in the shipping policy text beside the price (ADR-0034 §2). A layer that states any amount is a region surcharge and is held for review |
| `mtn=` on listing links | none needed: URL sanitization keeps only `goodsNo`, so `mtn` is dropped before any read |
| a shown `상품필수 정보` table | the template reads it as a notice and holds it for review, as the Cafe24 template does |

## 4. Site configuration and offline proof (ADR-0030 §8.2, S4)

- `integrations/suppliers/sites/ggsan.json`, revision `ggsan-1`, status **`RECON`**: products collected from it are refused by the registration preflight (`SUPPLIER_NOT_ACTIVE`, PT-06) until a reviewed PR flips it to `ACTIVE` with an acceptance record (§8.3).
- Hosts, all observed above: storefront `www.ggsan.com`; images `godomall-storage.cdn-nhncommerce.com`, `cdn-saas-web-216-59.cdn-nhncommerce.com`. Godomall needs no platform login host.
- Words: `판매가` is the purchase price and `정가` the list price; `판매가격절대준수` and `판매가격 준수` are the description minimum phrases (ADR-0034 §1); `쿠팡판매 불가` forbids Coupang.
- No seller-code convention: only the owner declares one (ADR-0024 §2).
- The offline proof is `tests/unit/integrations/suppliers/test_ggsan_site.py`: through the registry, on both captures, the identity, name, member price, description minimum, free-over shipping, stock, sales channel and images are read as recorded above.
