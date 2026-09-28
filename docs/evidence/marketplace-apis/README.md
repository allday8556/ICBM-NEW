# Domestic Marketplace API Evidence Catalog

> Evidence catalog only. Nothing here adopts an endpoint, enables provider I/O, or proves runtime behavior.
>
> Rule: `provider documented != ICBM adopted != runtime verified`.

## Purpose

Capability-first, platform-second catalog so later integration slices reuse official evidence instead of re-researching it (Issue #140; acceptance spec Issue #140 comment 5861608148).

For every provider contract fact already captured from an official source, the capability file itself holds the implementation-relevant detail. An implementer should not need Issue comments, old PRs, AI memory or a fresh visit to the provider site to recover an already-captured fact. Issue comment ids appear only as provenance.

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

## Navigation

1. Pick the capability file below.
2. Its **Status matrix** gives the four status dimensions for every platform.
3. The platform section under it holds the captured provider contract, the exact unresolved gaps, and, separately, the ICBM adoption/runtime state.

| Area | Files |
| --- | --- |
| Product | [PRODUCT_CREATE](PRODUCT_CREATE.md), [PRODUCT_READ](PRODUCT_READ.md), [PRODUCT_SEARCH](PRODUCT_SEARCH.md), [PRODUCT_UPDATE](PRODUCT_UPDATE.md), [PRODUCT_DELETE](PRODUCT_DELETE.md), [IMAGE_UPLOAD](IMAGE_UPLOAD.md), [CATEGORY](CATEGORY.md), [ATTRIBUTE](ATTRIBUTE.md), [OPTION](OPTION.md), [NOTICE](NOTICE.md), [PRICE](PRICE.md), [STOCK](STOCK.md) |
| Order / fulfillment | [ORDER_READ](ORDER_READ.md), [ORDER_CONFIRM](ORDER_CONFIRM.md), [SHIPMENT](SHIPMENT.md), [TRACKING](TRACKING.md) |
| Claims | [CANCEL](CANCEL.md), [RETURN](RETURN.md), [EXCHANGE](EXCHANGE.md), [CLAIM](CLAIM.md) |
| Customer / common | [QNA](QNA.md), [REVIEW](REVIEW.md), [AUTH](AUTH.md), [PERMISSIONS](PERMISSIONS.md), [RATE_LIMIT](RATE_LIMIT.md), [ERRORS](ERRORS.md) |

The SmartStore field-level `POST /v2/products` contract lives in exactly one place: [PRODUCT_CREATE.md § SmartStore](PRODUCT_CREATE.md#smartstore). `docs/platforms/smartstore/SOURCES.md` §5.2 and `ENDPOINT_MATRIX.md` §4.2 keep the source IDs, the adoption/safety state and a compact summary, and point here for nested keys.

## Status model — four separate dimensions

Every platform × capability entry carries all four. They never imply one another.

| Dimension | Values | Meaning |
| --- | --- | --- |
| Source authority | `OFFICIAL_API_DOC` | provider's official API reference / developer documentation |
| | `OFFICIAL_RELEASE_NOTE` | provider's official notice / release note / change log |
| | `OFFICIAL_SUPPORT` | provider-maintained official technical-support answer (SmartStore `P1` in `SOURCES.md` §2) |
| | `LOGIN_REQUIRED` | the official detail is behind a seller login and was not captured |
| | `UNAVAILABLE` | no official endpoint-level source was captured |
| Evidence coverage | `IMPLEMENTATION_EVIDENCE_COMPLETE` | every provider fact an implementer needs for this capability is recorded here; no fresh external research is needed for field names, required/conditional rules or response identifiers. May carry a stated scope and named excepted sub-scopes (below) |
| | `PARTIAL` | some endpoint-level contract facts (at least method/path or documented request/response fields) are recorded; the specific missing facts are listed |
| | `NOT_CAPTURED` | no endpoint-level contract fact is recorded; at most an official portal, family description or page locator |
| ICBM adoption | `ADOPTED` / `NOT_ADOPTED` | canonical current state from `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4. Only SmartStore rows can be `ADOPTED`; every non-SmartStore row is `NOT_ADOPTED` |
| | `NOT_APPLICABLE` | cross-cutting SmartStore concern with no endpoint of its own; its ICBM behaviour is owned by the named canonical contract |
| Runtime verification | `VERIFIED` / `UNVERIFIED` | `VERIFIED` only when the owning canonical contract records accepted runtime verification (`verified_at` non-null). Documentation retrieval never verifies |

**Scope and exceptions.** A coverage value may be qualified as `IMPLEMENTATION_EVIDENCE_COMPLETE` *for a stated scope*, **except** named sub-scopes. Each excepted sub-scope is itself `PARTIAL` and fail-closed, and the platform section lists exactly what it is missing; the exception never widens to the rest of the entry.

For the cross-cutting files (AUTH, PERMISSIONS, RATE_LIMIT, ERRORS), "endpoint-level contract fact" means a concrete mechanism fact: header/signature format, permission unit, limit value, or error body/status rule.

Two authority values may be listed together (for example `OFFICIAL_API_DOC + OFFICIAL_SUPPORT`) when different facts in the section rest on different classes; each fact line names its own source.

## Per-platform section fields

Where applicable and supported by captured official evidence: source locator(s); authority class; API version and observed/verified date; method/path; auth / permission group; request Content-Type; request structure with proven nested keys; required / optional / conditional fields; limits/defaults; success status/predicate; response envelope and provider identifiers; documented error statuses / body rules; retry / idempotency / replay guarantees or their explicit absence; timeout / connection / response-loss semantics; lookup uniqueness / completeness / freshness; rate limits; exact unresolved gaps; ICBM adoption/runtime state **separately**.

A field that is not listed for a platform was not captured. `Not captured` in a section means exactly that; no fact may be inferred by analogy with another marketplace.

## Provenance conventions

- SmartStore source IDs (`NAVER-P0-*`, `NAVER-P1-*`) resolve through `docs/platforms/smartstore/SOURCES.md` §5–§6 to the official NAVER URL, authority class and version.
- `NAVER-P0-PACKET-289` = Issue #89 comment 5746489554; `NAVER-P0-REVIEW-CREATE-289` = Issue #89 comments 5768199984 and 5768247290; `NAVER-P0-FIELDS-CREATE-289` = Issue #89 comment 5861477977; `NAVER-P0-REQUIRED-CREATE-289` = Issue #89 comment 5861933729. All are architect-reviewed extracts of NAVER Commerce API **2.89.0 (2026-09-15)**.
- Non-SmartStore facts come from Issue #140 research packet 1 (comment 5857814524, 2026-09-28) and the initial catalog pass of the same date (PR #141); each fact carries its official URL.
- ICBM-side decisions (adoption, outcome rules, projections) are labelled as such and cite the ICBM canonical document or architect ruling; they are never provider facts.

## Platform roots

| Platform | Official root | Observed | Note |
| --- | --- | --- | --- |
| SmartStore | https://apicenter.commerce.naver.com/docs/commerce-api/current | 2.89.0 (2026-09-15) for M5 rows; 2.88.0 (2026-09-07), retrieved 2026-09-14, for the M2 contract set | the only connected marketplace; strict adoption/audit path |
| Coupang | https://developers.coupang.com/en/api | 2026-09-28 | RESTful, JSON over HTTPS, HMAC-SHA256 |
| 11st | https://openapi.11st.co.kr/ | 2026-09-28 | portal reachable; the public fetch returned no endpoint reference content |
| Kakao Shopping | https://shopping-developers.kakao.com/hc/ko/categories/4406596840975-API-Docs | 2026-09-28 | API use requires integration review/selection |
| Gmarket / Auction | https://etapi.gmarket.com/category | 2026-09-28 | one ESM Trading API family for both sites |
| LotteON | https://ecapi.lotteon.com/apiService/?apiNm=GetStarted | 2026-09-28 | some captured pages are affiliate-only |
| SSG.COM | https://eapi.ssgadm.com/ | 2026-09-28 | research packet 1 captured no endpoint reference; the SSG facts in this catalog come from the PR #141 pass with the EAPI locators cited per fact |

## Validation level

- SmartStore: the connected marketplace; strict evidence/adoption/runtime rules remain. The strict ledgers under `docs/platforms/smartstore/` stay authoritative for ICBM adoption, runtime and safety state. A SmartStore row not registered in `ENDPOINT_MATRIX.md` §4 is `NOT_ADOPTED` and must go through the normal SmartStore adoption path before implementation.
- Other marketplaces: research-only. Manual check of official source, freshness/date, copied facts and explicit unknowns. Strong cross-audit is deferred until a platform/capability is proposed for ICBM adoption; the cited official source is re-read at that time.

## Provider-specific cautions

- 11st: official portal/key flow exists, but no current seller endpoint contract was captured.
- LotteON: some pages are explicitly labelled affiliate-only (`(계열사)`); do not generalize them to every seller.
- SSG.COM: old online product create/read/update APIs were scheduled to end on 2026-03-31; use the New online product API family for future research.
- Kakao: Open API access requires integration review/permission and has no separate sandbox.
- Gmarket/Auction: one ESM Trading API family covers both sites; site-specific product numbers stay distinct from the master `goodsNo`.

Tracking: Issue #140 (research packet 1: comment 5857814524; corrective acceptance: comment 5861608148). Initial pass 2026-09-28.
