# Exchange

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint.

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Locator:** https://apicenter.commerce.naver.com/docs/commerce-api/current/교환 — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the exchange family covers collection completion, redelivery, hold/release and rejection/withdrawal. No method/path or field was captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Captured (https://developers.coupang.com/en/api, 2026-09-28):** the Exchanges family exposes request list, receipt confirmation, rejection and waybill upload.
- **Missing:** method/paths and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Captured (https://shopping-developers.kakao.com/hc/ko/articles/4578928106639-, 2026-09-28):** the Claim API covers exchange request, collection, hold/withdrawal and the redelivery waybill flow.
- **Missing:** method/paths and fields.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (2026-09-28):** a claim family exists; no exchange endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (2026-09-28):** claim APIs exist; no general-seller exchange endpoint captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Locator:** https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg — `OFFICIAL_API_DOC`, observed 2026-09-28 (PR #141 pass).
- **Captured:** the return/exchange recollection read `POST /api/pd/{version}/listExchangeTarget.ssg` ([RETURN](RETURN.md#ssgcom)).
- **Missing:** host, request/response fields, and any exchange-processing (write) endpoint.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
