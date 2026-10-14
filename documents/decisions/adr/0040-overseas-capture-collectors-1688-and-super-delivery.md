# ADR-0040 — Overseas capture collectors: 1688 and Super Delivery, one page the user captures, and 1688's embedded product data

Status: **PROPOSED** 2026-10-14.

Decision owners:
- **Product direction:** the owner, in Issue #219:
  - `6158621894`: collect by product page; read exactly what the page states; the purchase cost is the price actually paid;
  - `6158624981`: the user captures one page with the extension; no list or whole-site crawling;
  - `6158718055`: for 1688, read the page's embedded product data, never executed, and only the paths this ADR names.
- **Already decided by canon:**
  - ADR-0019: the extension transport, `BrowserCapturePolicy`, the capture order, server-fetched images;
  - ADR-0010: facts are quoted, never guessed;
  - ADR-0030 §7: a supplier is `RECON` until its acceptance, then `ACTIVE`;
  - ADR-0038: source currency, FX snapshot, landed cost, and the 2026-10-14 reconnaissance (§9).
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- two packaged suppliers, `ali1688` and `superdelivery`, each with its own collector package;
- an `EXTENSION` capture of one product page per click, for each;
- for `ali1688` only, one **embedded product data** member of the capture (§3), which amends ADR-0019 for that supplier;
- a currency other than KRW on their revisions, through ADR-0038 slice O1.

What it does not authorize:
- a `DIRECT_URL` read, a list queue, or any crawl of these sites;
- a credential for either site held by ICBM;
- a browser byte relay;
- executing anything the page contains;
- any LIVE write.

Recorded by: Claude Code (Track B).

Date: 2026-10-14

---

## 1. One page, captured by the user

- **The only transport** for both suppliers is `EXTENSION`, started by the user on an open product page (ADR-0019 §1, §3).
  - The user's own signed-in browser session decides what the page shows: a Super Delivery wholesale price appears only for an approved company.
  - ICBM stores no credential for either site, and never reads one.
- **No other transport.** Their `CollectionProfile` declares no `DIRECT_URL` transport and no queue limits. A list queue therefore refuses before any read (ADR-0019 AC-28), and the server never fetches a product page of either site.
- **Images** are fetched by the server's policed fetch from the reviewed image hosts (ADR-0019 §7):
  - 1688: `cbu01.alicdn.com` and the other `*.alicdn.com` image hosts that reconnaissance names;
  - Super Delivery: its own image host.
  - *(Amendment, slice C2: reconnaissance named `cbu01.alicdn.com` and `img.alicdn.com` for 1688, and a description document can reference either. 1688's description images come from a public description document the server reads itself, §3.1.)*
- **Host permissions.** The extension gains the two product hosts, `detail.1688.com` and `www.superdelivery.com`, as reviewed supplier hosts (AC-07). The optional per-origin permissions of ADR-0036 D3 are not required by this ADR.

## 2. Super Delivery reads the DOM

- Its `BrowserCapturePolicy` takes the product block, the price block, the description and specification blocks, and the company's terms and shipping block.
  - It excludes the header, the member menu, the coupon and point widgets, reviews, and recommendations.
  - It keeps ADR-0019's no-script rule unchanged.
- **Fields** (ADR-0038 §9.3):
  - `prices`: JPY. The purchase price is the **税込** price of one set. The 税抜 price, the maker's suggested retail price (メーカー希望小売価格) and the stated wholesale ratio are kept as stated.
  - **Undisclosed price.** A page whose price is undisclosed ("卸価格を見る（申請）") has `prices` `REVIEW_REQUIRED` with the reason `PRICE_NOT_DISCLOSED`. Nothing is guessed.
  - `options`: each SD item number of the page (`…S1`, `…S2`, such as a ×18 set) with its own price.
  - `stock`: as stated ("在庫有り" or sold out). A company that keeps no stock count reads as stated.
  - `shipping`:
    - a single nationwide flat fee with a free-over threshold reads as a `CONDITIONAL` fee in JPY;
    - regional surcharges are kept as words (as ADR-0035 RS-01 does for KRW);
    - anything else is `REVIEW_REQUIRED`.
  - `brand`, `manufacturer`, `origin`, the JAN and the description: as stated.
- **Company terms.** Text such as "日本国内の小売店様向け" or "ドロップシッピング不可" is recorded as the company's quoted words (ADR-0038 §9.2).
  - It names no marketplace, so it is not a sales-channel restriction (ADR-0031).
  - It is not a gate. It is shown with the product.

## 3. 1688 reads its embedded product data

**Why.** The rendered page shows the price and stock of the selected option only. The page carries every option's price, stock and freight in one inline script whose single argument is a JSON object:

```text
window.context=(function(b,d){…})(window.contextPath,{ … JSON … });
```

**What is captured** (amends ADR-0019 §6 and AC-12 for `ali1688` only):
- **The DOM capture is unchanged.** It is the product root and regions under `BrowserCapturePolicy`, with `script` excluded.
- **One embedded-data member** is added beside it. The policy declares an `embedded_data` entry:
  - the inline script's required **prefix** (`window.contextPath = "/default";` followed by `window.context=(`);
  - the **argument marker** (`})(window.contextPath,`);
  - the **terminator** (`);` at the end of the script);
  - a **byte bound**.
- **The scope cut happens in the browser** (ADR-0019 §6: scope first). The extension:
  - finds **exactly one** inline script, with no `src`, that matches the prefix. With zero or more than one match, nothing is sent for the member, and the capture says so;
  - cuts the argument between the marker and the terminator, under the byte bound;
  - parses it with `JSON.parse`, which is a data parser, under a depth bound. It never runs the script and never reads a JS global;
    - *(Amendment, slice C2: the argument is JSON except that 1688 writes some object keys as bare decimal integers, such as `skuWeight: {5919566953309: 0.23}`. A policy may declare `bare_integer_keys`. The extension then quotes each such key, a run of digits right after `{` or `,` and before `:` outside any string literal, in one pass, and `JSON.parse` decides everything else. Nothing else is accepted.)*
  - **projects** the parsed document onto the allowlist the policy declares: exactly the paths of §4, as listed there. Nothing outside the allowlist leaves the browser.
    - The member's receive address (`freightInfo.recieveAddress`, `receiveAddressId`, the area codes) is never on the allowlist.
    - No other account or member payload is on it either (ADR-0019 AC-07).
  - sends only the projection, as the `embedded_data` member of the capture. It never sends the script's text.
- **Any mismatch** sends no member, fail-closed, so the fields it would have supplied are `REVIEW_REQUIRED`. A mismatch is:
  - the prefix, the marker or a trailing byte;
  - invalid JSON;
  - an over-bound size or depth;
  - an allowlisted path whose value has the wrong shape.

**What the server does** (it never trusts the browser's cut alone, ADR-0019 §6):
- It refuses a member that holds any key or path outside the allowlist, or any value of the wrong type. The server's allowlist is the same reviewed list, digested into the policy.
- The security gate (ADR-0019 §6.1) runs over every string in the member as it does over DOM text. A secret or the member's own identity refuses the capture.
- The extractor reads the member's allowlisted paths (§4). The member is dropped once the facts are written. Only canonical facts and their evidence are stored (§6.1).
- **Evidence.** A fact from the member carries evidence kind `EMBEDDED_DATA` with its JSON path as the locator. This is one new `EvidenceKind`, authorized here.
  - *(Amendment, slice C2: the evidence kind is the existing `EMBEDDED_JSON` (ADR-0010 §8: data embedded in the page, kept as a parsed tree and never as script text), with the locator `embedded:<JSON path>`. That kind already means exactly this. The evidence table constrains its kinds in the database, so a new kind would need a migration and would gain nothing. No `EvidenceKind` is added.)*

### 3.1 Detail images: the public description document (amendment, slice C2)

1688 does not put the product description in the product page. The page names a public description document, `description.fields.detailUrl` (for example `https://itemcdn.tmall.com/1688offer/icoss…`). It answers `text/plain` with exactly one statement: `var offer_details=` followed by a JSON object whose `content` is the description's HTML.

- **The projection allowlists `description.fields.detailUrl`.** The browser sends only that URL.
- **The server reads the document itself** (ADR-0019 §7, the same policed transport as for images):
  - only from the profile's declared description hosts (`itemcdn.tmall.com`), over https on port 443, with no query and no fragment;
  - with no cookie and no session, never following a redirect, under a byte bound;
  - in its own egress grant for the description hosts alone, reserved in the run's budget (one read per run).
- **Parsing is strict.** The body, trimmed, is `var offer_details=`, one JSON object and `;`. It is parsed with a JSON parser and never run. The object's `content` string is read as HTML for its `<img>` references, in document order.
- **Detail images.** A reference on a declared image host is a `DETAIL`-role candidate (ADR-0019 §7, AC-14). The server fetches it under the role's cap, like any other image. A reference on any other host is not a candidate.
- **Any mismatch gives no detail images.** A mismatch is a URL off the declared hosts, a refused or failed read, a status other than 200, a body that is not exactly that statement, invalid JSON, or no `content` string. The reason is recorded as `REVIEW_REQUIRED` evidence on `detail_description` (`description:<reason>`). Nothing is guessed, and the document is never stored.
- **OC-01 still holds.** The description document is not a product page of either site, and the server never reads `detail.1688.com`.

## 4. 1688 fields

The allowlisted paths are under `result.data`.

| field | read from | rule |
| --- | --- | --- |
| `original_name` | the page title in the DOM | as stated (Chinese) |
| `prices` | `mainPrice.fields.priceModel`, `skuInfoMap.*.price` | CNY. `skuPrice`: the lowest SKU original price is the product's labelled `PURCHASE` price, and each SKU's own price is on its option. `rangePrice`: the first tier (`beginAmount` 1) of `originalPrices`, or of `currentPrices` when no original is stated, is the purchase price |
| `options` | `Root.fields.dataJson.skuModel.skuProps`, `skuInfoMap` | axes and values as stated. Each SKU's original `price` is its purchase price; a `discountPrice` is kept as a fact (ADR-0038 §9.3 as amended) |
| `stock` | `skuInfoMap.*.canBookCount` | per SKU, as stated |
| `quantity_tiers` | `priceModel.currentPrices` when `rangePrice` | each tier as stated |
| `shipping` | `shippingServices.fields.freightInfo.totalCost`, `location` | CNY freight from the stated ship-from location to the user's receive address (the 배대지). A `FIXED` fee as stated, without the address |
| `brand`, `origin` | the product attribute table (品牌, 产地) | as stated |
| `images` | the DOM main gallery; `DETAIL` images from the description document (§3.1) | server-fetched (§1) |
| `minimum_sale_price`, `sales_channels` | not stated on 1688 | `ABSENT` |

- **The original (line) price is the purchase price** (ADR-0038 §9.3 as amended, Issue #219 `6164685916`). A promotion or member price is kept beside it as a fact.
- **Units.** The unit (套, 件, 个) and the minimum order (`beginAmount`) are kept as stated. A minimum order above one is `REVIEW_REQUIRED`: 구매대행 sells one unit.

## 5. Currency

- Both suppliers' revisions state one currency: CNY or JPY (ADR-0038 §1).
- This needs ADR-0038 slice **O1**, currency on facts and money values. The collectors come after O1.
- Until slice O2, a non-KRW Item is not priced: pricing returns `REVIEW_REQUIRED`, never a guessed KRW amount.

## 6. Acceptance

- Each supplier starts `RECON` (ADR-0030 §7).
- **The acceptance:** the user captures five product pages per supplier, chosen with the owner as for U-PICK and 건강산. A fresh run over the same captures yields zero diffs.
  - 1688: both price modes, at least one product with more than ten SKUs, and a promotion price.
  - Super Delivery: one undisclosed price, one set option, and one company with terms text.
- The supplier becomes `ACTIVE` when its acceptance document says PASS.

## Invariants

- **OC-01** Neither supplier is read except by a page the user captured. There is no direct read, list queue or crawl.
- **OC-02** ICBM holds no credential for either site.
- **OC-03** No capture carries script text. 1688's one declared embedded-data member is a browser-side projection, onto the reviewed allowlist, of one script's JSON argument. That argument is parsed as data and never executed.
- **OC-04** A mismatch in the embedded data refuses that member fail-closed. Its fields are `REVIEW_REQUIRED`, never inferred from the DOM's selected option.
- **OC-05** The member's receive address and identity never leave the browser. The allowlist excludes them, and the server refuses a member that holds any path outside it.
- **OC-06** Only allowlisted paths are read, and only canonical facts and evidence are stored.
- **OC-07** A price the page does not disclose is `REVIEW_REQUIRED`. The purchase price is what one unit costs at the regular price, in the source currency. Super Delivery adds the consumption tax its page's mark decides, and no discount sets the cost.
- **OC-08** A company's terms are kept as its words. They are not a gate.

## Slices

| # | slice |
| --- | --- |
| C0 | this ADR |
| C1 | `superdelivery` collector (DOM), after ADR-0038 O1 |
| C2 | `ali1688` collector, with the embedded-data member: extension, policy, gate and extractor |
| C3 | the two acceptances, then `ACTIVE` |

Migrations are numbered 0063 or later (0062 is Track A's).

## Amendments

- **2026-10-14, the owner's price basis (Issue #219 `6164685916`).** 1688's purchase price is the SKU's **original** price: the non-discounted `price`, the line price. A promotion or member `discountPrice` is recorded only and never sets the cost. When no original is shown, the shown price is used. A `rangePrice` page uses its first tier. §4 and OC-07 read so, and so does ADR-0038 §9.3.
- **2026-10-14, slice C2 (implementation choices, Claude Code Track B).**
  - §3 accepts bare integer object keys when the policy declares them. It records the member's facts under the existing `EMBEDDED_JSON` evidence kind: no new kind and no migration.
  - §3.1 adds the public description document as the source of 1688's detail images, read by the server under its own egress grant. This extends §1's reviewed hosts with the description host, which serves no image.
