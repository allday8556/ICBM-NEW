# Tracking / Waybill

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. Waybill upload itself is in [SHIPMENT](SHIPMENT.md).

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (2026-09-28):** tracking/dispatch information belongs to the order-shipment family ([SHIPMENT](SHIPMENT.md#smartstore)); no tracking read contract captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** `NOT_ADOPTED` (not in `ENDPOINT_MATRIX.md` §4); runtime `UNVERIFIED`.
- **Amendment (M6.5, 2026-10-08):** the dispatch `POST /v1/pay-order/seller/product-orders/dispatch` and the detail read's `delivery` object are captured at `2.90.1` (`SOURCES.md` §5.8, `NAVER-P0-DISPATCH-DELIVERY-2901`). The delivery members (carrier, tracking number, state, timestamps, wrong-tracking flag) are read back through the adopted detail read (`ENDPOINT_MATRIX.md` §4.1.4, ADR-0025 §6). The dispatch itself is adopted by M6.5-C (`ENDPOINT_MATRIX.md` §4.1.5). No tracking-change endpoint is stated.

## Coupang

- **Captured (https://developers.coupang.com/en/api/shipments, 2026-09-28):** the Shipments & Orders family includes tracking upload and shipment status.
- **Missing:** method/path and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (https://shopping-developers.kakao.com/hc/ko/articles/4578935244559-, 2026-09-28):** shipment registration accepts courier/invoice data; order reads return delivery data ([ORDER_READ](ORDER_READ.md#kakao-shopping)). No separate tracking endpoint captured.
- **Missing:** delivery-data keys in the order read; any tracking endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (2026-09-28):** a delivery family exists; no tracking endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6, 2026-09-28):** order data includes shipment-related fields; no tracking endpoint captured.
- **Missing:** every tracking-endpoint fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Captured (https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg, 2026-09-28, PR #141 pass):** shipping/return APIs use waybill fields and a shipping/recollection state. No tracking endpoint captured.
- **Missing:** waybill field keys; any tracking endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
