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
- Strong GPT+Claude review is deferred until a platform/capability is actually proposed for ICBM adoption.

## Status vocabulary

- `OFFICIAL_CAPTURED`: official provider source captured for the stated fact.
- `PARTIAL_OFFICIAL_CAPTURED`: official source exists, but not enough detail to freeze runtime contract.
- `OFFICIAL_PORTAL_FOUND`: official API portal exists, endpoint detail not captured.
- `OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED`: no current endpoint-level official source captured.
- `LOGIN_REQUIRED`: official detail appears seller-login gated.
- `UNVERIFIED`: do not infer.
- `REFER_TO_PLATFORM_LEDGER`: SmartStore strict provider ledger owns adoption truth.

## First-pass platforms

| Platform | Research state |
| --- | --- |
| SmartStore | REFER_TO_PLATFORM_LEDGER |
| Coupang | OFFICIAL_CAPTURED |
| 11st | OFFICIAL_PORTAL_FOUND |
| Kakao Shopping | OFFICIAL_CAPTURED |
| Gmarket / Auction | OFFICIAL_CAPTURED |
| LotteON | PARTIAL_OFFICIAL_CAPTURED |
| SSG.COM | OFFICIAL_ENDPOINT_SOURCE_NOT_CAPTURED |

Initial manual research pass: 2026-09-28. Tracking: Issue #140, research packet comment 5857814524.
