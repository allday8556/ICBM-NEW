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

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** item price update `PUT .../marketplace/vendor-items/{vendorItemId}/prices/{price}` (path prefix elided in the capture); an original-price endpoint also exists.
- **Missing:** full path, request/response, errors, idempotency; the original-price endpoint's path.
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
