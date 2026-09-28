# Permissions / Eligibility

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_APPLICABLE` (owned by `PERMISSIONS_SCOPES.md`) | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

### Provenance

- **Official docs:** `NAVER-P0-AUTH` https://apicenter.commerce.naver.com/docs/auth ; `NAVER-P0-RESTRICTION` https://apicenter.commerce.naver.com/docs/restriction ; `NAVER-P0-CURRENT` ; the AI-use guide read in `NAVER-P0-PACKET-289` (Issue #89 comment 5746489554). 2.88.0 (retrieved 2026-09-14) / 2.89.0 (2026-09-15).
- **Official support:** `NAVER-P1-GW-AUTHN-GROUP-1013`, `NAVER-P1-PRODUCT-GROUP-1835`, `NAVER-P1-SELLERINFO-GROUP-1895`, `NAVER-P1-ORDERSELLER-GROUP-1093` (`SOURCES.md` §6).

### Provider facts

| Fact | Source |
| --- | --- |
| No OAuth scope strings: the auth page states `Scopes: N/A` | `NAVER-P0-AUTH` |
| Permission is per application **API group** (`API 그룹`); an API call needs permission for the group containing that API; groups are obtained when the application is registered or modified in Commerce API Center, where they can be added or removed | `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`; #1835 |
| `판매자정보` — seller-information APIs (e.g. `GET /v1/seller/account`) | #1895 |
| `상품` — product registration, modification, lookup, deletion and category/attribute reads | 5746489554 (AI-use guide); #1835 |
| `주문 판매자` — order-seller APIs | #1093 |
| `문의` — inquiry/Q&A; some inquiry endpoints additionally require `주문 판매자`, so one endpoint may need more than one group | `PERMISSIONS_SCOPES.md` §4.4; #1013 |
| A missing API-group permission can surface as `GW.AUTHN` (observed as `401/GW.AUTHN` for a product API without `상품`); provider guidance points to `내스토어 애플리케이션 → 애플리케이션 상세 → API 그룹` | #1013, #1835 |
| Some APIs are unavailable to certain application types (for example APIs reserved for Commerce Solution or other programs) | `NAVER-P0-RESTRICTION` (`PERMISSIONS_SCOPES.md` §17) |
| No documented programmatic permission-introspection endpoint | `PERMISSIONS_SCOPES.md` §6 |

### Unresolved (exact)

- The endpoint-to-group mapping for every endpoint not registered in `ENDPOINT_MATRIX.md` §4 (orders, claims, Q&A, shipment), and which inquiry endpoints need both `문의` and `주문 판매자`.
- Whether permission edits propagate to already issued tokens (`PERMISSIONS_SCOPES.md` Q2).
- A specific error code for a missing API group, distinct from generic `GW.AUTHN` (Q3).

### ICBM adoption and runtime state — ICBM-side, not provider facts

- No endpoint of its own: `NOT_APPLICABLE`. `PERMISSIONS_SCOPES.md` owns `write_scope` (= required API-group permission state, not an OAuth scope). The M2 adopted permission union is `{판매자정보}`; `상품` is the planned product-write group (`ENDPOINT_MATRIX.md` §5); a registration flow's requirement is the union over its adopted endpoints (`PERMISSIONS_SCOPES.md` §16).
- Runtime: `UNVERIFIED`. `SMARTSTORE-A0-PERMISSION` is `PASS` at operator-attested strength only; it proves ICBM handles the attestation, not the provider permission model, and `PERMISSIONS_SCOPES.md verified_at` stays `null` (`SOURCES.md` §9, §10.2).

## Coupang

- **Locator:** https://developers.coupang.com/en/getting-started/open-api-test-guide — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** Open API key issuance and the IP allowlist are seller/Wing controlled.
- **Missing:** any per-API permission model.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Captured:** the seller registers/uses an 11ST Open API key (portal https://openapi.11st.co.kr/, 2026-09-28).
- **Missing:** the endpoint permission matrix.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locators:** integration review https://shopping-developers.kakao.com/hc/ko/articles/6592335927183- ; Open API overview https://shopping-developers.kakao.com/hc/ko/articles/4681097907087- — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** not every seller or integrator is eligible; API use requires an integration review/selection and contract; the Open API scope covers product registration/read, order read/process and inquiry read/process for Kakao Shopping channels; Gift API permission is granted separately.
- **Missing:** per-API permission units after approval.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Locator:** https://etapi.gmarket.com/pages/API-가이드 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** API access/permissions are approved per seller/tool/resource; auth success does not imply every resource is allowed.
- **Missing:** the permission units and how a denial is signalled per endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNm=GetStarted — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** APIs are split between public APIs and APIs usable only by specific partners (for example the affiliate-labelled product registration, [PRODUCT_CREATE](PRODUCT_CREATE.md#lotteon)).
- **Missing:** which APIs a general seller may use.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Captured (https://eapi.ssgadm.com/, 2026-09-28, PR #141 pass):** EAPI is vendor/partner-facing and uses vendor authentication keys.
- **Missing:** per-API permission units.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
