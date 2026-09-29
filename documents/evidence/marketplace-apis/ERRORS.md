# Error Semantics

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. Provider error facts only; ICBM classification is owned by `documents/contracts/platforms/smartstore/ERRORS.md`.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_APPLICABLE` (owned by `ERRORS.md`) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Official docs:** `NAVER-P0-TROUBLESHOOTING` https://apicenter.commerce.naver.com/docs/trouble-shooting ; `NAVER-P0-REST` https://apicenter.commerce.naver.com/docs/restful-api ; `NAVER-P0-RESTRICTION` ; `NAVER-P0-PRODUCT-CREATE` ; `NAVER-P0-PRODUCT-READ` ; `NAVER-P0-SELLER-ACCOUNT` ; upload-product https://apicenter.commerce.naver.com/docs/commerce-api/current/upload-product — 2.88.0 retrieved 2026-09-14; CREATE statuses re-read at 2.89.0 (2026-09-15, `NAVER-P0-FIELDS-CREATE-289`).
- **Official support (`OFFICIAL_SUPPORT`):** `NAVER-P1-GW-AUTHN-GROUP-1013`, `-PRODUCT-GROUP-1835`, `-GW-AUTHN-HEADER-3676`, `-GW-AUTHN-EXPIRED-3762`, `-IP-TEMPORARY-3428`, `-BADREQ-NOTICE-1649`, `-BADREQ-POLICY-3529` (`SOURCES.md` §6).

### Gateway layer (`GW.*`)

| Code | Captured fact | Source |
| --- | --- | --- |
| `GW.AUTHN` | authentication failure at the gateway; observed causes: expired token (`401`), missing API-group permission (`401` for a product API without `상품`), malformed `Authorization` header | `NAVER-P0-TROUBLESHOOTING`; #3762, #1013, #1835, #3676 |
| `GW.IP_NOT_ALLOWED` | caller IP not allowed; one observed case alternated with success and NAVER later identified a temporary provider-side condition | `NAVER-P0-TROUBLESHOOTING`; #3428 |
| `GW.NOT_FOUND` | gateway route not found (distinct from an API-server `NOT_FOUND`) | `NAVER-P0-TROUBLESHOOTING` |
| `GW.RATE_LIMIT` | HTTP `429`, request-rate limit ([RATE_LIMIT](RATE_LIMIT.md#smartstore)) | `NAVER-P0-RESTRICTION` |
| `GW.QUOTA_LIMIT` | HTTP `429`, longer-period quota | `NAVER-P0-RESTRICTION` |
| `GW.PROXY.01`–`05`, `GW.INTERNAL_SERVER_ERROR`, `GW.BLOCK.01`–`02`, `GW.TIMEOUT.01`–`02` | documented causes include gateway/service/network failure, circuit open, maintenance and timeout | `NAVER-P0-TROUBLESHOOTING` |

Gateway responses usually carry a trace ID (`traceId` / `GNCP-GW-Trace-ID`).

### API-server layer

- Standard codes: `BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `INTERNAL_SERVER_ERROR`, `PERMANENT_REDIRECT` (`308`, present in product API response sets) (`NAVER-P0-REST`, `NAVER-P0-PRODUCT-CREATE`).
- Product and image APIs document `308`, `400`, `401`, `403`, `404`, `500` with the JSON error body `code` / `message` / `invalidInputs` / `timestamp` (upload-product page; the CREATE status set is in [PRODUCT_CREATE](PRODUCT_CREATE.md#errors)).
- `BAD_REQUEST`: `invalidInputs` can be absent or insufficient; the reference says to use `message` (`NAVER-P0-PRODUCT-CREATE`). Structured entries carry name/type/message with types such as `NotEmpty`, `NotValidEnum`, `NumberMax`; a missing required notice field and a restricted seller tag (policy) have both surfaced as `BAD_REQUEST` (#1649, #3529).
- Seller-account domain codes: [AUTH](AUTH.md#seller-account-identity-read--provider-contract). Token endpoint statuses: [AUTH](AUTH.md#token-issuance--provider-contract).

### Unresolved (exact)

- The HTTP status of each gateway code other than `GW.AUTHN` (`401`, observed) and `GW.RATE_LIMIT` / `GW.QUOTA_LIMIT` (`429`).
- The endpoint-specific domain error catalog of the product endpoints (`ERRORS.md` Q4).
- Whether every pre-service gateway rejection guarantees that no mutation reached the target service (`ERRORS.md` Q2).
- The redirect target/behaviour behind a documented `308` (`ERRORS.md` Q3).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `NOT_APPLICABLE` (no endpoint). `documents/contracts/platforms/smartstore/ERRORS.md` is the canonical classification contract: cause class, `remote_outcome` and workflow state are separate axes; no code-only mapping; `NOT_APPLIED_PROVEN` is whitelist-only (§15); a `308` is never followed for a mutation (§10.6, §17); unknown codes default conservatively (§18).
- Runtime `UNVERIFIED`: `ERRORS.md verified_at = null`.

## Coupang

- **Captured (https://developers.coupang.com/en/api, 2026-09-28):** APIs use HTTP/JSON with HMAC; endpoint-specific errors exist; no cross-endpoint taxonomy captured.
- **Missing:** error body format and statuses.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** common guide https://shopping-developers.kakao.com/hc/ko/sections/6592286270223- (API 공통 가이드) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** success is HTTP `200`; failures use a non-200 status with the common `ErrorMessage` JSON.
- **Missing:** `ErrorMessage` field names and the code catalogue.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/pages/API-가이드 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** authorization failures may be blocked before the target API; the guide gives an HTTP `401` payload example.
- **Missing:** the general error body and status catalogue.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** captured APIs return result code/message fields inside endpoint-specific response envelopes.
- **Missing:** exact field names and the code catalogue.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** captured EAPI responses use `resultCode`, `resultMessage`, `resultDesc`; required-field failures are endpoint-specific.
- **Missing:** the code catalogue and HTTP-status usage.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
