# Order Read

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
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

## Coupang

- **Locators:** https://developers.coupang.com/en/api/shipments ; orders by minute https://developers.coupang.com/en/api/shipments/po-list-query-by-minute — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the Shipments & Orders family includes daily and per-minute order lists and a single-order lookup; `GET .../vendors/{vendorId}/ordersheets` is indexed (path prefix elided in the capture).
- **Missing:** full paths, query parameters, response fields, paging, errors, rate limit.
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
