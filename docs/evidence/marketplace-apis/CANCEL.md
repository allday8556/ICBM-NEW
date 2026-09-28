# Cancel

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locator:** https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-request-cancel-pay-order-seller — `OFFICIAL_API_DOC`, current reference observed 2026-09-28 (PR #141 pass; version not recorded by that capture).
- **Method / path:** `POST /v1/pay-order/seller/product-orders/{productOrderId}/claim/cancel/request`.
- **Missing:** API group of this endpoint, request keys (reason codes), success/response, errors, idempotency/replay, timeout semantics, rate limit.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Captured (https://developers.coupang.com/en/api, 2026-09-28):** the official order and return families expose order cancellation and return/cancel request lists.
- **Missing:** method/path and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** Claim API https://shopping-developers.kakao.com/hc/ko/articles/4578928106639- (클레임 API 명세) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the Claim API documents seller/buyer cancellation flows; some successful actions return a status-only response; claims use `claimId` and `orderIds` ([CLAIM](CLAIM.md#kakao-shopping)).
- **Missing:** cancel method/paths and request keys.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (https://etapi.gmarket.com/pages/API-가이드, 2026-09-28):** a claim-management family exists; no cancel endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** order/claim APIs exist; no cancellation endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Captured (2026-09-28):** return/exchange shipping APIs are captured ([RETURN](RETURN.md#ssgcom)); no distinct cancellation endpoint captured.
- **Missing:** every cancellation fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
