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

This file is the source index for the SmartStore integration contracts under this directory.

It deliberately uses the **index model**, not the single-source model.

The contract documents remain self-contained and keep their own provenance blocks:

- `ACCOUNT_IDENTITY.md`
- `AUTH.md`
- `PERMISSIONS_SCOPES.md`
- `ERRORS.md`
- `ENDPOINT_MATRIX.md`
- `CAPABILITY_MAPPING.md` when added

`SOURCES.md` does **not** replace those provenance blocks.

Its job is to answer:

1. Which upstream source supports a contract claim?
2. What authority and artifact kind does that source have?
3. Which ICBM documents depend on it?
4. What event makes the source stale enough to require review?
5. Are the duplicated provenance fields across the self-contained contracts still consistent?
6. Has the claim merely been documented, or has it been measured at runtime?

Core rules:

`source documented != runtime verified`

`SOURCES.md = reverse index + drift detector, not a replacement provenance block`

No source row in this file may populate a contract's `verified_at` field by itself.

---

## 1. Operating model: self-contained contracts plus reverse index

### 1.1 Why the index model is adopted

Each SmartStore contract must remain understandable and auditable when read by itself.

A reviewer reading only `AUTH.md`, for example, must still be able to see:

- the upstream URLs used by that contract;
- the upstream version reviewed;
- when the source was retrieved;
- whether runtime verification has happened;
- when or why review becomes due.

Therefore ICBM MUST NOT replace each contract's provenance block with only a pointer to `SOURCES.md`.

The duplication is intentional.

`SOURCES.md` exists to make that intentional duplication auditable.

### 1.2 Consequence

When a source/version/freshness value changes, the reviewed change must update:

1. every affected self-contained contract; and
2. the matching row or baseline in `SOURCES.md`.

Updating only this ledger does not update an owning contract.

Updating only one owning contract while leaving this ledger inconsistent is provenance drift and must be caught before merge once repository enforcement is implemented.

---

## 2. Authority hierarchy and artifact kinds

ICBM SHALL distinguish both **authority class** and **artifact kind**.

| Rank | Source class | Artifact kind | Meaning | May establish provider contract? |
| --- | --- | --- | --- | --- |
| `P0` | `PROVIDER_NORMATIVE` | `OFFICIAL_DOC` | Current official NAVER Commerce API documentation / provider contract | Yes |
| `P1` | `PROVIDER_OFFICIAL_SUPPORT` | `SUPPORT_DISCUSSION` | NAVER-maintained official technical-support repository/discussion | Supporting/clarifying evidence only |
| `S0` | `EXTERNAL_NORMATIVE_STANDARD` | `NORMATIVE_STANDARD` | Normative protocol standard adopted by the provider contract | Yes, within protocol scope |
| `L0` | `IMPLEMENTATION_LIBRARY_REFERENCE` | `LIBRARY_REFERENCE` | Documentation/source for a client library ICBM may adopt | Only for implementation behavior, never provider semantics |
| `R0` | `MEASURED_RUNTIME_EVIDENCE` | `RUNTIME_EVIDENCE` | Sanitized evidence measured by ICBM against an authorized real account | Runtime truth for the measured case, subject to generation/time scope |

### 2.1 Conflict rule

When sources disagree:

1. current `P0` provider documentation wins over older support commentary for provider contract semantics;
2. `P1` may clarify ambiguity but MUST NOT silently override a later contradictory `P0` contract;
3. `S0` governs protocol semantics only where the provider claims conformance to that protocol;
4. `L0` never defines NAVER behavior;
5. `R0` may prove that measured runtime behavior differs from documentation, but such a conflict triggers review rather than silently rewriting the documented contract.

A conflict that affects a READY/safety invariant SHALL fail closed until reviewed.

### 2.2 No evidence laundering

The following are forbidden:

- turning a support anecdote into a universal provider guarantee;
- turning a portal screenshot/operator statement into machine-verified provider truth;
- turning a library default into a provider contract;
- turning a documentation retrieval timestamp into runtime verification;
- citing this `SOURCES.md` ledger as if it were the original upstream source.

Contract documents SHOULD refer to the original upstream URL and MAY additionally use the stable source ID from this ledger.

---

## 3. Provenance timestamps

The timestamp fields have separate meanings.

| Field | Meaning |
| --- | --- |
| `retrieved_at` / `last_checked` | ICBM inspected the source at that time |
| `upstream_version` | Provider documentation release/version associated with the reviewed contract |
| `review_due` | Fallback calendar bound if change detection is absent or broken |
| `verified_at` | Real runtime evidence was measured and accepted; documentation review alone cannot set this |

For the current M2 document set:

- `upstream_version = 2.88.0`
- `retrieved_at = 2026-09-14`
- `verified_at = null` until real M2 runtime acceptance succeeds
- `review_due = 2026-10-14` in the current contracts

A version/contract change takes precedence over the fallback calendar date.

`review_due` is not required to remain globally identical forever. A contract may adopt a stricter freshness bound. If it does, both the owning contract and the consistency matrix below must change together.

---

## 4. Cross-document provenance consistency matrix

This matrix mirrors the self-contained provenance values. It is an audit index, not their source of truth.

| Contract | Upstream version | Retrieved at | Verified at | Review due | Consistency status |
| --- | --- | --- | --- | --- | --- |
| `ACCOUNT_IDENTITY.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `AUTH.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `PERMISSIONS_SCOPES.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `ERRORS.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |
| `ENDPOINT_MATRIX.md` | `2.88.0` | `2026-09-14` | `null` | `2026-10-14` | `MATCHED` |

### 4.1 Drift rules

For the active M2 SmartStore contract set:

- every contract MUST expose machine-readable-or-parseable `upstream_version`, `retrieved_at`, `verified_at`, and `review_due` provenance values;
- `upstream_version` MUST match this ledger's current provider baseline unless a reviewed explicit version-scope exception is recorded;
- the value shown in this matrix MUST exactly mirror the owning contract;
- a targeted re-review MAY legitimately change `retrieved_at` or `review_due` for one contract, but the corresponding matrix row MUST change in the same reviewed change;
- `verified_at` MUST NOT become non-null merely because documentation or a support discussion was re-read;
- if one contract moves to a new provider version baseline, all affected active contracts must be impact-reviewed before the SmartStore integration set may be considered fully current.

### 4.2 Repository/CI enforcement target

This is a required repository invariant, but its implementation is **not part of this documentation-only PR**.

Claude Code implementation should extend `test_repository_rules.py` or an equivalent repository-rule test so CI verifies at minimum:

1. the active SmartStore contract set contains the required provenance fields;
2. each active contract's `upstream_version` equals the current baseline recorded by `SOURCES.md`, unless a reviewed exception exists;
3. each contract's provenance values exactly match its row in this consistency matrix;
4. a new active SmartStore contract cannot silently omit provenance;
5. a `SUPPORT_DISCUSSION` ledger row cannot omit its summarized observation and dependent contract list.

The test SHOULD parse the actual provenance representation used by each document rather than requiring all documents to have identical Markdown formatting.

The test MUST NOT impose a permanent rule that every contract's `review_due` is identical. A stricter contract-specific review window is valid when explicitly recorded.

Until this repository rule is implemented, reviewers MUST treat this matrix as a manual drift check.

---

## 5. Primary NAVER provider sources (`P0` / `OFFICIAL_DOC`)

| Source ID | Upstream source | Primary use | Dependent contract(s) | Freshness trigger |
| --- | --- | --- | --- | --- |
| `NAVER-P0-CURRENT` | https://apicenter.commerce.naver.com/docs/commerce-api/current | Current Commerce API version and API catalog | all SmartStore contracts | Commerce API version changes from `2.88.0` |
| `NAVER-P0-AUTH` | https://apicenter.commerce.naver.com/docs/auth | OAuth2 Client Credentials, token URL, Bearer auth, scopes N/A, auth fallback/signature rules | `AUTH.md`, `PERMISSIONS_SCOPES.md`, `ERRORS.md`, `ENDPOINT_MATRIX.md` | authentication/token/signature/scopes contract changes |
| `NAVER-P0-RESTRICTION` | https://apicenter.commerce.naver.com/docs/restriction | TLS constraints, API-group permission model, request limits | `PERMISSIONS_SCOPES.md`, `ERRORS.md` | API-group/TLS/rate-limit model changes |
| `NAVER-P0-TROUBLESHOOTING` | https://apicenter.commerce.naver.com/docs/trouble-shooting | gateway error format, Trace ID, gateway code table | `ERRORS.md` | gateway status/code/meaning changes |
| `NAVER-P0-REST` | https://apicenter.commerce.naver.com/docs/restful-api | common REST/request/response conventions | `ERRORS.md`, `ENDPOINT_MATRIX.md` | common REST/error contract changes |
| `NAVER-P0-TOKEN` | https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth | token issuance request/response endpoint contract | `AUTH.md`, `ENDPOINT_MATRIX.md` | method/path/request/response/token lifetime contract changes |
| `NAVER-P0-SELLER-ACCOUNT` | https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers | protected seller-account read, response identity fields, endpoint domain errors | `ACCOUNT_IDENTITY.md`, `PERMISSIONS_SCOPES.md`, `ENDPOINT_MATRIX.md` | method/path/response/error/permission mapping changes |
| `NAVER-P0-BASIC-INTEGRATION` | https://apicenter.commerce.naver.com/docs/solution-doc/3000/%EA%B8%B0%EB%B3%B8-%EC%97%B0%EB%8F%99-%EC%9A%94%EC%86%8C-%EA%B0%80%EC%9D%B4%EB%93%9C | solution/account mapping guidance, account UID integration context | `ACCOUNT_IDENTITY.md`, `AUTH.md` | account mapping or solution-integration guidance changes |
| `NAVER-P0-PRODUCT-CREATE` | https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product | product endpoint/error examples and future M5 planning | `PERMISSIONS_SCOPES.md`, `ERRORS.md`; M5 planning only | product create contract changes before M5 adoption |
| `NAVER-P0-PRODUCT-READ` | https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product | product read/error examples and future reconciliation planning | `ERRORS.md`; M5 planning only | product read contract changes before M5 adoption |

### 5.1 Current provider-version observation

At consolidation time, the official current Commerce API page reports:

`2.88.0 (2026-09-07)`

This is documentation provenance, not proof that ICBM runtime behavior has been verified against that version.

### 5.2 Current provider authentication observation

At consolidation time, the official authentication documentation states:

- OAuth 2.0 Client Credentials;
- token URL `https://api.commerce.naver.com/external/v1/oauth2/token`;
- protected requests use `Authorization: Bearer {token}`;
- Commerce API does not provide an OAuth `scopes` specification.

These claims remain subject to the contract-specific runtime acceptance requirements.

---

## 6. NAVER official technical-support sources (`P1` / `SUPPORT_DISCUSSION`)

These sources come from NAVER's official Commerce API technical-support repository/discussions.

They are useful for cases where the normative documentation is incomplete, ambiguous, or where observed failure modes require provider clarification.

They are **point-in-time support evidence, not immutable provider contracts**. A discussion may later be edited, hidden, deleted, or superseded.

They MUST remain supporting evidence rather than being silently promoted above newer normative documentation.

| Source ID | URL | Supported observation preserved by ICBM | Dependent contract(s) |
| --- | --- | --- | --- |
| `NAVER-P1-ACCOUNT-UID-2425` | https://github.com/commerce-api-naver/commerce-api/discussions/2425 | `accountUid`/`accountId` uniqueness guidance; `accountUid` integration use | `ACCOUNT_IDENTITY.md`, `ENDPOINT_MATRIX.md` |
| `NAVER-P1-SELF-ACCOUNT-3339` | https://github.com/commerce-api-naver/commerce-api/discussions/3339 | own-store `SELF`; token response does not itself provide seller identity | `AUTH.md` |
| `NAVER-P1-APP-REAUTH-3557` | https://github.com/commerce-api-naver/commerce-api/discussions/3557 | own-store application manual/web re-authentication; 180-day application validity guidance | `AUTH.md` |
| `NAVER-P1-SECRET-REISSUE-1564` | https://github.com/commerce-api-naver/commerce-api/discussions/1564 | reissued application secret invalidates prior secret | `AUTH.md` |
| `NAVER-P1-TIMESTAMP-357` | https://github.com/commerce-api-naver/commerce-api/discussions/357 | millisecond timestamp/window and clock synchronization guidance | `AUTH.md` |
| `NAVER-P1-SELF-BODY-3751` | https://github.com/commerce-api-naver/commerce-api/discussions/3751 | `SELF` request body rules; no `account_id`; client_credentials requirement | `AUTH.md`, `ENDPOINT_MATRIX.md` |
| `NAVER-P1-OWN-STORE-780` | https://github.com/commerce-api-naver/commerce-api/discussions/780 | own-store application/SELF integration context | `ENDPOINT_MATRIX.md` |
| `NAVER-P1-GW-AUTHN-GROUP-1013` | https://github.com/commerce-api-naver/commerce-api/discussions/1013 | `GW.AUTHN` can accompany missing API-group permission; provider-admin group inspection | `PERMISSIONS_SCOPES.md`, `ERRORS.md` |
| `NAVER-P1-PRODUCT-GROUP-1835` | https://github.com/commerce-api-naver/commerce-api/discussions/1835 | product API authorization tied to `상품` API group; observed `GW.AUTHN` case | `PERMISSIONS_SCOPES.md`, `ERRORS.md` |
| `NAVER-P1-SELLERINFO-GROUP-1895` | https://github.com/commerce-api-naver/commerce-api/discussions/1895 | seller-information calls require `판매자정보` group | `PERMISSIONS_SCOPES.md` |
| `NAVER-P1-ORDERSELLER-GROUP-1093` | https://github.com/commerce-api-naver/commerce-api/discussions/1093 | order-seller API group requirement | `PERMISSIONS_SCOPES.md` |
| `NAVER-P1-GW-AUTHN-HEADER-3676` | https://github.com/commerce-api-naver/commerce-api/discussions/3676 | malformed Authorization header can produce `GW.AUTHN` | `ERRORS.md` |
| `NAVER-P1-GW-AUTHN-EXPIRED-3762` | https://github.com/commerce-api-naver/commerce-api/discussions/3762 | expired access token can produce `401/GW.AUTHN` | `ERRORS.md` |
| `NAVER-P1-IP-TEMPORARY-3428` | https://github.com/commerce-api-naver/commerce-api/discussions/3428 | intermittent `GW.IP_NOT_ALLOWED` can reflect temporary provider-side behavior despite same outbound IP | `ERRORS.md` |
| `NAVER-P1-BADREQ-NOTICE-1649` | https://github.com/commerce-api-naver/commerce-api/discussions/1649 | structured `BAD_REQUEST`/`invalidInputs` example for missing product notice field | `ERRORS.md` |
| `NAVER-P1-BADREQ-POLICY-3529` | https://github.com/commerce-api-naver/commerce-api/discussions/3529 | `BAD_REQUEST` can also represent provider policy restriction, e.g. seller tag restriction | `ERRORS.md` |

### 6.1 Support-discussion survivability rule

A `SUPPORT_DISCUSSION` URL by itself is insufficient durable provenance.

Every P1 entry MUST preserve, in repository-controlled text:

- the limited observation actually relied upon;
- the owning/dependent contract(s);
- enough wording to understand the historical rationale if the external thread later becomes unavailable.

For an important P1 claim, the owning contract SHOULD also summarize the claim near its use instead of relying on a bare link.

The summary is an audit record of what ICBM relied upon at review time. It is **not** a substitute for current upstream evidence.

If a support discussion later disappears or becomes inaccessible:

- do not pretend the historical summary re-verifies current provider behavior;
- retain the summary as historical rationale;
- mark current freshness as needing review when that P1 evidence is materially required;
- seek current P0, replacement P1, or R0 evidence before making a new or expanded claim.

### 6.2 Support-source freshness

Support discussions do not share the Commerce API documentation version number.

For a `P1` source:

- `last_checked` defaults to this ledger's consolidation date unless the dependent contract records a newer check;
- any contradictory/newer `P0` provider contract invalidates the old interpretation immediately;
- a provider support reply MUST NOT be generalized beyond the facts it actually establishes;
- runtime behavior that conflicts with the support guidance triggers review rather than automatic remapping.

---

## 7. External normative standards (`S0` / `NORMATIVE_STANDARD`)

| Source ID | Source | Scope in ICBM | Dependent contract(s) | Freshness |
| --- | --- | --- | --- | --- |
| `OAUTH-S0-RFC6749` | https://www.rfc-editor.org/rfc/rfc6749 | OAuth 2.0 Client Credentials/access-token response/token-type semantics | `AUTH.md`, `ENDPOINT_MATRIX.md` | Re-review if provider changes OAuth protocol/version/profile |

The current M2 token contract uses RFC 6749 only for protocol semantics that NAVER explicitly adopts through OAuth 2.0 Client Credentials.

Notably, `token_type` is a required access-token response field and the client must not use an access token whose token type it does not understand.

ICBM currently understands/implements `Bearer` for SmartStore.

Therefore a non-Bearer token type is contract drift and fails closed rather than being silently forced into a Bearer request.

---

## 8. Conditional implementation-library sources (`L0` / `LIBRARY_REFERENCE`)

These sources become implementation-relevant only if the corresponding library is actually adopted/pinned by ICBM.

They do not define NAVER behavior.

| Source ID | Source | Conditional use | Current status |
| --- | --- | --- | --- |
| `HTTPX-L0-QUICKSTART` | https://www.python-httpx.org/quickstart/ | HTTPX redirect/default client behavior | `CONDITIONAL` |
| `HTTPX-L0-CLIENT-SOURCE` | https://github.com/encode/httpx/blob/master/httpx/_client.py | HTTPX redirect method/body behavior, including 307/308 | `CONDITIONAL` |

If HTTPX is adopted, these source references MUST be bound to the actual pinned version/commit used by ICBM rather than relying indefinitely on `master` or a moving documentation page.

`library behavior != provider contract`

---

## 9. Contract-to-source dependency matrix

This table is the fast audit view.

| Contract | Primary source IDs | Supporting source IDs | Runtime evidence required before `verified_at` |
| --- | --- | --- | --- |
| `ACCOUNT_IDENTITY.md` | `NAVER-P0-CURRENT`, `NAVER-P0-SELLER-ACCOUNT`, `NAVER-P0-BASIC-INTEGRATION` | `NAVER-P1-ACCOUNT-UID-2425` | real authenticated `/v1/seller/account` identity read and comparison |
| `AUTH.md` | `NAVER-P0-AUTH`, `NAVER-P0-TOKEN`, `NAVER-P0-BASIC-INTEGRATION`, `OAUTH-S0-RFC6749` | `NAVER-P1-SELF-ACCOUNT-3339`, `NAVER-P1-APP-REAUTH-3557`, `NAVER-P1-SECRET-REISSUE-1564`, `NAVER-P1-TIMESTAMP-357`, `NAVER-P1-SELF-BODY-3751` | real token lifecycle, restart/crash, generation, and re-auth behavior acceptance |
| `PERMISSIONS_SCOPES.md` | `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`, `NAVER-P0-CURRENT`, `NAVER-P0-PRODUCT-CREATE` | `NAVER-P1-GW-AUTHN-GROUP-1013`, `NAVER-P1-PRODUCT-GROUP-1835`, `NAVER-P1-SELLERINFO-GROUP-1895`, `NAVER-P1-ORDERSELLER-GROUP-1093` | current permission evidence with provenance; actual write remains separate M5 proof |
| `ERRORS.md` | `NAVER-P0-TROUBLESHOOTING`, `NAVER-P0-REST`, `NAVER-P0-AUTH`, `NAVER-P0-RESTRICTION`, `NAVER-P0-PRODUCT-CREATE`, `NAVER-P0-PRODUCT-READ` | `NAVER-P1-GW-AUTHN-GROUP-1013`, `NAVER-P1-PRODUCT-GROUP-1835`, `NAVER-P1-GW-AUTHN-HEADER-3676`, `NAVER-P1-GW-AUTHN-EXPIRED-3762`, `NAVER-P1-IP-TEMPORARY-3428`, `NAVER-P1-BADREQ-NOTICE-1649`, `NAVER-P1-BADREQ-POLICY-3529` | measured failure fixtures/real traces for adopted endpoints; unknown behavior remains UNKNOWN |
| `ENDPOINT_MATRIX.md` | `NAVER-P0-CURRENT`, `NAVER-P0-AUTH`, `NAVER-P0-TOKEN`, `NAVER-P0-SELLER-ACCOUNT`, `NAVER-P0-REST`, `OAUTH-S0-RFC6749` | `NAVER-P1-OWN-STORE-780`, `NAVER-P1-ACCOUNT-UID-2425`, `NAVER-P1-SELF-BODY-3751` | real M2 calls for the two ADOPTED endpoints and allow-list enforcement evidence |

A future `CAPABILITY_MAPPING.md` SHALL use these stable source IDs rather than creating another independent source taxonomy.

---

## 10. Runtime evidence registry (`R0` / `RUNTIME_EVIDENCE`) — currently pending

Runtime evidence is intentionally separate from documentation evidence.

The following M2 evidence slots exist but are not yet verified:

| Evidence ID | Target | Required minimum proof | Status |
| --- | --- | --- | --- |
| `SMARTSTORE-R0-TOKEN` | `SMARTSTORE_AUTH_TOKEN` | real authorized token call, request shape, response success predicate, observed latency/lifetime, sanitized trace reference | `PENDING` |
| `SMARTSTORE-R0-SELLER-ACCOUNT` | `SMARTSTORE_SELLER_ACCOUNT` | real protected GET, current session generation, observed `accountUid`, identity comparison, sanitized trace reference | `PENDING` |
| `SMARTSTORE-R0-FIRST-TOKEN-CRASH` | AUTH first-token uncertainty | measured behavior for remote token success followed by lost/uncommitted local result and bounded recovery | `PENDING` |
| `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW` | token persistence/reissue decision | behavior with >30 min remaining, <30 min remaining, restart, and old-token validity | `PENDING` |
| `SMARTSTORE-R0-PERMISSION` | permission evidence | provider-admin attestation or future official introspection bound to current application fingerprint; provenance/strength retained | `PENDING` |

`PENDING` is not failure.

It means the contract is documented but the corresponding runtime fact has not yet been measured and accepted.

When runtime evidence is collected, it MUST include generation/time/application bindings required by the owning contract.

---

## 11. Freshness and invalidation rules

### 11.1 Immediate review triggers

Re-review affected SmartStore contracts immediately when any of the following occurs:

- official Commerce API version changes from `2.88.0`;
- token endpoint method/path/request/response or lifetime contract changes;
- seller-account endpoint identity schema changes;
- NAVER changes `accountUid` / `accountId` guidance;
- OAuth/authentication mode or `SELF`/`SELLER` behavior changes;
- API-group permission model or endpoint/group mapping changes;
- gateway/domain error semantics change;
- redirect behavior or documented endpoint responses change;
- own-store application re-authentication policy changes;
- NAVER introduces a machine-readable permission introspection endpoint;
- an adopted implementation library changes behavior relied on by a safety invariant;
- measured runtime evidence contradicts a currently adopted contract.

### 11.2 Calendar fallback

If automated source/version change detection is absent or broken, the owning contract's `review_due` applies.

Calendar review is a fallback, not the primary change-detection mechanism.

### 11.3 Unknown beats invention

When a source is stale, unavailable, contradictory, or insufficient:

- do not infer a favorable provider guarantee;
- do not preserve READY merely because the previous documentation once said so;
- mark the affected evidence/state as stale or UNKNOWN according to the owning contract;
- require review or fresh measurement before restoring trust.

---

## 12. Adding a new source

A new SmartStore source entry MUST record at least:

- stable `source_id`;
- source class/authority (`P0`, `P1`, `S0`, `L0`, or `R0`);
- artifact kind (`OFFICIAL_DOC`, `SUPPORT_DISCUSSION`, `NORMATIVE_STANDARD`, `LIBRARY_REFERENCE`, or `RUNTIME_EVIDENCE`);
- canonical URL or evidence locator;
- exact claim(s) it supports;
- dependent contract(s);
- retrieval/measurement time;
- upstream version when applicable;
- freshness/review trigger;
- whether it is documentation, operator attestation, or measured runtime evidence.

Before using a new source to change a contract:

`source -> claim -> impacted contract -> tests/evidence -> review`

A source addition alone does not authorize implementation behavior.

---

## 13. Source removal / supersession

Do not silently delete a source that previously supported an adopted contract.

When a source becomes obsolete, inaccessible, edited incompatibly, or contradicted:

1. mark it superseded/unavailable in the ledger or remove it only in the same reviewed change that records the replacement;
2. identify every dependent contract;
3. re-evaluate affected invariants;
4. update tests/evidence requirements;
5. preserve enough repository-controlled summary/history to explain why the prior decision existed and why it changed.

Historical support evidence may remain in the ledger when useful for audit, but MUST be clearly distinguished from current normative truth.

A cached summary of an unavailable discussion is historical rationale, not current provider verification.

---

## 14. Final provenance invariants

`self-contained contract provenance remains in each owning document`

`SOURCES.md = reverse index + consistency audit, not single provenance source`

`provider documentation != runtime verification`

`support discussion != universal provider guarantee`

`support URL alone != durable rationale`

`operator attestation != machine verification`

`library behavior != provider behavior`

`retrieved_at != verified_at`

`cross-document provenance drift must be detectable and later CI-enforced`

`source freshness failure -> re-review / UNKNOWN, not invented certainty`

`runtime contradiction -> investigate and fail closed before rewriting canonical truth`
