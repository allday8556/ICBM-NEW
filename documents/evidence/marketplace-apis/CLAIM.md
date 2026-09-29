# Claim — cross-claim status

> Evidence catalog; status model and provenance conventions in [README](README.md). Nothing here adopts an endpoint. Individual flows: [CANCEL](CANCEL.md), [RETURN](RETURN.md), [EXCHANGE](EXCHANGE.md).

## Status matrix

| Platform | Source authority | Evidence coverage | ICBM adoption | Runtime verification |
| --- | --- | --- | --- | --- |
| SmartStore | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Coupang | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| 11st | `UNAVAILABLE` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| Kakao Shopping | `OFFICIAL_API_DOC` | `PARTIAL` | `NOT_ADOPTED` | `UNVERIFIED` |
| Gmarket / Auction | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| LotteON | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |
| SSG.COM | `OFFICIAL_API_DOC` | `NOT_CAPTURED` | `NOT_ADOPTED` | `UNVERIFIED` |

## SmartStore

- **Captured (https://apicenter.commerce.naver.com/docs/commerce-api/current, 2026-09-28):** the current reference exposes claim status plus cancel/return/exchange families. No claim-status method/path or field was captured.
- **Missing:** every endpoint-level fact.
- **ICBM:** not registered in `ENDPOINT_MATRIX.md` §4 → `NOT_ADOPTED`; runtime `UNVERIFIED`.

## Coupang

- **Captured (https://developers.coupang.com/en/api, 2026-09-28):** Returns and Exchanges are first-class API families; the order API also exposes cancellation.
- **Missing:** a cross-claim status endpoint, if any.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## 11st

- **Missing:** every endpoint-level fact (no endpoint reference captured, 2026-09-28).
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Kakao Shopping

- **Locators:** Claim API https://shopping-developers.kakao.com/hc/ko/articles/4578928106639- ; change log https://shopping-developers.kakao.com/hc/ko/articles/4681018938255- — `OFFICIAL_API_DOC`, observed 2026-09-28.
- **Captured:** the Claim API unifies cancellation, exchange and return workflows using `claimId` and `orderIds`; some successful actions return a status-only response.
- **Missing:** method/paths, full request/response keys, claim-state enumeration, errors.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## Gmarket / Auction

- **Captured (https://etapi.gmarket.com/pages/API-가이드, 2026-09-28):** the ESM API guide lists claim management as an API family; claim/return APIs are documented on the official portal.
- **Missing:** every endpoint-level fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## LotteON

- **Captured (https://ecapi.lotteon.com/apiService/?apiNo=80&menuIdx=6, 2026-09-28):** the order schema includes a claim number and related fields; no dedicated claim endpoint captured.
- **Missing:** every claim-endpoint fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.

## SSG.COM

- **Captured (https://eapi.ssgadm.com/info/shpp/listExchangeTarget.ssg, 2026-09-28):** shipping/return APIs expose recollection, return/exchange reasons and status ([RETURN](RETURN.md#ssgcom)); no cross-claim status endpoint captured.
- **Missing:** every cross-claim endpoint fact.
- **ICBM:** research-only; `NOT_ADOPTED`, `UNVERIFIED`.
