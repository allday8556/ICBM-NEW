# Product Delete

> Evidence catalog; status model and provenance conventions in [README](README.md). A documented delete endpoint is not adopted by ICBM; any destructive marketplace action needs user approval (CLAUDE.md §7.2).

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (NAVER Commerce API 2.89.0, `NAVER-P0-PACKET-289` / Issue #89 comment 5746489554):** the AI-use guide places product deletion in API group `상품`.
- **Missing:** the delete method/path, request, success/response, errors, idempotency/replay and timeout semantics.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Locator:** https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `DELETE /v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}`.
- **Missing:** preconditions, success/response, errors, idempotency.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Missing:** no delete endpoint captured in the 2026-09-28 pass.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Missing:** no whole-product delete endpoint captured in the 2026-09-28 pass.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Missing:** no product-delete endpoint captured (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Missing:** no product-delete endpoint captured (2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
