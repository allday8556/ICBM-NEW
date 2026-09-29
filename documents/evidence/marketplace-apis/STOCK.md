# Stock

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. ICBM stock-state rules (CLAUDE.md §6.3) are separate from provider fields.

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

- **Captured (NAVER Commerce API 2.89.0; `NAVER-P0-PACKET-289` 5746489554, `NAVER-P0-FIELDS-CREATE-289` 5861477977, `NAVER-P0-REQUIRED-CREATE-289` 5861933729, `NAVER-P0-REGISTRATION-CREATE-289` 5862400626):** in the product body, `originProduct.stockQuantity` is **required on product registration**, at least 1 and at most 99,999,999 (5862400626); `optionInfo.optionCombinations[].stockQuantity` is not required, defaults to 0, at most 99,999,999 ([PRODUCT_CREATE § Request structure](PRODUCT_CREATE.md#request-structure)). The adopted read-backs retain `stockQuantity` ([PRODUCT_READ](PRODUCT_READ.md#smartstore)).
- **Missing:** the relation between product-level and combination-level stock; a standalone stock-change endpoint (method/path/request/response); errors and idempotency.
- **ICBM:** stock is written only through the `NOT_ADOPTED` CREATE; no stock-change endpoint is registered (`ENDPOINT_MATRIX.md` §4). Runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** item quantity update `PUT .../marketplace/vendor-items/{vendorItemId}/quantities/{quantity}` (path prefix elided in the capture).
- **Missing:** full path, request/response, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (API docs, 2026-09-28):** product structures expose stock quantity; no standalone stock endpoint captured.
- **Missing:** the stock field key/rules and any stock endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** ESM notice https://etapi.gmarket.com/182 — official ESM notice, observed 2026-09-28.
- **Captured:** price/stock update surface `PUT https://sa2.esmplus.com/item/v1/goods/{goodsNo}/sell-status` ([PRICE](PRICE.md#gmarket--auction)).
- **Missing:** request/response fields, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** stock APIs exist in the API center, but the captured page was an affiliate additional-product stock API, not a general-seller contract.
- **Missing:** a general-seller stock contract.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/notice/noticeInfo.ssg?postngId=1247484055 — `OFFICIAL_RELEASE_NOTE`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the New product API includes sale-state and option/price areas; no standalone stock endpoint captured.
- **Missing:** every stock-endpoint fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
