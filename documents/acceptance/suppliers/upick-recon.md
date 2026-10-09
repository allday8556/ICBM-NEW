# U-PICK — reconnaissance record (ADR-0030 §8.1)

- **Site:** U-PICK B2B, `https://upickb2b.com`. Platform: Cafe24.
- **Owner go-ahead:** Issue #219 `6086058056` (2026-10-10).
- **Date:** 2026-10-10.
- **Who signed in:** the owner, in the desktop app's built-in browser. The agent typed no credential and read no credential store.
- **Fixtures:** `tests/fixtures/suppliers/upick/4954.html` and `4082.html`. They are reduced to the product module and the description block, with scripts, hidden inputs and inline lazy-load placeholders removed. No member-identifying text was present or kept. No order or member page was kept.

## 1. What was read

| read | count |
| --- | --- |
| `robots.txt` | 1 |
| storefront main page (listing links only) | 3 |
| login page (form structure only, nothing typed) | 1 |
| product pages: 4954 (on sale), 4082 (sold out), 3833 (sold out) | 3 |
| `/product/list.html?cate_no=1` (answered with the platform's error page) | 1 |

No image was fetched. The ADR-0030 §8.1 bound is six product pages; three were read.

## 2. Observations

- **`robots.txt`:**
  - `/product/` is allowed.
  - `/member/`, `/myshop/` and `/exec/front/` are disallowed for crawlers.
  - `Crawl-delay: 10` applies to the named bots.
- **CONNECT:**
  - The login form is the Cafe24 standard, `form[action="/exec/front/Member/login/"]` with `member_id` and `member_passwd`. Its submit control is `a.btnSubmit`, where KM통상's is `a.btnLogin`.
  - Signed in, the page carries `xans-layout-statelogon` and the logout action, as KM통상's does.
- **Product path:**
  - KM통상's `seo` form, `/product/<name>/<number>/category/<n>/display/<n>/`.
  - Listing links append a tracking query, `?icid=MAIN.product_listmain_<n>`.
- **Identity:** `product:productId`, `product:retailer_item_id`, the canonical link and the URL path. All four agree.
- **Name:** the `og:title` appends the shop's name (`… - U-PICK B2B`). The `상품명` row states the product's own name.
- **Information rows** (`xans-product-detaildesign`, th/td):

  | row | value |
  | --- | --- |
  | `상품명` | |
  | `판매가능플랫폼` | `모든마켓 판매가능` / `폐쇄몰` / `폐쇄몰 전용/오픈마켓 판매불가` |
  | `상품 요약설명` | |
  | `최저판매가`, or `폐쇄몰 최저판매가` | `7,900원`, `29,500원 이상` |
  | `판매정책` | |
  | `소비자가` | `16900`, `118,000원` |
  | `회원가` | the member price, `(부가세포함)` |
  | `최대포장수량` | |
  | `배송비` | `3,000원`, or a range `3,000원 ~ 4,000원` with quantity tiers in a tooltip |
  | `추가배송비` | |
  | `소비기한` | |
  | `발주마감` | |
  | `자체상품코드` | |
  | `상품코드` | |

- **Stock:**
  - On sale: `span#actionBuy` inside `a.btnSubmit`, and `button#actionCart.actionCart`.
  - Sold out: a visible `span.sub_sold` reading `SOLD OUT`; the purchase controls are `displaynone`.
- **Images:**
  - The representative image is `img.BigImage` in `.xans-product-image .prdImg`. This skin has no `keyImg`.
  - Additional images are in `xans-product-addimage`.
  - Description images are in `#prdDetail`, lazy-loaded through `ec-data-src`.
  - Hosts:
    - `upickb2b.com`;
    - `thepath.cafe24.com`;
    - `img.echosting.cafe24.com` serves layout assets only, and is not approved.
  - One description image was reported as `4,40 MB`. The owner then set 5 MiB per image for every supplier (Issue #219 `6086299406`).
- **Options:** none of the three products had an option axis. The option container is present and empty.

## 3. What does not fit the template, and its resolution

| finding | resolution |
| --- | --- |
| `og:title` suffix | `cafe24-2`: a title that only extends the `상품명` row reads as the row |
| no `keyImg` | `cafe24-2`: the key-image container is the region slot `key_image`; U-PICK sets `prdImg` |
| smart-design purchase controls | `cafe24-2`: `actionBuy` and `actionCart` join the template's purchase controls |
| `소비자가` beside `회원가` | ADR-0032: U-PICK declares `회원가` as `PURCHASE` and `소비자가` as `LIST` |
| shipping range | ADR-0032 §4: the highest amount |
| `판매가능플랫폼` | ADR-0031: U-PICK's phrases go in its channel slots |
| `?icid=` on listing links | none needed: URL sanitization keeps only a host's safe query keys, so `icid` is dropped before any read |
| login submit `a.btnSubmit` | the `cafe24` template's submit selector names both `a.btnLogin` and `a.btnSubmit` inside the login form |
