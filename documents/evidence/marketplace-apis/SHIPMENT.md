# Shipment / Dispatch

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** dispatch https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-dispatch-product-orders-pay-order-seller ; family https://apicenter.commerce.naver.com/docs/commerce-api/current/발주-발송-처리 — `OFFICIAL_API_DOC`, observed 2026-09-28 (research packet 1 and PR #141 pass).
- **Captured:** the official 발주/발송 family supports dispatch processing. No method/path or field was captured.
- **Group:** order-seller APIs require API group `주문 판매자` (`NAVER-P1-ORDERSELLER-GROUP-1093`, `OFFICIAL_SUPPORT`); the group of this specific endpoint is not separately captured.
- **Missing:** method/path, request keys (courier/invoice fields), limits, success/response, errors, idempotency, timeout semantics.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.
- **Amendment (M6.5-C, 2026-10-08):** the dispatch is captured at `2.90.1` (`SOURCES.md` §5.8, `NAVER-P0-DISPATCH-DELIVERY-2901`): `POST /v1/pay-order/seller/product-orders/dispatch`, the body, the 30-order limit, the per-order success and fail lists, the error codes and the carrier and method enumerations. It is `ADOPTED` as `SMARTSTORE_ORDER_DISPATCH` (`ENDPOINT_MATRIX.md` §4.1.5; ADR-0025 §5), one order per call and only through the DISPATCH stage. Still not stated: the group on the page, the dispatchable statuses, the `dispatchDate` range, re-dispatch behaviour and idempotency. Runtime stays `UNVERIFIED` until a real dispatch.

## Coupang

- **Locators:** https://developers.coupang.com/en/api/shipments ; https://developers.coupang.com/en/api/shipments/uploading-waybills — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `POST /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/orders/invoices` uploads waybills.
- **Missing:** request/response fields, batch limits, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578935244559- (배송상품 발송처리) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `POST /v1/shopping/orders/deliveries/invoices`; at most 100 waybills per request; processing is asynchronous and the docs advise a delay before read-back; the request accepts courier/invoice data.
- **Missing:** request keys, response/result retrieval, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (https://etapi.gmarket.com/pages/API-가이드, 2026-09-28):** a delivery-management family exists; no shipment endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** shipping APIs exist; no seller shipment-write endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the shipping workflow is exposed under `/api/pd/{version}`; shipping-instructed orders are documented ([ORDER_READ](ORDER_READ.md#ssgcom)). No shipment-write endpoint captured.
- **Missing:** every shipment-write fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
