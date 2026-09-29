# Authentication

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `IMPLEMENTATION_EVIDENCE_COMPLETE` | `ADOPTED` (token issuance, seller-account identity read) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Official docs (`OFFICIAL_API_DOC`):** `NAVER-P0-AUTH` https://apicenter.commerce.naver.com/docs/auth ; `NAVER-P0-TOKEN` https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth ; `NAVER-P0-SELLER-ACCOUNT` https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers ; `NAVER-P0-BASIC-INTEGRATION` (solution account-mapping guide). NAVER Commerce API 2.88.0 (2026-09-07), retrieved 2026-09-14; the auth page was re-read for 2.89.0 (2026-09-15) in `NAVER-P0-PACKET-289` (Issue #89 comment 5746489554).
- **Official support (`OFFICIAL_SUPPORT`):** `NAVER-P1-SELF-ACCOUNT-3339`, `NAVER-P1-SELF-BODY-3751`, `NAVER-P1-TIMESTAMP-357`, `NAVER-P1-APP-REAUTH-3557` (`P1_ONLY`), `NAVER-P1-SECRET-REISSUE-1564` (`P1_ONLY`), `NAVER-P1-OWN-STORE-780` (`P1_ONLY` for the 1:1 claim), `NAVER-P1-ACCOUNT-UID-2425`, `NAVER-P1-SELLERINFO-GROUP-1895` — `SOURCES.md` §6.
- **Standard:** `OAUTH-S0-RFC6749` for the OAuth semantics NAVER adopts.

### Token issuance — provider contract

| Field | Provider fact | Source |
| --- | --- | --- |
| Protocol | OAuth 2.0 Client Credentials; the Commerce API exposes **no OAuth scopes** (`Scopes: N/A`) | `NAVER-P0-AUTH` |
| Method / URL | `POST https://api.commerce.naver.com/external/v1/oauth2/token` (path `/v1/oauth2/token` under the base URL) | `NAVER-P0-TOKEN` |
| Content-Type | `application/x-www-form-urlencoded`; the values belong in the body, not the query string | `NAVER-P0-TOKEN`; #3751 |
| Body | `client_id`; `timestamp`; `client_secret_sign`; `grant_type=client_credentials`; `type=SELF` for an own-store application; **`account_id` must not be sent for `SELF`** | `NAVER-P0-TOKEN`; #3751, #3339 |
| Signature | concatenate `client_id` + `_` + `timestamp`; bcrypt it using `client_secret` as the salt; Base64-encode the bcrypt result; send it as `client_secret_sign`. The same `timestamp` goes in the body | `NAVER-P0-AUTH` |
| Timestamp | millisecond Unix time; not in the future relative to provider time; rejected outside the provider's documented validity window; clock synchronization recommended | #357 |
| Success | HTTP `200`; body `access_token`, `expires_in`, `token_type` (Bearer). The token response carries no seller identity | `NAVER-P0-TOKEN`; #3339 |
| Use | protected calls send `Authorization: Bearer {access_token}` | `NAVER-P0-AUTH` |
| Lifetime | 10,800 s (180 min); while the existing token has 30 min or more left, a token request returns that existing token; below 30 min a new token may be issued; a previous token stays valid until its own expiry; an expired token cannot be used | `NAVER-P0-AUTH`, `NAVER-P0-TOKEN` |
| Refresh | no refresh token and no refresh grant; on expiry/invalidity call the token API again | `NAVER-P0-AUTH` |
| Documented statuses | `200` issued/reissued; `400` validation error; `403` access/authorization error; `500` temporary internal error | `NAVER-P0-TOKEN` |
| Application lifecycle | own-store application authentication is valid 180 days from authentication; re-authentication is a manual web action by the store's integrated manager (`P1_ONLY`) | #3557 |
| Secret reissue | reissuing the application secret makes the previous secret unusable immediately (`P1_ONLY`) | #1564 |
| Application ↔ store | an own-store application (`내스토어 애플리케이션`) uses `type=SELF` and is connected to one SmartStore account (`P1_ONLY` for the 1:1 claim) | #780 |
| `SELLER` tokens | Commerce Solution guidance describes `SELLER` tokens for subscriber-store operations; that is solution-specific and not applicable evidence for an own-store application | 5746489554 |

### Seller-account identity read — provider contract

| Field | Provider fact | Source |
| --- | --- | --- |
| Method / URL | `GET https://api.commerce.naver.com/external/v1/seller/account` | `NAVER-P0-SELLER-ACCOUNT` |
| Auth / group | `Authorization: Bearer {access_token}`; API group `판매자정보` | `NAVER-P0-SELLER-ACCOUNT`; #1895 |
| Response identity | `accountUid` (provided for Commerce API integration and seller-account mapping) and `accountId`; both identify a SmartStore uniquely under current guidance; no immutability guarantee is documented | `NAVER-P0-SELLER-ACCOUNT`; #2425 |
| Documented errors | `400 GENERAL_ERROR`; `401 UNAUTHORIZED`; `403 ROLE_NOT_FOUND`, `PROVISION_NOT_FOUND`, `INVALID_CHANNEL_STATUS`, `INVALID_STORE_STATUS`, `INVALID_REPRESENT_STATUS`, `INVALID_MEMBER_STATUS`, `INVALID_INTERLOCK_STATUS`, `RESOURCE_NOT_AVAILABLE`; `404 CHANNEL_NOT_FOUND`, `STORE_NOT_FOUND`, `REPRESENT_NOT_FOUND`, `MEMBER_NOT_FOUND`, `INTERLOCK_NOT_FOUND`; `500 PARSING_FAIL`, `SERDES_FAIL`, `ENCDEC_FAIL`, `GENERAL_ERROR` | `NAVER-P0-SELLER-ACCOUNT` (`ENDPOINT_MATRIX.md` §7) |

### Open runtime questions (not documentation gaps)

- Immediate reissue behaviour after a lost first issuance response (`AUTH.md` Q1).
- The exact provider-visible error when the 180-day application re-authentication becomes mandatory (`AUTH.md` Q2; `ERRORS.md` Q1).
- Concurrent token issuance behaviour (`AUTH.md` Q3).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- `SMARTSTORE_AUTH_TOKEN` and `SMARTSTORE_SELLER_ACCOUNT` are `ADOPTED` (M2; `ENDPOINT_MATRIX.md` §4, §6, §7). ICBM mode `AUTH_MODE=SELF` (`AUTH.md` §2); a switch to `SELLER` needs its own ADR. ICBM policy: token connect `5s` / read `30s`; seller-account connect `5s` / read `10s`; redirect `NO_FOLLOW`; success predicates in `ENDPOINT_MATRIX.md` §6–§7. Lifecycle, persistence and crash rules: `documents/contracts/platforms/smartstore/AUTH.md`; identity rules: `ACCOUNT_IDENTITY.md` (`accountUid` primary, `accountId` corroborating).
- **Runtime: `UNVERIFIED` at contract level** — `AUTH.md`, `ACCOUNT_IDENTITY.md` and `ENDPOINT_MATRIX.md` keep `verified_at = null`. The measured slots `SMARTSTORE-R0-TOKEN`, `SMARTSTORE-R0-SELLER-ACCOUNT` and `SMARTSTORE-R0-FIRST-TOKEN-CRASH` are `PASS` (M2 closeout `m2-campaign-02`); `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW` is `DEFERRED_LONG_HORIZON` and `SMARTSTORE-R0-APP-REAUTH` is `BLOCKED_BY_TIME` (`SOURCES.md` §10.1).

## Coupang

- **Locator:** https://developers.coupang.com/en/getting-started/open-api-test-guide ; API root https://developers.coupang.com/en/api — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** Wing issues an Access Key / Secret Key; requests are HMAC-SHA256 signed; IP allowlisting is part of setup; RESTful JSON over HTTPS.
- **Missing:** the signed-string composition, header names/format, timestamp rules, auth error responses.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Locator:** https://openapi.11st.co.kr/ — official Open API portal, observed 2026-09-28. A portal/key-issuance landing page is a page locator, not an endpoint-level auth source, and the public fetch returned no endpoint reference content; source authority therefore stays `UNAVAILABLE` and coverage `NOT_CAPTURED` (README status model).
- **Missing:** every endpoint-level auth fact — header name/format, key or signature composition, timestamp rules, auth error responses.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578909656975- (API 공통 인증 헤더 구성) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured headers:** `Authorization: KakaoAK {admin_app_key}`; `Target-Authorization: KakaoAK {seller_app_key}`; `channel-ids`.
- **Missing:** `channel-ids` value format; auth error responses.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/pages/API-가이드 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** JWT HS256 signed with the issued secret; `Authorization: Bearer {JWT}`; master/site seller IDs participate in the claims; authorization failures may be blocked before the target API (HTTP `401` payload example, [ERRORS](ERRORS.md#gmarket--auction)).
- **Missing:** exact claim names/values and token lifetime.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNm=GetStarted — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the seller center issues the OpenAPI key; base `https://openapi.lotteon.com`; RESTful GET/POST over HTTPS; JSON/XML.
- **Missing:** how the key is sent (header name/format); auth errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listShppDirection.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** captured EAPI pages require an `Authorization` header carrying the vendor API authentication key, and an `Accept` of JSON or XML.
- **Missing:** key issuance/rotation; auth errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
