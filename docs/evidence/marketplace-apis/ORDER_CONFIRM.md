# Order Confirm / Acknowledge

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locator:** https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-confirm-placed-product-orders-pay-order-seller — `OFFICIAL_API_DOC`, current reference observed 2026-09-28 (research packet 1 and PR #141 pass; version not recorded by that capture).
- **Method / path:** `POST /v1/pay-order/seller/product-orders/confirm`.
- **Limit:** at most 30 product orders per request.
- **Group:** order-seller APIs require API group `주문 판매자` (`NAVER-P1-ORDERSELLER-GROUP-1093`, `OFFICIAL_SUPPORT`); the group of this specific endpoint is not separately captured.
- **Missing:** request keys, success status/response (including per-item results), errors, idempotency/replay, timeout semantics, rate limit.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `PATCH /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets/acknowledgement` changes status to Product in Preparation.
- **Missing:** request/response fields, batch limits, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (API docs, 2026-09-28):** an order-processing family exists; no seller acknowledgement endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (https://etapi.gmarket.com/pages/API-가이드, 2026-09-28):** an order-management family exists; no acknowledgement endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6, 2026-09-28):** order integration status fields are documented; no acknowledgement endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Captured (https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg, 2026-09-28):** the shipping-direction workflow is captured ([ORDER_READ](ORDER_READ.md#ssgcom)); no separate acknowledgement endpoint captured.
- **Missing:** every acknowledgement fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
