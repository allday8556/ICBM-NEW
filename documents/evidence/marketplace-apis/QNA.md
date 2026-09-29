# Q&A / Customer Inquiry

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` + `OFFICIAL_SUPPORT` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locators:** list https://apicenter.commerce.naver.com/docs/commerce-api/current/get-comments-contents ; answer create/update https://apicenter.commerce.naver.com/docs/commerce-api/current/create-or-update-answer-contents — `OFFICIAL_API_DOC`, observed 2026-09-28 (research packet 1; version not recorded by that capture).
- **List:** `GET /v1/contents/qnas` lists product inquiries.
- **Answer:** the official family includes answer create/update; its method/path was not captured.
- **Group:** inquiry capabilities use API group `문의`, and provider guidance shows some inquiry endpoints additionally require `주문 판매자` (`documents/contracts/platforms/smartstore/PERMISSIONS_SCOPES.md` §4.4; `NAVER-P1-GW-AUTHN-GROUP-1013`, `OFFICIAL_SUPPORT`). Which inquiry endpoints need both is not captured.
- **Missing:** list query parameters, paging and response fields; the answer method/path/body; errors; idempotency of the answer write; rate limit.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Captured (https://developers.coupang.com/en/api/cs, 2026-09-28):** the Customer Service family exposes product-inquiry reads/replies and call-center inquiry APIs.
- **Missing:** method/paths and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locator:** https://shopping-developers.kakao.com/hc/ko/articles/4578925770511- (상품문의 목록 조회) — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `GET /v1/store/qna` lists product Q&A, filterable by `qnaId`, `productId` and answer state.
- **Missing:** exact parameter keys for answer state, response fields, the answer endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (https://etapi.gmarket.com/pages/API-가이드, 2026-09-28):** the API guide lists CS inquiry-response management; no endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Locator:** https://ecapi.lotteon.com/apiService/?apiNo=179&menuIdx=8 — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** `POST https://openapi.lotteon.com/v1/openapi/customer/v1/getSellerInquiryList`.
- **Missing:** request/response fields, the answer endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/itemQnaApi.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** Q&A list `POST /api/postng/qnaList.ssg`; answer `POST /api/postng/ansQna.ssg`.
- **Missing:** host, request/response fields, errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
