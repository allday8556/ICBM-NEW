# Price

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. ICBM pricing rules (CLAUDE.md §6.1) are separate from provider fields.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_RELEASE_NOTE` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_RELEASE_NOTE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (NAVER Commerce API 2.89.0; `NAVER-P0-PACKET-289` 5746489554, `NAVER-P0-FIELDS-CREATE-289` 5861477977):** in the product body, `originProduct.salePrice` is required, at most 999,999,990; `optionInfo.optionCombinations[].price` defaults to 0, at most 999,999,990 ([PRODUCT_CREATE § Request structure](PRODUCT_CREATE.md#request-structure)). The adopted read-backs retain `salePrice` ([PRODUCT_READ](PRODUCT_READ.md#smartstore)).
- **Missing:** a standalone price-change endpoint (method/path/request/response); the meaning of the option `price` relative to `salePrice`; price-change errors and idempotency.
- **ICBM:** price is written only through CREATE, which is adopted as a contract only and never called (`ENDPOINT_MATRIX.md` §4.1.1); no price-change endpoint is registered (`ENDPOINT_MATRIX.md` §4). Runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api/products/changing-price-of-each-item-of-a-product — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Method / path:** `PUT /v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/prices/{price}`. It is usable after approval has issued the item ID.
- **Input:** `vendorItemId` is the unique option ID. Price is entered in 10-won units. Query parameters include `forceSalePriceUpdate`; auto-pricing parameters `apMinSalePrice` and `apActive` must be supplied together when used.
- **Response:** `code`, `message`, `data`; the success example has `code=SUCCESS`. Captured `400` cases include ordinary price-change bounds, auto-generated option restrictions, deleted/invalid ID, non-10-won price, and invalid auto-pricing parameters.
- **CREATE/read:** `items[].salePrice` is item-level; `GET .../vendor-items/{vendorItemId}/inventories` returns the item sale price ([PRODUCT_READ](PRODUCT_READ.md#coupang)).
- **Missing:** idempotency/replay and ambiguous-outcome semantics, rate limit, complete error classification, and the original-price endpoint contract.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (API docs, 2026-09-28):** product models expose a sale price; no standalone price endpoint captured.
- **Missing:** the price field key/rules and any price endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** ESM notice https://etapi.gmarket.com/182 — official ESM notice, observed 2026-09-28.
- **Captured:** `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}/sell-status` is documented for price/stock/sale-period changes.
- **Missing:** request/response fields, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** price fields are present in product/order schemas; no standalone price endpoint captured.
- **Missing:** every price-endpoint fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API includes a dedicated price area.
- **Missing:** method/path and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
