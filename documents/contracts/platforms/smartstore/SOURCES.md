# SmartStore Source Provenance Ledger

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Purpose | Reverse source index, authority ledger, and provenance-drift audit view |
| Operating model | `INDEX_NOT_SINGLE_SOURCE` |
| Current upstream Commerce API version | `2.88.0` |
| Upstream version date | `2026-09-07` |
| Retrieved / last consolidated | `2026-09-14` |
| Runtime verification | `PENDING` |
| Fallback review due | `2026-10-14` |

This file indexes the SmartStore integration evidence used by ICBM.

It deliberately uses the **index model**, not the single-source model.

The owning contracts remain self-contained and keep their own provenance blocks:

- `ACCOUNT_IDENTITY.md`
- `AUTH.md`
- `PERMISSIONS_SCOPES.md`
- `ERRORS.md`
- `ENDPOINT_MATRIX.md`
- `CAPABILITY_MAPPING.md`

`SOURCES.md` does **not** replace those blocks.

Its responsibilities are:

1. identify the upstream/runtime evidence supporting a contract claim;
2. classify source authority and artifact kind;
3. preserve the limited observation actually relied upon;
4. show which contracts depend on each source;
5. detect provenance drift between otherwise self-contained documents;
6. distinguish documentation evidence from measured runtime evidence;
7. expose claims that currently depend on provider support because normative documentation is silent.

Core rules:

`source documented != runtime verified`

`support discussion != immutable provider contract`

`SOURCES.md = reverse index + drift detector, not a replacement provenance block`

No source row in this file may populate an owning contract's `verified_at` field by itself.

---

## 1. Operating model: self-contained contracts plus reverse index

### 1.1 Why the index model is adopted

Each SmartStore contract must remain understandable and auditable when read by itself.

A reviewer reading only `AUTH.md`, for example, must still be able to see:

- the upstream URLs used by that contract;
- the provider version reviewed;
- when the sources were retrieved;
- whether runtime verification has happened;
- when or why review becomes due.

Therefore ICBM MUST NOT replace each contract's provenance block with only a pointer to `SOURCES.md`.

The duplication is intentional.

`SOURCES.md` exists to make that duplication auditable.

### 1.2 Consequence

When source/version/freshness information changes, the reviewed change must update:

1. every affected self-contained contract; and
2. the corresponding row/baseline in `SOURCES.md`.

Updating only this ledger does not update an owning contract.

Updating only one owning contract while leaving this ledger inconsistent is provenance drift.

---

## 2. Authority hierarchy and artifact kinds

ICBM SHALL distinguish both **authority class** and **artifact kind**.

| Rank | Source class | Artifact kind | Meaning | Contract role | May establish provider contract? |
| --- | --- | --- | --- | --- | --- |
| `P0` | `PROVIDER_NORMATIVE` | `OFFICIAL_DOC` | Current official NAVER Commerce API documentation | Primary provider contract evidence | `YES` — within the reviewed provider documentation scope |
| `P1` | `PROVIDER_OFFICIAL_SUPPORT` | `SUPPORT_DISCUSSION` | NAVER-maintained official Commerce API technical-support discussion | Supporting/clarifying evidence; may temporarily fill a P0 gap when explicitly marked | `LIMITED` — never silently overrides later P0; claim scope must stay explicit |
| `S0` | `EXTERNAL_NORMATIVE_STANDARD` | `NORMATIVE_STANDARD` | Normative protocol standard adopted by the provider contract | Governs only the protocol scope actually adopted by NAVER | `LIMITED` — only for the protocol/profile NAVER actually adopts |
| `L0` | `IMPLEMENTATION_LIBRARY_REFERENCE` | `LIBRARY_REFERENCE` | Documentation/source for a client library ICBM may adopt | Implementation behavior only; never NAVER semantics | `NO` |
| `R0` | `MEASURED_RUNTIME_EVIDENCE` | `RUNTIME_EVIDENCE` | Sanitized evidence measured by ICBM against an authorized real account | Runtime truth for the measured case, subject to generation/time/application scope | `NO` — a contradiction triggers review; R0 does not silently rewrite the provider contract |
| `A0` | `OPERATOR_ATTESTED_EVIDENCE` | `ATTESTATION_EVIDENCE` | Operator-supplied observation/attestation, including provider-admin UI evidence, bound to the recorded application/context | Evidence that ICBM received and handled an attestation at the recorded strength; never machine/provider runtime truth | `NO` — A0 can never establish NAVER/provider contract semantics |

`R0` and `A0` are intentionally different evidence classes:

- `R0 = MEASURED_RUNTIME_EVIDENCE` — ICBM measured a real provider interaction/case;
- `A0 = OPERATOR_ATTESTED_EVIDENCE` — ICBM records and handles a human/operator attestation without converting it into provider runtime truth.

### 2.1 Conflict rule

When sources disagree:

1. current `P0` provider documentation wins over older contradictory support commentary for provider contract semantics;
2. `P1` may clarify ambiguity but MUST NOT silently override a later contradictory `P0` contract;
3. `S0` governs protocol semantics only where NAVER adopts that protocol/profile;
4. `L0` never defines NAVER behavior;
5. `R0` may prove that measured behavior differs from documentation, but that conflict triggers review rather than silently rewriting the documented contract;
6. `A0` never establishes provider behavior and therefore cannot resolve a provider-contract conflict by itself.

A conflict affecting a safety or READY invariant must fail closed until reviewed.

### 2.2 P0-silent / P1-only claims

Some current SmartStore contracts rely on an official provider-support answer because the reviewed P0 documentation does not state the needed behavior.

Those claims are not ordinary "supporting" claims. They SHALL be marked:

`evidence_basis = P1_ONLY`

A `P1_ONLY` claim means:

- no reviewed P0 source currently establishes that exact claim;
- the provider's official support answer is the best available upstream evidence;
- the claim MUST NOT be generalized beyond the limited observation recorded here and in the owning contract;
- the claim has lower durability than an equivalent P0 contract;
- R0 measurement SHOULD be collected when the behavior is practically measurable;
- if the P1 source becomes unavailable, materially edited, or contradicted, the dependent claim enters re-review immediately.

Examples in the current set include:

- the 180-day own-store application re-authentication lifecycle;
- immediate invalidation of an old `client_secret` after provider secret reissue;
- observed cases where missing API-group permission produced `GW.AUTHN`.

`P1_ONLY` does **not** mean false or unusable. It makes the evidence limitation explicit.

### 2.3 No evidence laundering

The following are forbidden:

- turning a support anecdote into a universal provider guarantee;
- turning an operator screenshot/statement into machine-verified provider truth;
- promoting `A0` operator-attested evidence into `R0` measured-runtime evidence without a separate real provider measurement;
- treating `A0` as proof of NAVER/provider contract semantics merely because the attestation came from a provider-admin UI;
- turning a library default into a provider contract;
- turning a documentation retrieval timestamp into runtime verification;
- citing this ledger as though it were the original upstream source.

Owning contracts SHOULD keep the original upstream URL and MAY additionally record the stable source ID defined here.

---

## 3. Provenance timestamps

| Field | Meaning |
| --- | --- |
| `retrieved_at` / `last_checked` | ICBM inspected the source at that time |
| `upstream_version` | Provider documentation release/version associated with the reviewed contract |
| `review_due` | Fallback calendar bound if change detection is absent or broken |
| `verified_at` | Real runtime evidence required by the owning contract was measured and accepted |

For the current M2 document set:

- `upstream_version = 2.88.0`
- `retrieved_at = 2026-09-14`
- `verified_at = null`
- `review_due = 2026-10-14`

A version/contract change takes precedence over the fallback calendar date.

`review_due` is not required to remain globally identical forever. An owning contract may adopt a stricter freshness bound; if it does, the owning contract and the consistency matrix below must change together.

---

## 4. Cross-document provenance consistency matrix

This matrix mirrors the self-contained provenance values. It is an audit index, not their source of truth.

| Contract | Upstream version | Retrieved at | Verified at | Review due | Consistency |
| --- | --- | --- | --- | --- | --- |
| `ACCOUNT_IDENTITY.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `AUTH.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `PERMISSIONS_SCOPES.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `ERRORS.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `ENDPOINT_MATRIX.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `CAPABILITY_MAPPING.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |

### 4.1 Drift rules

For the active M2 SmartStore contract set:

- every contract MUST expose parseable `upstream_version`, `retrieved_at`, `verified_at`, and `review_due` provenance values;
- `upstream_version` MUST match the current SmartStore provider baseline unless a reviewed version-scope exception exists;
- each matrix value MUST exactly mirror the owning contract;
- a targeted re-review MAY legitimately change one contract's `retrieved_at` or `review_due`, but the matching row must change in the same reviewed change;
- `verified_at` MUST NOT become non-null merely because documentation/support was re-read;
- moving any contract to a new provider-version baseline requires impact review of the affected active contract set.

### 4.2 Repository/CI enforcement target

This is a required repository invariant, but its implementation is not part of this documentation-only PR.

Claude Code implementation should extend `test_repository_rules.py` or an equivalent repository-rule test so CI verifies at minimum:

1. every active SmartStore contract exposes required provenance fields;
2. each active contract's `upstream_version` matches the current baseline unless a reviewed exception exists;
3. each contract's provenance values match its `SOURCES.md` consistency row;
4. a new active SmartStore contract cannot omit provenance;
5. a `SUPPORT_DISCUSSION` row cannot omit its summarized observation, dependency list, or P1-only marker when applicable.

The parser SHOULD tolerate Markdown representation differences rather than forcing unrelated formatting changes.

The test MUST NOT permanently require every `review_due` value to be identical.

---

## 5. Primary NAVER provider sources (`P0` / `OFFICIAL_DOC`)

| Source ID | Upstream source | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-CURRENT` | https://apicenter.commerce.naver.com/docs/commerce-api/current | Current Commerce API version and API catalog | all SmartStore contracts | Commerce API version changes from `2.88.0` |
| `NAVER-P0-AUTH` | https://apicenter.commerce.naver.com/docs/auth | OAuth2 Client Credentials, token URL, Bearer auth, scopes N/A, auth/signature rules | `AUTH.md`, `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `ENDPOINT_MATRIX.md`, `CAPABILITY_MAPPING.md` | auth/token/signature/scopes contract changes |
| `NAVER-P0-RESTRICTION` | https://apicenter.commerce.naver.com/docs/restriction | TLS constraints, API-group model, request limits | `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `CAPABILITY_MAPPING.md` | API-group/TLS/rate-limit model changes |
| `NAVER-P0-TROUBLESHOOTING` | https://apicenter.commerce.naver.com/docs/trouble-shooting | gateway error format, Trace ID, gateway code table | `ERRORS.md`, `CAPABILITY_MAPPING.md` | gateway status/code/meaning changes |
| `NAVER-P0-REST` | https://apicenter.commerce.naver.com/docs/restful-api | common REST/request/response conventions | `ERRORS.md`, `ENDPOINT_MATRIX.md` | common REST/error contract changes |
| `NAVER-P0-TOKEN` | https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth | token issuance request/response endpoint contract | `AUTH.md`, `ENDPOINT_MATRIX.md`, `CAPABILITY_MAPPING.md` | method/path/request/response/token-lifetime contract changes |
| `NAVER-P0-SELLER-ACCOUNT` | https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers | protected seller-account read, identity fields, endpoint errors | `ACCOUNT_IDENTITY.md`, `PERMISSIONS_SCOPES.md`, `ENDPOINT_MATRIX.md`, `CAPABILITY_MAPPING.md` | method/path/response/error/permission mapping changes |
| `NAVER-P0-BASIC-INTEGRATION` | https://apicenter.commerce.naver.com/docs/solution-doc/3000/%EA%B8%B0%EB%B3%B8-%EC%97%B0%EB%8F%99-%EC%9A%94%EC%86%8C-%EA%B0%80%EC%9D%B4%EB%93%9C | solution/account mapping guidance and account UID context | `ACCOUNT_IDENTITY.md`, `AUTH.md` | account mapping or solution guidance changes |
| `NAVER-P0-PRODUCT-CREATE` | https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product | product errors; the `(v2) 상품 등록` reference that the M5 CREATE evidence of §5.1–§5.2 was read against | `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore, `ENDPOINT_MATRIX.md` §4.2 | product-create contract changes before M5 adoption |
| `NAVER-P0-PRODUCT-READ` | https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product | product read/error examples and future reconciliation | `ERRORS.md`; M5 planning only | product-read contract changes before M5 adoption |

At consolidation time the official current Commerce API page reports `2.88.0 (2026-09-07)`.

This is documentation provenance, not runtime verification.

### 5.1 M5 PR-D source packet (`2.89.0`)

The implementation session could not reach the provider's documentation host, so PR-D stopped
under §11.4 (Issue #89 comment 5746470583) and the architect supplied an official-source packet
instead (Issue #89 comment **5746489554**), reviewed against release **2.89.0 (2026-09-15)**.

| Source ID | Locator | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-PACKET-289` | Issue #89 comment 5746489554 (architect-reviewed extract of the official 2.89.0 reference, the auth page, the AI-use guide and the 원상품 정보 구조체 schema) | the M5 PR-D endpoint adoption, the `상품` API group, the proven product-structure field names and bounds | `ENDPOINT_MATRIX.md` §4.1, `integrations/marketplaces/smartstore/{registry,product,readback}.py` | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |

Packet scope, and what it deliberately does **not** license:

- proven and used: method and path of the product endpoints; bearer auth with `AUTH_MODE=SELF`
  unchanged; API group `상품`; `multipart/form-data` for image upload; the product-structure
  field names `name`, `detailContent`, `images`(`.url`), `salePrice`, `stockQuantity`,
  `sellerCodeInfo.sellerManagementCode`, option `sellerManagerCode`, `productInfoProvidedNotice`;
  the bounds 999,999,990 / 99,999,999 / ≤3 option dimensions / 1 representative + ≤9 optional
  images; and that notice fields are category-specific with conditional fields omitted;
- **not proven by this packet, so fail-closed**: the image upload part name, the product-search
  request schema (no strong duplicate key), the category query keys of the attribute/option reads,
  and any response field of the category/notice reads. The CREATE request and response contract is
  recorded field by field in `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; its
  sources are in §5.2.

`AUTH_MODE` stays `SELF`: the packet's Commerce Solution `SELLER` guidance is solution-specific and
is not applicable evidence for ICBM's own-store application (§11.4 — unknown beats invention).

### 5.2 M5 CREATE request/response evidence (`2.89.0`)

This is the single ledger entry for the sources of the `POST /v2/products` contract evidence. It
combines the architect's official evidence reviews **5768199984** and **5768247290**, the
field-level packet **5861477977**, the required/conditional-field packet **5861933729** and the
registration-requirement and value-level packet **5862400626** (all retrieved 2026-09-28), the
success-identifier and value-level packet **5868542027** with
packet 5746489554 (§5.1), all read against the current `(v2) 상품 등록`
reference and the `원상품 정보 구조체` and `스마트스토어 채널상품 정보 구조체` schemas of release **2.89.0
(2026-09-15)**. The reviews closed `INSUFFICIENT` for idempotent replay and remote-absence proof
(ADR-0014 §17.2); that verdict is unchanged, and the value-level packets do not overturn it.

| Source ID | Locator | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-REVIEW-CREATE-289` | Issue #89 comments 5768199984 and 5768247290 (architect-reviewed official evidence reviews of https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product, release 2.89.0) | CREATE media type, success status, success identifiers and stored product data, absence of idempotency | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.2; the CREATE adoption slice (ADR-0020 §4) | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |
| `NAVER-P0-FIELDS-CREATE-289` | Issue #89 comment 5861477977 (architect-reviewed field-level extract of the 2.89.0 CREATE reference and the `원상품 정보 구조체` schema, https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/%EC%9B%90%EC%83%81%ED%92%88-%EC%A0%95%EB%B3%B4-%EA%B5%AC%EC%A1%B0%EC%B2%B4) | CREATE top-level objects, nested request keys, success `Content-Type`, documented statuses | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.2; the CREATE adoption slice (ADR-0020 §4) | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |
| `NAVER-P0-REQUIRED-CREATE-289` | Issue #89 comment 5861933729 (architect-reviewed required/optional/conditional-field extract of the 2.89.0 CREATE reference, the `원상품 정보 구조체` schema and the `스마트스토어 채널상품 정보 구조체` schema, https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/%EC%8A%A4%EB%A7%88%ED%8A%B8%EC%8A%A4%ED%86%A0%EC%96%B4-%EC%B1%84%EB%84%90%EC%83%81%ED%92%88-%EC%A0%95%EB%B3%B4-%EA%B5%AC%EC%A1%B0%EC%B2%B4) | schema-marked required `originProduct` fields, M5-relevant conditional rules (options, notice, delivery, after-service, origin area, unit price), channel-product required fields, `windowChannelProduct` scope. Superseded by `NAVER-P0-REGISTRATION-CREATE-289` for the registration requirement of `leafCategoryId` and top-level `stockQuantity` | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.2; the CREATE adoption slice (ADR-0020 §4) | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |
| `NAVER-P0-REGISTRATION-CREATE-289` | Issue #89 comment 5862400626 (architect-reviewed second pass of the 2.89.0 CREATE reference, the `원상품 정보 구조체` schema, the origin-product update reference and the `스마트스토어 채널상품 정보 구조체` schema, re-read 2026-09-28) | the endpoint-specific registration requirements (`leafCategoryId`; top-level `stockQuantity` ≥ 1), which override a conclusion drawn only from generic schema badges; the value-level contract of `statusType` (registration value `SALE`), `deliveryType`, `deliveryAttributeType`, `deliveryFee`, `claimDeliveryInfo`, `optionCombinationSortType`, `skuYn`, `naverShoppingRegistration` and `channelProductDisplayStatusType` | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.2; the CREATE adoption slice (ADR-0020 §4) | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |
| `NAVER-P0-VALUES-CREATE-289` | Issue #89 comment 5868542027 (architect-supplied value-level evidence packet of the 2.89.0 `(v2) 상품 등록` reference https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product, the `원상품 정보 구조체` schema and the `스마트스토어 채널상품 정보 구조체` schema) | E1: `smartstoreChannelProduct.naverShoppingRegistration` is a required JSON **boolean** (its type only; a non-advertiser seller's value is stored as `false`); E2: on registration the CREATE endpoint accepts only **`SALE`** as `originProduct.statusType`; E3: the HTTP 200 success object carries `originProductNo`, `smartstoreChannelProductNo` and `windowChannelProductNo` as **top-level** `integer<int64>` members | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.1.1; `integrations/marketplaces/smartstore/{product,create,execution}.py` | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |
| `NAVER-CATEGORY-LIST-290` | Official Commerce API 2.90.0 `전체 카테고리 조회`, https://apicenter.commerce.naver.com/docs/commerce-api/current/get-category-list-product, read 2026-10-04 | bearer-authenticated `GET /v1/categories`; optional boolean query `last` selects leaf categories; HTTP 200 array fields `wholeCategoryName`, `id`, `name`, `last` | `ENDPOINT_MATRIX.md` §4; `integrations/marketplaces/smartstore/{registry,caller,category}.py`; migration `0041` | Commerce API version changes from `2.90.0` or the response/query schema changes |
| `NAVER-ADDRESSBOOK-LIST-290` | Official Commerce API `주소록 목록 조회` (`GET /v1/seller/addressbooks-for-page`); a real bearer read on 2026-10-04 (canary) returned HTTP 200 with `addressBooks[]` entries carrying `addressBookNo` (integer), `name`, `addressType` (`RELEASE`, `REFUND_OR_EXCHANGE`, `GENERAL`) | only those three fields are retained; the `page` query pages the list | `ENDPOINT_MATRIX.md` §4; `integrations/marketplaces/smartstore/{registry,caller,addressbook}.py` | Commerce API version or response/query schema changes |

Summary, limited to what these sources state: `POST /v2/products` takes an `application/json` body
whose required top-level objects are `originProduct` and `smartstoreChannelProduct`; the
`originProduct` fields required on registration are `statusType` (registration value `SALE`),
`leafCategoryId`, `name`, `detailContent`, `images`, `salePrice`, `stockQuantity` (at least 1) and
`detailAttribute` (`sellerCodeInfo` and `optionInfo` are not provider-required),
`productInfoProvidedNotice` is required for registration,
and further structures carry documented conditional rules; `windowChannelProduct` is a separate
Shopping Window channel structure outside the SmartStore-only scope. Success is HTTP `200`,
`application/json;charset=UTF-8`, returning `originProductNo`, `smartstoreChannelProductNo`,
`windowChannelProductNo` (which may be absent for a SmartStore-only CREATE) as top-level
`integer<int64>` members, and the stored `originProduct`; on registration `statusType` accepts only
`SALE`, and `naverShoppingRegistration` is a required boolean; the documented statuses are `200`,
`308`, `400`, `401`, `403`, `404` and `500`; the reference contains no idempotency key,
request-correlation key, replay rule or duplicate-prevention guarantee. The field-by-field contract
(nested keys, required/optional/conditional rules, limits, defaults) is recorded once, in
`documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore, with a per-field source column;
this ledger does not repeat it.

Not proven, so fail-closed:

- any CREATE idempotency or ambiguous-outcome replay safety. A timeout, a connection loss, a
  response loss or a `5xx` never proves non-application (5768199984, 5768247290; `ERRORS.md` §14.3,
  §15.2), so such a CREATE is `UNKNOWN` and is never resent (ADR-0014 §28);
- that a `500` or a zero-result lookup proves the product was not registered. Neither does
  (ADR-0014 §17.2, §28.2);
- sending a structure merely because the schema contains it. Only fields owned by the immutable
  RegistrationSnapshot/preparation and required by the selected product/channel conditions may be
  sent; an unsupported category/feature condition stays fail-closed / `REVIEW_REQUIRED` (5861477977
  §F, 5861933729);
- an enumerated value these sources do not list; none may be invented. The CREATE `statusType`
  input is listed (`SALE` only, 5862400626, 5868542027 E2);
- which boolean ICBM sends as `naverShoppingRegistration`. The sources give its type (5862400626,
  5868542027 E1), not ICBM's value: that is an ICBM ownership decision, and no value is guessed
  (*amendment note:* the decision is Issue #89 architect resolution `5915900049` D2.1, `true`);
- an ICBM projection for the simple, custom or standard option structures. The evidence selects no
  new option policy: only an option shape the canonical ICBM contracts already allow may be
  projected (5861477977 §C);
- the field set of any type-specific `productInfoProvidedNotice` child. This sub-scope is not
  captured; no value that ICBM does not own may be invented (5861933729 §D). (*Amendment note:*
  §5.5 captures the member sets of seven types and the mapping of 36 at 2.90.0; every other type
  stays uncaptured.)

The per-status meaning stays the product-API error contract of `ERRORS.md` §10: `BAD_REQUEST` is read
from `invalidInputs` together with `message` (§10.1), then `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`
and `INTERNAL_SERVER_ERROR` (§10.2–§10.5), and `308/PERMANENT_REDIRECT` is never followed for a
mutation (§10.6, §17).

This evidence adopts nothing and authorizes no call. `SMARTSTORE_PRODUCT_CREATE_V2` is adopted by
its own adoption slice (ADR-0020 §4 order 1; `ENDPOINT_MATRIX.md` §4.1.1), which froze the success
predicate, timeout, redirect, error classification and the typed request projection against these
facts; the code-side contract (`integrations/marketplaces/smartstore/registry.py`, `product.py`,
`create.py`) changed with that slice, not with this evidence record. Adoption is a contract, never a
call: execution stays `DRY_RUN` and the provider-evidence verdict above is unchanged.

### 5.3 M5 product-search evidence (`2.89.0`)

This is the single ledger entry for the sources of the `POST /v1/products/search` contract that the
positive-only reconcile slice adopts (ADR-0020 §4 order 2).

| Source ID | Locator | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-SEARCH-289` | Issue #89 comment 5904349289 (architect resolution of the 2.89.0 `상품 목록 조회` reference, https://apicenter.commerce.naver.com/docs/commerce-api/current/search-product, S1–S4) | S1 the seller-code request (`searchKeywordType` `SELLER_CODE`, `sellerManagementCode`, `page` from 1, `size` at most 500, `application/json`); S2 the HTTP 200 page (`contents[]` with `originProductNo` and `channelProducts[]` carrying `originProductNo`, `channelProductNo`, `channelServiceType` `STOREFARM`/`WINDOW`/`AFFILIATE` and `sellerManagementCode`; `page`, `size`, `totalElements`, `totalPages`, `first`, `last`); S3 the documented statuses and the gateway `429` rate/quota refusals; S4 the page-size bound and the dynamic rate/quota model | `documents/evidence/marketplace-apis/PRODUCT_SEARCH.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.1.2; `integrations/marketplaces/smartstore/{search,caller,execution}.py` | Commerce API version changes from `2.89.0`, or the host becomes reachable again for direct re-read |

The support answers of §5.2's reviews (similar/partial/exact seller-code matching, no uniqueness, no
completeness or freshness guarantee) still bind this endpoint. This evidence adopts nothing and
authorizes no call; the adoption, and the rule that no search result is ever remote absence or a
CREATE authorization, are `ENDPOINT_MATRIX.md` §4.1.2 and ADR-0014 §28.2–§28.4.

---

### 5.4 M5 origin-read status evidence (`2.90.0`)

This is the single ledger entry for the source of the two published-state members the origin
read-back retains.

| Source ID | Locator | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-READ-STATUS-290` | Issue #89 comment 5911962320 (research packet of the 2.90.0 `(v2) 원상품 조회` reference, https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product, read 2026-09-30) | the 200 response's top-level members (`groupProduct`, `originProduct`, `windowChannelProduct`, `smartstoreChannelProduct`); `originProduct.statusType` (string; `WAIT`, `SALE`, `OUTOFSTOCK`, `UNADMISSION`, `REJECTION`, `SUSPENSION`, `CLOSE`, `PROHIBITION`, `DELETE`); `smartstoreChannelProduct.channelProductDisplayStatusType` (string; `WAIT` 전시 대기, `ON` 전시 중, `SUSPENSION` 전시 중지) | `documents/evidence/marketplace-apis/PRODUCT_READ.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.1; `integrations/marketplaces/smartstore/readback.py` | any change of the origin-read response schema or of either enumeration |

**How it was read, and what that limits.** The official reference is not reachable from the
implementing environment, so the page was read through GPT (`codex`, web access) and the result is
recorded verbatim in the locator comment. It is a reading of `P0` documentation, not an architect
resolution and not runtime evidence, and nothing here is `R0`. It is no longer single-reader: the
architect independently re-read the 2.90.0 reference and confirmed this packet in Issue #89
resolution `5915900049` ("This independently confirms the facts recorded in research packet
`5911962320`. The packet is no longer single-reader evidence for these members."). It agrees with the enumerations already recorded for the same two structures by §5.2's
packets. The channel-product read response was not accessible to that reader and nothing about it
is recorded. The reference states no read-after-write consistency and does not state that `SALE`
with `ON` guarantees a buyer can see and purchase the listing.

This evidence adopts nothing and authorizes no call.

---

### 5.5 M5 CREATE notice-child evidence (`2.90.0`)

This is the single ledger entry for the pinned `productInfoProvidedNotice` table of the CREATE
projection (Issue #89 architect resolution `5915900049` D2.3).

| Source ID | Locator | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-NOTICE-CHILD-290` | Issue #89 comment 5916962285 (research packet of the 2.90.0 `(v2) 상품 등록` reference and its `원상품 정보 구조체` schema, https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product, read 2026-10-01) | `productInfoProvidedNoticeType` (string, required; "하위 요소 중 하나를 선택해서 입력"); the explicit type → sibling member mapping of 36 enum values (`LODGMENT_RESERVATION`, `TRAVEL_PACKAGE`, `AIRLINE_TICKET`, `RENT_CAR`: no documented member); the member sets, types, required badges, length bounds and conditional rules of `wear`, `shoes`, `homeAppliances`, `kitchenUtensils`, `cosmetic`, `generalFood` and `etc`; the five repeated members and their "미입력 시 상품상세 참조로 입력됩니다" default | `documents/evidence/marketplace-apis/PRODUCT_CREATE.md` § SmartStore; `NOTICE.md` § SmartStore; `ENDPOINT_MATRIX.md` §4.1.1; `integrations/marketplaces/smartstore/product.py` (`smartstore-notice-children/2.90.0-r1`) | any change of the notice-type enumeration, of a mapping, or of a captured child's members |

**How it was read, and what that limits.** The official reference is not reachable from the
implementing environment, so it was read through GPT (`codex`, web access, read-only, a prompt
that forbade inference) and the answer is recorded unchanged in the locator comment. It is a
single reading of `P0` documentation, not an architect resolution and not runtime evidence, and
nothing here is `R0`. The two notice-lookup pages it could open still rendered 2.89.0; the table
was taken from the 2.90.0 CREATE and schema pages only. The exact string `상세페이지 참조` does not
occur in the schema; the documented text is `상품상세 참조`, as the omission default of the five
repeated members, and the reference does not say that string itself may be sent — so ICBM omits
such a member and never sends the string.

### 5.6 M6-C order-read evidence (`2.90.1`)

| Source ID | Locator | Claims supported | Used by | Re-review trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-ORDER-READ-2901` | Issue #219 comment 6035983005 (research packet of the 2.90.1 `변경 상품 주문 내역 조회` — https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-last-changed-status-pay-order-seller — and `상품 주문 상세 내역 조회` — https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-product-orders-pay-order-seller — with their schemas, the RESTful API and 제약 사항 pages, read 2026-10-07) | the two methods and paths; the change query parameters, the 300 limit and the default 24-hour span; the `more`/`moreFrom`/`moreSequence` continuation; the change, order, product-order, shipping-address and delivery members and their types; the status, change, claim and place-order enumerations; KST ISO 8601 date-times; `429` `GW.RATE_LIMIT`/`GW.QUOTA_LIMIT`. NOT STATED: the API group, a maximum window, a fixed call rate | `ENDPOINT_MATRIX.md` §4, §4.1.4; `documents/evidence/marketplace-apis/ORDER_READ.md` § SmartStore; `integrations/marketplaces/smartstore/{registry,caller,orders}.py` (`m6-orders-r1`); ADR-0023 §5–§7 | Commerce API version or any order-read request/response schema change |

### 5.7 M6-C order-read runtime evidence (`R0`, 2026-10-08)

| Source ID | Locator | Claims supported | Used by | Re-review trigger |
| --- | --- | --- | --- | --- |
| `SMARTSTORE-R0-ORDER-EMPTY-WINDOW` | Issue #219 comment 6048219909 (30 change windows read through ICBM's own caller on the operating data, structure only, no value recorded) | a window with no change answers HTTP 200 with exactly `timestamp` and `traceId`; unpaced consecutive calls meet `429` with `code`/`message`; the 주문 판매자 group is granted to the application | `integrations/marketplaces/smartstore/registry.py` (`empty_window_answer`, `m6-order-changes-r2`); `app/stages/operate/orders.py` (`PROVIDER_PAUSE_S`); `ENDPOINT_MATRIX.md` §4.1.4 | a change of the empty answer, or a documented rate limit |

### 5.8 M6.5 dispatch and delivery evidence (`2.90.1`)

| Source ID | Locator | Claims supported | Used by | Re-review trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-DISPATCH-DELIVERY-2901` | Issue #219 comment 6053086881 (research packet of the 2.90.1 `발송 처리` — https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-dispatch-product-orders-pay-order-seller — the `상품 주문 상세 내역 조회` `delivery` object and the `상품 주문 정보 구조체` schema, with the 제약 사항 page, read 2026-10-08) | the dispatch method and path, its body, the 30-order limit, the per-order success and fail lists and the documented error codes; the `deliveryMethod`, `deliveryCompanyCode` and `deliveryStatus` enumerations; the members of the detail read's `delivery` object and their types. NOT STATED: the API group, the dispatchable statuses, the `dispatchDate` range, re-dispatch behaviour, idempotency, a tracking-change endpoint | ADR-0025 §4–§6; `ENDPOINT_MATRIX.md` §4.1.4 (the delivery read-back, `m65-delivery-r1`); `integrations/marketplaces/smartstore/{registry,orders,delivery_companies}.py` | Commerce API version or any dispatch, delivery-object or carrier-code change |

## 6. NAVER official technical-support sources (`P1` / `SUPPORT_DISCUSSION`)

These sources are point-in-time official support evidence. They may later be edited, hidden, deleted, or superseded.

| Source ID | URL | Observation preserved by ICBM | Basis | Dependent contract(s) |
| --- | --- | --- | --- | --- |
| `NAVER-P1-ACCOUNT-UID-2425` | https://github.com/commerce-api-naver/commerce-api/discussions/2425 | `accountUid`/`accountId` uniqueness guidance; `accountUid` Commerce API integration use | `SUPPORTING` | `ACCOUNT_IDENTITY.md`, `ENDPOINT_MATRIX.md`, `CAPABILITY_MAPPING.md` |
| `NAVER-P1-SELF-ACCOUNT-3339` | https://github.com/commerce-api-naver/commerce-api/discussions/3339 | own-store application uses `SELF`; token response does not itself provide seller identity | `SUPPORTING` | `AUTH.md` |
| `NAVER-P1-APP-REAUTH-3557` | https://github.com/commerce-api-naver/commerce-api/discussions/3557 | own-store application re-authentication is a web/manual action; authentication validity guidance is 180 days from authentication | `P1_ONLY` | `AUTH.md`, `CAPABILITY_MAPPING.md` |
| `NAVER-P1-SECRET-REISSUE-1564` | https://github.com/commerce-api-naver/commerce-api/discussions/1564 | provider application-secret reissue makes the previous secret unusable immediately | `P1_ONLY` | `AUTH.md` |
| `NAVER-P1-TIMESTAMP-357` | https://github.com/commerce-api-naver/commerce-api/discussions/357 | token-signature timestamp is millisecond Unix time; documented validity window/clock-sync guidance | `SUPPORTING` | `AUTH.md` |
| `NAVER-P1-SELF-BODY-3751` | https://github.com/commerce-api-naver/commerce-api/discussions/3751 | own-store `SELF`; no `account_id`; `grant_type=client_credentials`; strict body shape | `SUPPORTING` | `AUTH.md`, `ENDPOINT_MATRIX.md` |
| `NAVER-P1-OWN-STORE-780` | https://github.com/commerce-api-naver/commerce-api/discussions/780 | `내스토어 애플리케이션` uses `type=SELF`; one own-store application is connected to one SmartStore account under the cited provider guidance | `P1_ONLY_FOR_1_TO_1_CLAIM` | `ENDPOINT_MATRIX.md`; identity/binding design context |
| `NAVER-P1-GW-AUTHN-GROUP-1013` | https://github.com/commerce-api-naver/commerce-api/discussions/1013 | missing required API-group permission can produce `GW.AUTHN`; provider guidance points to the application's API-group configuration | `P1_ONLY_FOR_PERMISSION_CAUSE` | `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `CAPABILITY_MAPPING.md` |
| `NAVER-P1-PRODUCT-GROUP-1835` | https://github.com/commerce-api-naver/commerce-api/discussions/1835 | product API authorization tied to the `상품` API group; observed missing-group `GW.AUTHN` case | `P1_ONLY_FOR_PERMISSION_CAUSE` | `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `CAPABILITY_MAPPING.md` |
| `NAVER-P1-SELLERINFO-GROUP-1895` | https://github.com/commerce-api-naver/commerce-api/discussions/1895 | seller-information API calls require `판매자정보` group | `SUPPORTING` | `PERMISSIONS_SCOPES.md`, `CAPABILITY_MAPPING.md` |
| `NAVER-P1-ORDERSELLER-GROUP-1093` | https://github.com/commerce-api-naver/commerce-api/discussions/1093 | order-seller APIs require `주문 판매자` group | `SUPPORTING` | `PERMISSIONS_SCOPES.md` |
| `NAVER-P1-GW-AUTHN-HEADER-3676` | https://github.com/commerce-api-naver/commerce-api/discussions/3676 | malformed Authorization header can produce `GW.AUTHN` | `P1_ONLY_FOR_OBSERVED_CAUSE` | `ERRORS.md` |
| `NAVER-P1-GW-AUTHN-EXPIRED-3762` | https://github.com/commerce-api-naver/commerce-api/discussions/3762 | expired access token can produce `401/GW.AUTHN` | `SUPPORTING` | `ERRORS.md` |
| `NAVER-P1-IP-TEMPORARY-3428` | https://github.com/commerce-api-naver/commerce-api/discussions/3428 | same outbound IP intermittently received `GW.IP_NOT_ALLOWED` and success; NAVER later identified a temporary provider-side condition | `P1_ONLY_FOR_OBSERVED_EXCEPTION` | `ERRORS.md` |
| `NAVER-P1-BADREQ-NOTICE-1649` | https://github.com/commerce-api-naver/commerce-api/discussions/1649 | structured `BAD_REQUEST` / `invalidInputs` example caused by a missing required product-notice field | `SUPPORTING_EXAMPLE` | `ERRORS.md` |
| `NAVER-P1-BADREQ-POLICY-3529` | https://github.com/commerce-api-naver/commerce-api/discussions/3529 | `BAD_REQUEST` can also represent a provider policy restriction such as restricted seller tags | `P1_ONLY_FOR_OBSERVED_EXAMPLE` | `ERRORS.md` |

### 6.1 Support-discussion survivability rule

A `SUPPORT_DISCUSSION` URL by itself is insufficient durable provenance.

Every P1 entry MUST preserve in repository-controlled text:

- the limited observation actually relied upon;
- the dependent contract(s);
- whether the claim is ordinary supporting evidence or `P1_ONLY`/claim-specific P1-only evidence;
- enough wording to reconstruct the historical rationale if the external thread becomes unavailable.

The repository summary is historical rationale, not current provider verification.

---

## 7. External normative standards (`S0` / `NORMATIVE_STANDARD`)

| Source ID | Source | Scope in ICBM | Dependent contract(s) | Freshness |
| --- | --- | --- | --- | --- |
| `OAUTH-S0-RFC6749` | https://www.rfc-editor.org/rfc/rfc6749 | OAuth 2.0 Client Credentials/access-token response/token-type semantics | `AUTH.md`, `ENDPOINT_MATRIX.md`, `CAPABILITY_MAPPING.md` | re-review if provider changes OAuth protocol/profile |

ICBM uses RFC 6749 only for OAuth semantics NAVER actually adopts.

`token_type` is required by the OAuth access-token response contract; ICBM currently understands SmartStore `Bearer`. Unsupported token types fail closed.

---

## 8. Conditional implementation-library sources (`L0` / `LIBRARY_REFERENCE`)

| Source ID | Source | Conditional use | Status |
| --- | --- | --- | --- |
| `HTTPX-L0-QUICKSTART` | https://www.python-httpx.org/quickstart/ | HTTPX redirect/default-client behavior | `CONDITIONAL` |
| `HTTPX-L0-CLIENT-SOURCE` | https://github.com/encode/httpx/blob/master/httpx/_client.py | HTTPX redirect method/body behavior including 307/308 | `CONDITIONAL` |

If HTTPX is adopted, these references must be bound to the actual pinned version/commit used by ICBM rather than relying indefinitely on a moving page or `master`.

`library behavior != provider contract`

---

## 9. Contract-to-source and evidence dependency matrix

| Contract | Primary source IDs | Supporting / P1-only source IDs | Required evidence before `verified_at` |
| --- | --- | --- | --- |
| `ACCOUNT_IDENTITY.md` | `NAVER-P0-CURRENT`, `NAVER-P0-SELLER-ACCOUNT`, `NAVER-P0-BASIC-INTEGRATION` | `NAVER-P1-ACCOUNT-UID-2425` | `SMARTSTORE-R0-SELLER-ACCOUNT` |
| `AUTH.md` | `NAVER-P0-AUTH`, `NAVER-P0-TOKEN`, `NAVER-P0-BASIC-INTEGRATION`, `OAUTH-S0-RFC6749` | `NAVER-P1-SELF-ACCOUNT-3339`, `NAVER-P1-APP-REAUTH-3557`, `NAVER-P1-SECRET-REISSUE-1564`, `NAVER-P1-TIMESTAMP-357`, `NAVER-P1-SELF-BODY-3751` | `SMARTSTORE-R0-TOKEN`, `SMARTSTORE-R0-SELLER-ACCOUNT`, `SMARTSTORE-R0-FIRST-TOKEN-CRASH`, `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`, `SMARTSTORE-R0-APP-REAUTH` |
| `PERMISSIONS_SCOPES.md` | `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`, `NAVER-P0-CURRENT`, `NAVER-P0-PRODUCT-CREATE` | `NAVER-P1-GW-AUTHN-GROUP-1013`, `NAVER-P1-PRODUCT-GROUP-1835`, `NAVER-P1-SELLERINFO-GROUP-1895`, `NAVER-P1-ORDERSELLER-GROUP-1093` | `SMARTSTORE-A0-PERMISSION` validates attestation handling only. It MUST NOT make `PERMISSIONS_SCOPES.md verified_at` non-null. Full permission-contract `verified_at` remains `null` until a machine-readable provider permission source or a separately reviewed provider-measured equivalent can verify the provider permission model. Reason while unavailable: `NO_MACHINE_READABLE_PERMISSION_SOURCE`. Actual write proof remains separate. |
| `ERRORS.md` | `NAVER-P0-TROUBLESHOOTING`, `NAVER-P0-REST`, `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`, `NAVER-P0-PRODUCT-CREATE`, `NAVER-P0-PRODUCT-READ` | error-related P1 rows above | adopted-endpoint measured error/ambiguity evidence required by `ERRORS.md`; no blanket `verified_at` from documentation fixtures alone |
| `ENDPOINT_MATRIX.md` | `NAVER-P0-CURRENT`, `NAVER-P0-AUTH`, `NAVER-P0-TOKEN`, `NAVER-P0-SELLER-ACCOUNT`, `NAVER-P0-REST`, `OAUTH-S0-RFC6749`; for §4.1–§4.2 `NAVER-P0-PRODUCT-CREATE`, `NAVER-P0-PACKET-289`, `NAVER-P0-REVIEW-CREATE-289`, `NAVER-P0-FIELDS-CREATE-289`, `NAVER-P0-REQUIRED-CREATE-289`, `NAVER-P0-REGISTRATION-CREATE-289` | `NAVER-P1-OWN-STORE-780`, `NAVER-P1-ACCOUNT-UID-2425`, `NAVER-P1-SELF-BODY-3751` | `SMARTSTORE-R0-TOKEN`, `SMARTSTORE-R0-SELLER-ACCOUNT` plus allow-list/no-redirect repository/runtime acceptance evidence |
| `CAPABILITY_MAPPING.md` | `NAVER-P0-CURRENT`, `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`, `NAVER-P0-TROUBLESHOOTING`, `NAVER-P0-TOKEN`, `NAVER-P0-SELLER-ACCOUNT`, `OAUTH-S0-RFC6749` | `NAVER-P1-ACCOUNT-UID-2425`, `NAVER-P1-APP-REAUTH-3557`, `NAVER-P1-GW-AUTHN-GROUP-1013`, `NAVER-P1-PRODUCT-GROUP-1835`, `NAVER-P1-SELLERINFO-GROUP-1895` | `SMARTSTORE-R0-TOKEN`, `SMARTSTORE-R0-SELLER-ACCOUNT`, `SMARTSTORE-R0-FIRST-TOKEN-CRASH`, `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`, `SMARTSTORE-A0-PERMISSION`, `SMARTSTORE-R0-APP-REAUTH`; plus capability-state/UI/state-transition acceptance per `CAPABILITY_MAPPING.md` |

The owning contract remains authoritative for its exact acceptance set.

`SMARTSTORE-A0-PERMISSION=ACCEPTED` does not satisfy full runtime verification of the NAVER permission model. Until machine-readable provider permission introspection or an explicitly reviewed provider-measured equivalent exists, `PERMISSIONS_SCOPES.md verified_at` remains `null` even when the A0 handling slot is accepted.

---

## 10. Evidence registries — pending

### 10.1 Runtime evidence registry (`R0` / `RUNTIME_EVIDENCE`)

| Evidence ID | Target | Required minimum proof | Status |
| --- | --- | --- | --- |
| `SMARTSTORE-R0-TOKEN` | `SMARTSTORE_AUTH_TOKEN` | real authorized token call, request shape, success predicate, observed latency/lifetime, sanitized trace | `PASS` (M2 closeout `m2-campaign-02`) |
| `SMARTSTORE-R0-SELLER-ACCOUNT` | `SMARTSTORE_SELLER_ACCOUNT` | real protected GET using current committed session, observed `accountUid`, identity comparison, sanitized trace | `PASS` (M2 closeout `m2-campaign-02`) |
| `SMARTSTORE-R0-FIRST-TOKEN-CRASH` | AUTH Case E | measured bounded recovery behavior for remote token success followed by lost/uncommitted local result | `PASS` (M2 closeout `m2-campaign-02`) |
| `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW` | token persistence/reissue decision | >30m, <30m, restart, old-token validity, and reissue behavior | `DEFERRED_LONG_HORIZON` (non-gating for M2; a supporting more-than-30-minutes observation is recorded in `AUTH.md` §24.3) |
| `SMARTSTORE-R0-APP-REAUTH` | application re-authentication detection | observe or obtain provider-supported evidence for the machine-visible condition that distinguishes application re-authentication required from generic auth failure; record safe recovery behavior | `BLOCKED_BY_TIME` (non-gating for M2) |

All R0 slots use `evidence_kind=PROVIDER_MEASURED`.

`PENDING` is not failure.

### 10.2 Operator-attested evidence registry (`A0` / `ATTESTATION_EVIDENCE`)

| Evidence ID | Target | Required minimum proof | Status |
| --- | --- | --- | --- |
| `SMARTSTORE-A0-PERMISSION` | permission-attestation handling | operator/provider-admin attestation stored with `evidence_strength=OPERATOR_ATTESTED`, current application fingerprint, required/observed groups, mapping revision, freshness, and zero-network handling proof | `PASS` (OPERATOR_ATTESTED, limited strength; M2 closeout `m2-campaign-02`) |

`SMARTSTORE-A0-PERMISSION` proves only that ICBM handles operator-attested permission evidence correctly. It does **not** prove that NAVER machine-reports those permissions, that a provider API confirms them, or that product write is READY.

All A0 slots use `evidence_kind=OPERATOR_ATTESTED`.

A0 evidence MUST NOT be promoted to R0 without a distinct provider-measured observation satisfying an R0 contract.

An owning contract's `verified_at` may become non-null only after its own complete required evidence/acceptance set has been accepted. One shared slot never automatically verifies every dependent contract.

Until `SMARTSTORE-R0-APP-REAUTH` is accepted, `APPLICATION_REAUTH_REQUIRED` remains a schema-reserved remediation reason but MUST NOT be automatically inferred from generic token/auth failures.

All R0 evidence must carry generation/time/application bindings required by the owning contract. A0 evidence must carry the application/mapping/freshness bindings required by the owning permission contract.

---

## 11. Freshness and invalidation rules

### 11.1 Immediate review triggers

Re-review affected SmartStore contracts when any of the following occurs:

- official Commerce API version changes from `2.88.0`;
- token endpoint method/path/request/response/lifetime contract changes;
- seller-account identity schema changes;
- NAVER changes `accountUid` / `accountId` guidance;
- OAuth/auth mode or `SELF`/`SELLER` behavior changes;
- API-group permission model or endpoint/group mapping changes;
- gateway/domain error semantics change;
- redirect behavior or adopted endpoint responses change;
- own-store application re-authentication policy changes;
- NAVER introduces machine-readable permission introspection;
- an adopted implementation library changes behavior relied on by a safety invariant;
- measured R0 evidence contradicts an adopted contract;
- a materially required `P1_ONLY` source disappears, changes incompatibly, or is superseded.

### 11.2 Calendar fallback

If automated change detection is absent or broken, the owning contract's `review_due` applies.

Calendar review is a fallback, not the primary change detector.

### 11.3 Contract freshness is separate from runtime capability

`contract_freshness in {CURRENT, STALE, REVIEW_REQUIRED}` is separate from runtime states such as auth/write_scope/write.

A calendar/source freshness failure by itself MUST NOT blindly demote a still-valid runtime-proven capability.

`STALE` blocks expansion/new trust decisions while existing runtime-proven behavior may continue under its existing proof.

`REVIEW_REQUIRED` means a contradiction/incompatible change/evidence conflict exists; operations depending on that disputed invariant stop until review resolves it.

### 11.4 Unknown beats invention

When a source is stale, unavailable, contradictory, or insufficient:

- do not infer a favorable provider guarantee;
- do not preserve a documentation-derived READY claim merely because an older source once supported it;
- mark affected contract/evidence freshness stale or review-required;
- let the owning contract determine whether a specific runtime state becomes UNKNOWN;
- require review/fresh measurement before a new trust or expansion decision.

---

## 12. Adding a new source

A new SmartStore source entry records at least:

- stable `source_id`;
- source class and artifact kind;
- canonical URL/evidence locator;
- exact supported claim(s);
- dependent contract(s);
- P1-only basis when applicable;
- retrieval/measurement time;
- upstream version when applicable;
- freshness/review trigger;
- documentation/operator/runtime evidence class.

`source -> claim -> impacted contract -> tests/evidence -> review`

A source addition alone does not authorize implementation behavior.

---

## 13. Source removal / supersession

Do not silently delete a source supporting an adopted contract.

When a source becomes obsolete, inaccessible, materially edited, or contradicted:

1. mark it superseded/unavailable or replace it in the same reviewed change;
2. identify every dependent contract/P1-only claim;
3. re-evaluate affected invariants;
4. update tests/evidence requirements;
5. preserve enough repository-controlled rationale to explain the change.

A cached support summary is historical rationale, not current verification.

---

## 14. Final provenance invariants

`self-contained provenance remains in each owning contract`

`SOURCES.md = reverse index + consistency audit, not single provenance source`

`provider documentation != runtime verification`

`P1_ONLY = explicit evidence limitation, not hidden normative truth`

`support URL alone != durable rationale`

`R0 = provider-measured runtime evidence only`

`A0 = operator-attested evidence only`

`A0 cannot establish provider contract or provider runtime truth`

`A0 -> R0 promotion without distinct provider measurement is forbidden`

`operator attestation != machine verification`

`library behavior != provider behavior`

`retrieved_at != verified_at`

`owning contract verified_at requires its complete accepted evidence/acceptance set`

`PERMISSIONS_SCOPES.md verified_at remains null while no machine-readable permission source or reviewed provider-measured equivalent exists`

`contract freshness != runtime capability truth`

`stale contract understanding blocks unsafe expansion/new trust decisions`

`cross-document provenance drift must be detectable and later CI-enforced`

`runtime contradiction -> investigate and fail closed before rewriting canonical truth`
