# SmartStore Account Identity Contract

## Status

- Provider: NAVER SmartStore / Commerce API
- Contract status: `DOCUMENTED_SUPPORTED`
- Runtime verification: `PENDING`
- Identity proof method: authenticated non-mutating protected read
- Primary remote identity: `accountUid`
- Secondary remote identity: `accountId`

## Provenance

- source_url:
  - https://apicenter.commerce.naver.com/docs/commerce-api/current
  - https://github.com/commerce-api-naver/commerce-api/discussions/2425
  - https://apicenter.commerce.naver.com/docs/solution-doc/3000/%EA%B8%B0%EB%B3%B8-%EC%97%B0%EB%8F%99-%EC%9A%94%EC%86%8C-%EA%B0%80%EC%9D%B4%EB%93%9C
- upstream_version: `2.88.0`
- retrieved_at: `2026-09-14`
- verified_at: `null`
- review_due: `2026-10-14`
- review_trigger: re-review immediately if the documented upstream version changes from `2.88.0`, the `/v1/seller/account` contract changes, or NAVER changes guidance for `accountUid` / `accountId` identity semantics.

`review_due` is a fallback calendar freshness bound for periods where automated upstream-change detection is not available. An upstream-version or contract change takes precedence and triggers review earlier.

`verified_at` MUST remain unset until ICBM performs a real authenticated API call against an authorized SmartStore account and records the measured response.

Reading official documentation is not runtime verification.

---

## 1. Question

Can ICBM reliably identify the seller account represented by the currently authenticated SmartStore API session using a non-mutating authenticated API response?

### Decision

**YES — documented capability exists.**

SmartStore provides an authenticated, non-mutating seller account read:

`GET /v1/seller/account`

The endpoint returns information for the seller account associated with the authenticated request.

The response contains provider account identifiers including:

- `accountUid`
- `accountId`

NAVER technical support states that both identify a SmartStore uniquely and that `accountUid` is specifically provided for Commerce API account integration.

Therefore ICBM SHALL use `accountUid` as the primary provider identity used for authenticated account proof.

---

## 2. Canonical terminology

ICBM MUST NOT overload the name `account_id` across internal and provider concepts.

Recommended terminology:

- `marketplace_account_id`
  - ICBM-owned canonical MarketplaceAccount identifier.
  - MUST NOT depend on a provider-visible store name.

- `provider_account_uid`
  - NAVER `accountUid`.
  - Primary external identity proof field.

- `provider_account_id`
  - NAVER `accountId`.
  - Secondary/corroborating external identifier.

- `account_id`
  - Reserved for the NAVER OAuth/token protocol parameter when referring to the external API wire contract.

Store name, channel name, representative channel name, URL, business display name, and other human-readable attributes MUST NOT be promoted to canonical account identity.

---

## 3. Authentication proof

Token issuance by itself is NOT sufficient proof of account identity.

Successful authentication means only that NAVER accepted the authentication request.

ICBM auth capability SHALL become READY only when all required conditions hold:

1. valid authentication material exists;
2. authentication/token acquisition succeeds;
3. `GET /v1/seller/account` succeeds as an authenticated protected read;
4. the response contains the required identity field;
5. observed `accountUid` equals the configured expected `provider_account_uid`;
6. the evidence belongs to the current authentication/session generation.

Formally:

`AUTH_READY = authenticated_read_success && identity_present && identity_match`

A persisted state string of `READY` does not override these invariants.

---

## 4. Identity mismatch

If:

`observed.accountUid != expected.provider_account_uid`

then:

- auth MUST NOT become READY;
- previous READY MUST be invalidated;
- state MUST converge to `AUTH_MISMATCH`;
- human review MUST be required;
- ICBM MUST NOT silently replace the configured identity;
- ICBM MUST NOT automatically switch to or bind the newly observed account.

Result:

`AUTH_MISMATCH -> REVIEW_REQUIRED`

At mismatch-detection time ICBM MUST NOT claim to know whether the cause is:

1. authentication against a different SmartStore account; or
2. a provider-side identity change affecting what appears to be the same business/store.

Those causes are operationally different but are not safely distinguishable from `accountUid` inequality alone.

The review surface SHOULD therefore show expected and observed identity evidence side-by-side, for example:

- expected `provider_account_uid` and `provider_account_id` when available;
- observed `accountUid` and `accountId`;
- weak corroborating fields such as store/channel name and channel URL for both sides when available;
- timestamp and evidence/session generation for the observation.

Weak corroborating fields are review context only. A visual or textual match in those fields MUST NOT automatically authorize rebinding.

Any rebinding after `AUTH_MISMATCH` requires an explicit human action and a fresh authenticated protected read. The old and new provider identities and the evidence used for the decision SHOULD be retained for audit.

---

## 5. First binding

An account with no canonical provider identity MUST NOT silently become trusted merely because an authentication attempt succeeded.

A first binding must establish `provider_account_uid` through an explicit account-linking flow.

The authenticated protected-read response is the evidence used for the binding.

After the binding is persisted, subsequent authentication must compare the observed value against that canonical value.

Store name or other weak display data MUST NOT be used as a substitute when `accountUid` is unavailable.

### First-binding atomicity

First binding is a state transition, not a collection of independently trustworthy field writes.

The canonical binding unit MUST include enough data to prove one completed account association, including at minimum:

- `marketplace_account_id`;
- `provider_account_uid`;
- `provider_account_id` when available;
- the evidence/session generation used for the binding;
- binding completion state or equivalent transaction proof.

ICBM MUST persist that binding atomically, or provide an equivalent transaction mechanism with the same externally observable guarantee.

If a crash or partial failure occurs before the completed binding commit can be proven, the account MUST be treated logically as `NOT_BOUND` after restart, even if one or more partial fields are physically present.

An incomplete or orphaned binding record MUST NOT be sufficient for `AUTH_READY` and MUST NOT be silently completed from stale evidence.

Recovery MAY quarantine, clean up, or overwrite incomplete storage as an implementation detail, but the safety invariant is:

`binding_commit_not_proven -> NOT_BOUND`

A retry must obtain current authenticated identity evidence again before establishing the binding.

---

## 6. Weak identity fields

The following MAY be stored for display, diagnostics, corroboration, and human mismatch review but MUST NOT independently prove account identity:

- store/channel name
- representative channel name
- channel URL
- representative image
- business display information
- category
- seller grade

These values may change while the underlying marketplace account remains the same.

Their intended use in `AUTH_MISMATCH` is to help a human understand whether the observed account appears operationally related to the expected account. They MUST NOT be promoted into a canonical identity key and MUST NOT drive automatic rebinding or READY convergence.

---

## 7. `accountId` vs `accountUid`

NAVER exposes both `accountId` and `accountUid`.

Current official support guidance indicates:

- both are unique to a SmartStore;
- `accountUid` is separately provided for Commerce API integration and seller-account mapping.

Therefore:

`accountUid` = primary ICBM remote identity

`accountId` = secondary/corroborating identity

ICBM MUST NOT assume that either value is permanently immutable unless NAVER explicitly guarantees that property.

If an already bound identity changes unexpectedly, fail closed and require review instead of automatic migration.

---

## 8. Restart and state convergence

Persisted auth state is not truth.

After restart, ICBM MUST NOT trust `AUTH_READY` merely because it was previously stored.

The system must verify that the evidence required by the current session/account contract is still valid.

If required runtime proof is absent, expired, corrupt, mismatched, or belongs to an incomplete first binding, the state must converge away from READY.

Examples:

### Valid persisted identity + valid authenticated read

`AUTH_READY`

### Persisted READY + unavailable/corrupt authentication proof

non-READY

### Persisted READY + authenticated account differs

`AUTH_MISMATCH -> REVIEW_REQUIRED`

### Incomplete first binding after restart

`NOT_BOUND`

### Authentication temporarily unavailable

apply bounded retry policy; do not preserve READY solely from persistence.

---

## 9. Acceptance requirements

M2 account-identity acceptance MUST include at least:

### Happy path

- acquire valid authentication;
- call `GET /v1/seller/account`;
- capture `accountUid`;
- compare with expected canonical provider identity;
- match;
- AUTH becomes READY.

### Identity mismatch

- configured `provider_account_uid = A`;
- authenticated response reports `accountUid = B`;
- AUTH MUST NOT become READY;
- result MUST be `AUTH_MISMATCH`;
- no automatic rebinding;
- expected and observed strong identity values are available to the review flow;
- available weak corroborating fields are presented as review context only;
- matching weak fields MUST NOT auto-resolve the mismatch.

### Partial failure

- authentication succeeds;
- protected account read fails or has unusable identity data;
- AUTH MUST remain non-READY.

### First binding success

- account starts with no canonical provider binding;
- current authenticated protected read returns usable `accountUid`;
- explicit binding action completes;
- the binding unit is committed atomically;
- restart preserves one complete binding;
- later auth proof compares against that committed identity.

### First binding crash/restart

Acceptance MUST simulate failure at least after identity read but before completed binding commit, and SHOULD also simulate any implementation-specific partial-write boundary.

After restart:

- an incomplete binding MUST be treated as `NOT_BOUND`;
- no partial `provider_account_uid` or other orphan field may establish trust;
- AUTH MUST NOT become READY from the incomplete binding;
- retry MUST use fresh authenticated identity evidence.

### Auth proof crash/restart

- persist state;
- restart;
- remove/corrupt/expire required authentication evidence;
- previously persisted READY MUST NOT alone re-establish READY.

### State mismatch convergence

- persisted state says READY;
- measured external/account identity invariant disagrees;
- measured invariant wins;
- state converges fail-closed.

---

## 10. Runtime verification still required

This document establishes that the capability is supported by current upstream documentation.

It does NOT yet establish actual runtime compatibility for ICBM.

Before SmartStore auth acceptance is complete, ICBM MUST perform a real non-mutating authenticated call and record:

- HTTP method/path
- HTTP status
- response identity fields used for comparison
- expected identity
- observed identity
- comparison result
- timestamp
- application/account mode
- sanitized trace/evidence reference

Only after this measured runtime evidence succeeds may `verified_at` be populated.

---

## 11. Final contract

For SmartStore:

`authentication success != account identity proof`

`token issuance success != AUTH_READY`

`store name match != account identity proof`

`incomplete first binding != trusted binding`

`GET /v1/seller/account + accountUid match = account identity proof`

subject to successful real-world runtime verification.
