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

---

## 5. First binding

An account with no canonical provider identity MUST NOT silently become trusted merely because an authentication attempt succeeded.

A first binding must establish `provider_account_uid` through an explicit account-linking flow.

The authenticated protected-read response is the evidence used for the binding.

After the binding is persisted, subsequent authentication must compare the observed value against that canonical value.

Store name or other weak display data MUST NOT be used as a substitute when `accountUid` is unavailable.

---

## 6. Weak identity fields

The following MAY be stored for display, diagnostics, or corroboration but MUST NOT independently prove account identity:

- store/channel name
- representative channel name
- channel URL
- representative image
- business display information
- category
- seller grade

These values may change while the underlying marketplace account remains the same.

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

If required runtime proof is absent, expired, corrupt, or mismatched, the state must converge away from READY.

Examples:

### Valid persisted identity + valid authenticated read

`AUTH_READY`

### Persisted READY + unavailable/corrupt authentication proof

non-READY

### Persisted READY + authenticated account differs

`AUTH_MISMATCH -> REVIEW_REQUIRED`

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
- no automatic rebinding.

### Partial failure

- authentication succeeds;
- protected account read fails or has unusable identity data;
- AUTH MUST remain non-READY.

### Crash/restart

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

`GET /v1/seller/account + accountUid match = account identity proof`

subject to successful real-world runtime verification.
