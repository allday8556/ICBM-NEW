# Product Delete

> Evidence catalog; status model and provenance conventions in [README](README.md). Only the SmartStore origin-product delete is adopted (`ENDPOINT_MATRIX.md` §4.1.3, ADR-0018 §3.5); any destructive marketplace action needs user approval (CLAUDE.md §7.2).

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `ADOPTED` (ADR-0018 §3.5; `ENDPOINT_MATRIX.md` §4.1.3) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (NAVER Commerce API 2.89.0, `NAVER-P0-PACKET-289` / Issue #89 comment 5746489554):** the AI-use guide places product deletion in API group `상품`.
- **Official OpenAPI (`OFFICIAL_API_DOC`):** the NAVER-published specification in the official repository `commerce-api-naver/commerce-api`, `docs/2.0.0-RC.js` (blob `f90d1582c41de5fe5000dba909b02c59e210f5de`, last changed 2022-11-21), operation `deleteOriginProduct` "(v2) 원상품 삭제": `DELETE /v2/products/origin-products/{originProductNo}`, path parameter `originProductNo` `integer<int64>` required, group `상품`, rate limit 20 rps per application; `200` 성공 with `CommonResponse` (`code`, `message`, `data`); `400` `BAD_REQUEST`, `401` `UNAUTHORIZED`, `403` `FORBIDDEN`, `404` `NOT_FOUND`, `500` `INTERNAL_SERVER_ERROR`, `308` `PERMANENT_REDIRECT`, each `CommonErrorResponse`. The origin-product `statusType` enumeration of the same specification includes `DELETE`. The current documentation site was not reachable from the capture environment on 2026-10-03, so the release this specification belongs to is older than the 2.89.0 baseline; the path is confirmed current by the 2026 incident notice below.
- **Official support (`OFFICIAL_SUPPORT`, the NAVER `commerce-api-naver` account):**
  - #2439 (2025-04-08) https://github.com/commerce-api-naver/commerce-api/discussions/2439 — a product cannot be deleted, origin or channel alike, while an order (payment pending, in delivery and so on) or a claim (cancel, return, exchange, purchase-confirmation hold) is in progress, or while it is under a sale ban.
  - #3127 (2026-01-02) https://github.com/commerce-api-naver/commerce-api/discussions/3127 — no API deletes several products in one call.
  - #3357 (2026-03-31) https://github.com/commerce-api-naver/commerce-api/discussions/3357 — incident notice naming `DELETE /v2/products/origin-products/:originProductNo`; calls during the incident returned an unintended HTTP 400, and the guidance is to read the product's current data before calling again.
- **Missing:** idempotency or replay semantics (none documented), a provider-side request key, the response of a read-back of a deleted product (a `200` carrying `statusType` `DELETE`, or an error).
- **ICBM:** `ADOPTED` (`ENDPOINT_MATRIX.md` §4.1.3, ADR-0018 §3.5) with mapping revision `m5-delete-r1`: only the documented `200` is applied, every other answer after the handoff is `UNKNOWN` and never resent, and a read-back resolves it. Runtime `UNVERIFIED`.

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
