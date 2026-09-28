# Rate Limit / Volume Limit

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. Only captured facts are recorded; an endpoint-specific limit is never generalized.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_APPLICABLE` (owned by `ERRORS.md` §16) | `UNVERIFIED` |
| Coupang | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** `NAVER-P0-RESTRICTION` https://apicenter.commerce.naver.com/docs/restriction ; `NAVER-P0-TROUBLESHOOTING` https://apicenter.commerce.naver.com/docs/trouble-shooting — `OFFICIAL_API_DOC`, 2.88.0, retrieved 2026-09-14 (`docs/platforms/smartstore/ERRORS.md` §16).
- **Captured:** request-rate limits return `429` with `GW.RATE_LIMIT`; limits are token-bucket style per API and application; response headers describe the replenish rate, burst capacity and remaining requests; applicable longer-period quotas return `429` with `GW.QUOTA_LIMIT`.
- **Endpoint-specific:** product search — more than 100,000 matching products may return an error ([PRODUCT_SEARCH](PRODUCT_SEARCH.md#smartstore)); order confirm — at most 30 product orders per request ([ORDER_CONFIRM](ORDER_CONFIRM.md#smartstore)); image upload — at most 10 images per request ([IMAGE_UPLOAD](IMAGE_UPLOAD.md#smartstore)).
- **Missing:** the exact rate-limit header names; the numeric bucket size/replenish rate per API; quota periods and values.
- **ICBM:** `NOT_APPLICABLE` (no endpoint). `ERRORS.md` §9.4–§9.5, §16 own the handling: `RATE_LIMITED`, scheduled back-off, no hot loop, no token refresh because of a `429`, and a rate-limited write still keeps its remote-outcome/replay rule. Runtime `UNVERIFIED`.

## Coupang

- **Missing:** no global requests-per-second/day limit captured from the official index (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578935244559- (배송상품 발송처리) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** shipment invoice registration accepts at most 100 waybills per request and is processed asynchronously ([SHIPMENT](SHIPMENT.md#kakao-shopping)).
- **Missing:** any request-rate limit.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/160 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** product list search allows at most 20 calls per minute ([PRODUCT_SEARCH](PRODUCT_SEARCH.md#gmarket--auction)). Not generalized to other endpoints.
- **Missing:** limits of every other endpoint and the throttling response.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Missing:** no global rate-limit contract captured (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Missing:** no global rate-limit contract captured (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
