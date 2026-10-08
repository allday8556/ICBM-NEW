# Order Read

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` + `RUNTIME_EVIDENCE` | `PARTIAL` | `ADOPTED` (M6-C, read-only) | `PARTIAL` (change listing: empty windows and `429`, 2026-10-08) |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locator:** https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-product-orders-with-conditions-pay-order-seller — `OFFICIAL_API_DOC`, current reference observed 2026-09-28 (PR #141 pass; version not recorded by that capture).
- **Method / path:** `GET /v1/pay-order/seller/product-orders` (conditional product-order read); the reference documents a wider order-read family.
- **Group:** order-seller APIs require API group `주문 판매자` (`NAVER-P1-ORDERSELLER-GROUP-1093`, `OFFICIAL_SUPPORT`); the group of this specific endpoint is not separately captured.
- **Missing:** query parameters and their limits, response envelope and order/product-order identifiers, paging, errors, rate limit, freshness semantics.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.
- **Amendment (M6-C, 2026-10-07):** the change listing `GET /v1/pay-order/seller/product-orders/last-changed-statuses` and the product-order query `POST /v1/pay-order/seller/product-orders/query` are captured at `2.90.1` (`SOURCES.md` §5.6, `NAVER-P0-ORDER-READ-2901`): parameters, the 300 limit, the `more` continuation, the change, product-order, shipping-address and delivery members, the enumerations and KST date-times. Both are `ADOPTED` read-only in `ENDPOINT_MATRIX.md` §4.1.4. Still not stated: the group on the page, a maximum window, a fixed call rate. The conditional read above stays `NOT_ADOPTED`; runtime stays `UNVERIFIED` until a real read.
- **Amendment (M6.5-B, 2026-10-08):** the detail read also retains the seven `delivery` members of ADR-0025 §6 (`SOURCES.md` §5.8), under mapping revision `m65-delivery-r1`.

## Coupang

- **Locators:** family index https://developers.coupang.com/en/api/shipments ; minute query https://developers.coupang.com/en/api/shipments/po-list-query-by-minute — `OFFICIAL_API_DOC`, observed 2026-10-03.
- **Method / path:** `GET /v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets` for per-minute and daily/paged queries; the minute query accepts at most a 24-hour time window.
- **Item identity:** every `data[].orderItems[]` includes `vendorItemId`; it also carries `productId`, `vendorItemName`, `shippingCount`, `sellerProductId`, `sellerProductName`, `sellerProductItemName`, and `firstSellerProductItemName`. `externalVendorSkuCode` is present only optionally.
- **Canonical resolution consequence:** resolve the optional seller code exactly when present and cross-check the persisted `vendorItemId`; otherwise resolve by the unique persisted vendor ID. Names and exposure `productId` are not identity. A disagreement or non-unique match is `REVIEW_REQUIRED`.
- **Missing:** a complete query/paging/error contract, order-line read-after-write timing, full retention/sanitization profile and endpoint-specific rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578918827151- (주문정보 조회) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `GET /v1/shopping/order?order_id={order_id}`; a bulk order read also exists.
- **Missing:** bulk-read path, response fields, errors, rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/pages/API-가이드 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the official API guide confirms an order-management family; no order-read endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `POST https://openapi.lotteon.com/v1/openapi/order/v1/getOrderList`; returns order, product and shipping fields, with endpoint-specific result code/message fields ([ERRORS](ERRORS.md#lotteon)); the order schema includes a claim number ([CLAIM](CLAIM.md#lotteon)).
- **Missing:** request keys, exact response keys, paging, rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** `POST /api/pd/{version}/listShppDirection.ssg` retrieves shipping-instructed orders; `Authorization` vendor key, Accept JSON/XML ([AUTH](AUTH.md#ssgcom)); response `resultCode` / `resultMessage` / `resultDesc` ([ERRORS](ERRORS.md#ssgcom)).
- **Missing:** host, request keys, order fields, paging, rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
