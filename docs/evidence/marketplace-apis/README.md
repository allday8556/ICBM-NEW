# Domestic Marketplace API Evidence Catalog

> Research-only evidence index. Nothing here adopts an endpoint, enables provider I/O, or proves runtime behavior.
>
> Rule: `provider documented != ICBM adopted != runtime verified`.
>
> SmartStore adoption truth remains in `docs/platforms/smartstore/SOURCES.md` and `ENDPOINT_MATRIX.md`.

## Purpose

Capability-first, platform-second catalog so later integration slices can reuse official evidence instead of re-researching it.

```text
PRODUCT_CREATE
  ├─ SmartStore
  ├─ Coupang
  ├─ 11st
  ├─ Kakao
  ├─ Gmarket / Auction
  ├─ LotteON
  └─ SSG.COM
```

## Validation level

- SmartStore: current connected marketplace; strict evidence/adoption/runtime rules remain.
- Other marketplaces: manual pre-research only. Check official source, freshness/date, copied facts, and explicit unknowns.
- Strong GPT+Claude implementation review is deferred until a platform/capability is actually proposed for ICBM adoption.

## Status vocabulary

- `OFFICIAL_CAPTURED`: official provider source captured for the stated fact.
- `PARTIAL_OFFICIAL_CAPTURED`: official source exists, but not enough detail to freeze runtime contract.
- `OFFICIAL_PORTAL_FOUND`: official API portal exists, endpoint detail not captured.
- `OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED`: no current endpoint-level official source captured.
- `LOGIN_REQUIRED`: official detail appears seller-login gated.
- `UNVERIFIED`: do not infer.
- `REFER_TO_PLATFORM_LEDGER`: SmartStore strict provider ledger owns adoption truth.

## First-pass platforms

| Platform | Research state | Official root / note |
| --- | --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER | https://apicenter.commerce.naver.com/docs/commerce-api/current |
| Coupang | OFFICIAL_CAPTURED | https://developers.coupang.com/en/api |
| 11st | OFFICIAL_PORTAL_FOUND | https://openapi.11st.co.kr/ — current seller endpoint detail not captured |
| Kakao Shopping | OFFICIAL_CAPTURED | https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs |
| Gmarket / Auction | OFFICIAL_CAPTURED | https://etapi.gmarket.com/category |
| LotteON | PARTIAL_OFFICIAL_CAPTURED | https://ecapi.lotteon.com/apiService/?apiNm=GetStarted — endpoint eligibility varies by seller type |
| SSG.COM | OFFICIAL_CAPTURED | https://eapi.ssgadm.com/ — current EAPI plus New online product API captured |

## Important provider-specific cautions

- 11st: official portal/key flow exists, but current seller endpoint contracts were not publicly captured in this pass.
- LotteON: some pages are explicitly labelled affiliate-only; do not generalize those to every seller.
- SSG.COM: old online product create/read/update APIs were scheduled to end on 2026-03-31; use the New online product API family for future integration research.
- Kakao: Open API access requires integration review/permission and has no separate sandbox.
- Gmarket/Auction: one ESM Trading API family covers both sites; site-specific product numbers must remain distinct from master `goodsNo`.

## Files

Product: PRODUCT_CREATE, PRODUCT_READ, PRODUCT_SEARCH, PRODUCT_UPDATE, PRODUCT_DELETE, IMAGE_UPLOAD, CATEGORY, ATTRIBUTE, OPTION, NOTICE, PRICE, STOCK.

Order/fulfillment: ORDER_READ, ORDER_CONFIRM, SHIPMENT, TRACKING.

Claims: CANCEL, RETURN, EXCHANGE, CLAIM.

Customer/common: QNA, REVIEW, AUTH, PERMISSIONS, RATE_LIMIT, ERRORS.

Initial manual research pass: 2026-09-28. Tracking: Issue #140, research packet comment 5857814524.
