# SmartStore Endpoint Matrix

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M2_CONNECT_ADOPTED_SET_FROZEN_FOR_REVIEW` |
| M2 integration mode | `OWN_STORE_SELF` |
| Adopted endpoint count | `2` |
| M5 endpoints | `NOT_ADOPTED` |
| Runtime verification | `PENDING` |
| Upstream version | `2.88.0` |
| Retrieved at | `2026-09-14` |
| Verified at | `null` |
| Review due | `2026-10-14` |

## Provenance

Primary upstream documentation:

- https://apicenter.commerce.naver.com/docs/commerce-api/current
- https://apicenter.commerce.naver.com/docs/auth
- https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth
- https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers
- https://apicenter.commerce.naver.com/docs/restful-api
- https://www.rfc-editor.org/rfc/rfc6749
- https://www.rfc-editor.org/rfc/rfc6750

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/780
  - `내스토어 애플리케이션` uses `type=SELF` only.
  - one own-store application is connected to one SmartStore account.
- https://github.com/commerce-api-naver/commerce-api/discussions/2425
  - `accountUid` and `accountId` identify a SmartStore uniquely under current provider guidance.
  - `accountUid` is the Commerce API integration-oriented identifier.
- https://github.com/commerce-api-naver/commerce-api/discussions/3751
  - own-store applications use `type=SELF`.
  - `account_id` MUST NOT be included for `type=SELF`.
  - token payload fields must be sent in `application/x-www-form-urlencoded` body format.

Freshness:

- Upstream version, endpoint path/method, auth mode, response schema, permission mapping, redirect behavior, or application-mode changes trigger immediate review.
- `review_due` is a fallback when automated upstream-change detection is absent or broken.
- Documentation inspection does not populate `verified_at`.

---

## 1. Purpose

This file is the SmartStore endpoint allow-list adopted by ICBM.

It is not a catalog of every NAVER endpoint.

Core rule:

`documented by provider != adopted by ICBM`

Only `ADOPTED` endpoints may be reachable from the applicable production integration path.

`NOT_ADOPTED` means no network call, even when the provider documents the endpoint and its URL is known.

For M2 CONNECT the adopted network surface is deliberately limited to:

1. token issuance/reissuance;
2. protected seller-account identity read.

Product registration, images, categories, attributes, options, and product read-back belong to later milestones.

---

## 2. Adoption states

| State | Meaning | Runtime rule |
| --- | --- | --- |
| `ADOPTED` | Required contract fields are frozen for the current milestone. | Callable only through the adopted endpoint contract. |
| `NOT_ADOPTED` | Known/planned endpoint whose operational contract is not frozen. | Must fail locally before network I/O. |

Changing `NOT_ADOPTED -> ADOPTED` requires review of method/path, app-mode eligibility, required groups, timeout policy, success predicate, error contract, redirects, and — for writes — idempotency/read-back/reconciliation.

---

## 3. Base URL

Provider base URL:

`https://api.commerce.naver.com/external`

Documented endpoint paths are modeled separately from the base URL.

| Field | Example |
| --- | --- |
| `base_url` | `https://api.commerce.naver.com/external` |
| `path_relative_to_base_url` | `/v1/seller/account` |
| wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |

Every registry `Path` value in this document is relative to `base_url` unless explicitly stated otherwise.

ICBM MUST NOT maintain competing base-prefix logic that can omit `/external` or produce `/external/external`.

---

## 4. Master registry

| Endpoint ID | Adoption | Milestone | Method | Path (relative to `base_url`) | Purpose | App mode | Required group | Mutation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | `ADOPTED` | M2 | `POST` | `/v1/oauth2/token` | Issue/reissue bearer token | `OWN_STORE_SELF` | `N/A` | No marketplace resource mutation |
| `SMARTSTORE_SELLER_ACCOUNT` | `ADOPTED` | M2 | `GET` | `/v1/seller/account` | Account identity proof | `OWN_STORE_SELF` | `판매자정보` | No |
| `SMARTSTORE_PRODUCT_CREATE_V2` | `NOT_ADOPTED` | M5 candidate | `POST` | `/v2/products` | Product CREATE | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | Yes |
| `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v2/products/origin-products/{originProductNo}` | Origin-product read-back candidate | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v2/products/channel-products/{channelProductNo}` | Channel-product read-back candidate | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` | `NOT_ADOPTED` | M5 candidate | `POST` | `/v1/product-images/upload` | Image upload | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | Side effect; not frozen |
| `SMARTSTORE_CATEGORY_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories` | Category discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_CATEGORY_READ` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories/{categoryId}` | Category validation | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attributes` | Attribute discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attribute-values` | Attribute-value discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_STANDARD_OPTIONS` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/options/standard-options` | Standard-option discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_NOTICE_TYPES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/products-for-provided-notice` | Notice-type discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |

The M5 list is planning metadata only. Presence does not imply eventual adoption.

---

## 5. M2 CONNECT graph

The complete M2 network sequence is:

`SMARTSTORE_AUTH_TOKEN`

`-> durable session commit per AUTH.md`

`-> SMARTSTORE_SELLER_ACCOUNT`

`-> identity comparison/binding per ACCOUNT_IDENTITY.md`

`-> AUTH_READY candidate`

No product endpoint is part of this graph.

### M2 adopted permission union

`required_groups(SMARTSTORE_AUTH_TOKEN) = {}`

`required_groups(SMARTSTORE_SELLER_ACCOUNT) = {판매자정보}`

Therefore:

`M2_ADOPTED_ENDPOINT_GROUP_UNION = {판매자정보}`

`PERMISSIONS_SCOPES.md` separately tracks `상품` as the planned product-write permission baseline.

That does **not** make a product endpoint part of M2.

`상품 permission observed != product endpoint adopted`

`상품 permission observed != product write verified`

M2 MUST NOT issue a product request merely to discover or reconfirm product permission.

M2 `write_scope` is populated, when evidence exists, from the non-network provider-admin attestation/introspection contract in `PERMISSIONS_SCOPES.md`; `ENDPOINT_MATRIX.md` does not add a product network call for that purpose.

---

## 6. `SMARTSTORE_AUTH_TOKEN`

### Contract

| Field | Value |
| --- | --- |
| Adoption | `ADOPTED` |
| Method | `POST` |
| Path relative to `base_url` | `/v1/oauth2/token` |
| Wire URL | `https://api.commerce.naver.com/external/v1/oauth2/token` |
| App mode | `OWN_STORE_SELF` |
| Grant | Client Credentials |
| Content-Type | `application/x-www-form-urlencoded` |
| Bearer required | No |
| Required API group | `N/A` |
| Connect timeout | `5s` |
| Read timeout | `30s` |
| Redirect policy | `NO_FOLLOW` |
| Lifecycle owner | `AUTH.md` |

The timeout values are ICBM M2 policy, not provider guarantees. They are intentionally asymmetric with the seller-account read because an avoidably short token read timeout creates expensive issuance uncertainty. Runtime acceptance SHALL record observed latency and may propose a reviewed policy change; implementation MUST NOT silently substitute arbitrary library defaults.

### Request predicate

| Field | M2 rule |
| --- | --- |
| `client_id` | Required; current own-store application ID |
| `timestamp` | Required; generated under `AUTH.md` |
| `client_secret_sign` | Required; generated from the current credential generation |
| `grant_type` | Exact `client_credentials` |
| `type` | Exact `SELF` |
| `account_id` | **Forbidden** for `OWN_STORE_SELF` |

The required fields belong in the form body. Query-string substitution is not accepted by the ICBM contract.

A locally detectable violation should fail before network I/O.

### Success predicate

`token_endpoint_success =`

`HTTP 200`

`AND JSON object parses`

`AND access_token is non-empty string`

`AND expires_in is positive integer`

`AND token_type equals Bearer case-insensitively`

`token_type` remains part of the success predicate deliberately.

OAuth 2.0 defines `token_type` as a required token-response field and states that a client MUST NOT use an access token when it does not understand the token type. ICBM M2 implements the Bearer token scheme only, and NAVER documents protected calls as `Authorization: Bearer {access_token}`. Therefore a non-Bearer token type is provider/contract drift, not a warning that ICBM may ignore.

Case changes such as `bearer` vs `Bearer` are accepted by the case-insensitive comparison.

If a 200 response fails this predicate:

- semantic success is false;
- `session_generation` MUST NOT advance;
- evidence is handled as schema/provider-contract drift under `ERRORS.md`.

Token endpoint success does **not** prove account identity or `AUTH_READY`.

### Session boundary

A successful response creates a token candidate only.

`token_endpoint_success != session_generation_committed`

The new session generation exists only after the complete local session bundle is durably committed under `AUTH.md`.

### Current provider token behavior

Current documentation states:

- default lifetime: 10,800 seconds / 180 minutes;
- at least 30 minutes remaining: existing token may be returned;
- fewer than 30 minutes remaining: new token may be issued;
- an older token remains usable until its own expiry after a new token is issued.

These facts do not resolve the `remote success + local commit unknown` crash case; that remains owned by `AUTH.md` and runtime measurement.

### Documented endpoint-level responses

| HTTP | Provider-level description | Handling |
| --- | --- | --- |
| `200` | Token issued/reissued | Must pass success predicate |
| `400` | Validation error | Evidence-based diagnosis; not automatic credential-invalid truth |
| `403` | Access/authorization error | Diagnose application/lifecycle/auth evidence |
| `500` | Temporary internal system error | Bounded recovery only |

Transport/gateway failures may also occur.

### Redirect

The adopted endpoint contract does not list redirect as a normal result.

`redirect_policy = NO_FOLLOW`

Unexpected redirect is contract/error evidence and MUST NOT be followed by a generic client.

### Retry

This POST is an authentication-control operation, not a marketplace CREATE.

Retry/reissue semantics are owned by `AUTH.md`, including bounded issuance, atomic session commit, lifecycle rules, and first-token crash uncertainty.

A read timeout after the request may have reached NAVER does not prove token issuance failed. It remains an authentication-session uncertainty owned by `AUTH.md`; it MUST NOT be translated into user re-approval without evidence.

---

## 7. `SMARTSTORE_SELLER_ACCOUNT`

### Contract

| Field | Value |
| --- | --- |
| Adoption | `ADOPTED` |
| Method | `GET` |
| Path relative to `base_url` | `/v1/seller/account` |
| Wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |
| App mode | `OWN_STORE_SELF` |
| Authentication | `Authorization: Bearer {access_token}` |
| Required API group | `판매자정보` |
| Mutation | No |
| Connect timeout | `5s` |
| Read timeout | `10s` |
| Redirect policy | `NO_FOLLOW` |
| Identity owner | `ACCOUNT_IDENTITY.md` |

The timeout values are ICBM M2 policy, not provider guarantees. This read may use the shorter read timeout because it is non-mutating and already has bounded retry/re-auth rules. Runtime acceptance SHALL measure observed latency before any future timeout-policy change.

### Request predicate

The request MUST:

- use `GET` on the exact adopted path;
- use the bearer token from the current committed `session_generation`;
- preserve credential/session-generation linkage in evidence;
- contain no product/business mutation payload.

A token candidate that was never durably committed MUST NOT produce durable account-proof evidence.

### Endpoint success predicate

`accountUid` is the strong identity field required by `ACCOUNT_IDENTITY.md`.

`accountId` is secondary/corroborating and is recorded when available.

Therefore:

`seller_account_endpoint_success =`

`HTTP 200`

`AND JSON object parses`

`AND accountUid is non-empty string`

A missing optional/secondary `accountId` does not by itself manufacture failure of the primary identity proof path; it is preserved as schema/evidence context and reviewed if provider behavior diverges from the documented response contract.

This distinction keeps the matrix aligned with:

`provider_account_uid = primary identity`

`provider_account_id = secondary identity when available`

### Bound-account identity predicate

For an already bound MarketplaceAccount:

`account_identity_proven = seller_account_endpoint_success`

`AND observed.accountUid == expected.provider_account_uid`

A mismatch remains:

`AUTH_MISMATCH -> REVIEW_REQUIRED`

Weak fields and `accountId` correlation MUST NOT override an `accountUid` mismatch.

### First binding

For an unbound account:

- endpoint success supplies current authenticated identity evidence;
- explicit first binding is still required;
- the binding unit is committed atomically under `ACCOUNT_IDENTITY.md`;
- successful read alone does not silently bind the account.

### AUTH_READY effect

For an already bound account:

`AUTH_READY =`

`current AUTH.md invariants hold`

`AND seller_account_endpoint_success`

`AND accountUid match`

`AND evidence belongs to current credential/session generations`

Token success by itself remains insufficient.

### Permission group

This endpoint requires `판매자정보` under the adopted M2 contract.

Consequences:

- if the account-read proof path cannot be completed, token issuance alone cannot establish `AUTH_READY`;
- this failure MUST NOT automatically become product `write_scope=MISSING`;
- `상품` is not part of the M2 network endpoint union.

### Documented domain error surface

| HTTP | Provider code | Meaning | Matrix rule |
| --- | --- | --- | --- |
| `400` | `GENERAL_ERROR` | Unhandled error | Use `ERRORS.md`; do not assume input validation |
| `401` | `UNAUTHORIZED` | Access authority unavailable | Diagnose auth/permission/context; no unconditional mapping |
| `403` | `ROLE_NOT_FOUND` | Role missing | Provider condition evidence |
| `403` | `PROVISION_NOT_FOUND` | Terms/agreement required | Provider condition; not credential invalidity |
| `403` | `INVALID_CHANNEL_STATUS` | Invalid channel state | Provider/account state evidence |
| `403` | `INVALID_STORE_STATUS` | Invalid store state | Provider/account state evidence |
| `403` | `INVALID_REPRESENT_STATUS` | Invalid representative state | Provider/account state evidence |
| `403` | `INVALID_MEMBER_STATUS` | Invalid member state | Provider/account state evidence |
| `403` | `INVALID_INTERLOCK_STATUS` | Invalid integration state | Provider/integration evidence |
| `403` | `RESOURCE_NOT_AVAILABLE` | Resource unavailable | Diagnose before canonical classification |
| `404` | `CHANNEL_NOT_FOUND` | Channel absent | No global NOT_FOUND mapping |
| `404` | `STORE_NOT_FOUND` | Store absent | No global NOT_FOUND mapping |
| `404` | `REPRESENT_NOT_FOUND` | Representative absent | No global NOT_FOUND mapping |
| `404` | `MEMBER_NOT_FOUND` | Member absent | No global NOT_FOUND mapping |
| `404` | `INTERLOCK_NOT_FOUND` | Interlock data absent | No global NOT_FOUND mapping |
| `500` | `PARSING_FAIL` | JSON parsing failure | Evidence-based classification |
| `500` | `SERDES_FAIL` | Serialization/deserialization failure | Evidence-based classification |
| `500` | `ENCDEC_FAIL` | Encryption/decryption failure | Evidence-based classification |
| `500` | `GENERAL_ERROR` | Unhandled error | Candidate transient/unknown depending evidence |

These are endpoint evidence inputs, not new canonical error classes.

### Redirect

The endpoint's adopted response set does not include redirect.

`redirect_policy = NO_FOLLOW`

### Retry

The endpoint is read-only.

Retry follows `ERRORS.md` and `AUTH.md`:

- bounded transient retry;
- bounded auth recovery only when AUTH cause is positively established;
- no hot loop;
- `UNKNOWN` does not become credential invalidity;
- mismatch never causes automatic account switching.

---

## 8. M2 completion contract

For an already bound account:

| Step | Required result |
| --- | --- |
| 1 | `SMARTSTORE_AUTH_TOKEN` success predicate passes |
| 2 | token/session bundle is durably committed as current `session_generation` |
| 3 | `SMARTSTORE_SELLER_ACCOUNT` success predicate passes using that generation |
| 4 | observed `accountUid` matches canonical `provider_account_uid` |
| 5 | `auth=READY` may be established |

For first binding, step 4 is replaced by the explicit atomic binding flow.

Product capability remains separate:

`auth READY != write_scope READY != write READY`

No M5 endpoint may be called to complete M2 CONNECT.

---

## 9. Mandatory success-predicate gate

Every `ADOPTED` endpoint MUST have a machine-checkable success predicate.

For **every** response, including HTTP 2xx:

`receive`

`-> parse endpoint contract`

`-> evaluate success_predicate`

`-> only then declare semantic success`

An endpoint without a frozen success predicate is not eligible for automated execution.

This closes the `ERRORS.md` OPERATION_RESULT gap: HTTP success is never the final success decision by itself.

---

## 10. Timeout policy

Timeouts are endpoint-contract fields, not incidental HTTP-library defaults.

M2 baseline:

| Endpoint ID | Connect timeout | Read timeout | Reason |
| --- | ---: | ---: | --- |
| `SMARTSTORE_AUTH_TOKEN` | `5s` | `30s` | Favor avoiding avoidable token-issuance uncertainty. |
| `SMARTSTORE_SELLER_ACCOUNT` | `5s` | `10s` | Read-only operation with bounded retry/re-auth paths. |

Rules:

- timeout expiry is classified under `ERRORS.md`/`AUTH.md`; it is not proof of provider rejection;
- token timeout after possible transmission MUST preserve issuance uncertainty rather than invent credential failure;
- values are reviewed from measured M2 latency evidence, not silently tuned per call site;
- implementation SHALL expose the endpoint policy to tests so accidental fallback to client defaults is detectable.

---

## 11. Redirect policy

Generic automatic redirect following is forbidden for SmartStore integration clients unless an adopted endpoint explicitly permits it.

Both M2 endpoints are:

`NO_FOLLOW`

Future redirect adoption must freeze:

- accepted status;
- allowed target host/path;
- method/body behavior;
- credential-forwarding behavior;
- replay/idempotency safety.

This is mandatory before any mutating endpoint with possible 308 behavior can become `ADOPTED`.

---

## 12. M5 rows are intentionally incomplete

M5 candidate rows do not freeze:

- required groups;
- app-mode eligibility;
- timeout policy;
- success predicate;
- domain errors;
- redirect behavior;
- idempotency;
- read-back identity;
- consistency window;
- `remote_outcome` reconciliation;
- retry budget;
- cleanup behavior.

Those values are filled only when M5 actually adopts the endpoint.

No code may substitute guessed defaults for `TBD_AT_ADOPTION`.

---

## 13. Static/repository enforcement

M2 implementation SHALL make the matrix enforceable, not documentary only.

The SmartStore integration layer MUST NOT directly own a raw/general-purpose HTTP client in adapters, services, or feature code.

All provider network calls MUST pass through one registry-gated SmartStore endpoint caller (for example, an abstraction equivalent to `caller.call(endpoint_id, request_data)`) that:

- resolves only an `ADOPTED` endpoint ID;
- owns/receives the raw HTTP transport internally;
- composes `base_url + path_relative_to_base_url` exactly once;
- applies the endpoint method, timeout, redirect, auth, and success-predicate policy;
- rejects `NOT_ADOPTED` before transport handoff.

The illustrative class/function name above is not frozen; the ownership boundary is.

SmartStore feature/adaptor code MUST NOT receive a raw HTTP client that permits arbitrary URL construction or direct `get/post/request` calls around the registry.

Acceptance must prove at least:

1. the M2 SmartStore runtime registry exposes exactly the two `ADOPTED` endpoint IDs;
2. `NOT_ADOPTED` resolution fails locally before network I/O;
3. base URL/method/path come from one provider endpoint contract, not duplicated raw strings;
4. token requests enforce form encoding, `type=SELF`, and absent `account_id`;
5. account reads require the current committed bearer session;
6. endpoint-specific connect/read timeouts are applied and library defaults cannot silently replace them;
7. generic auto-redirect is disabled;
8. every adopted endpoint has a success predicate;
9. malformed 2xx responses fail closed;
10. evidence records contain the endpoint ID and relevant generation IDs;
11. static/repository tests reject SmartStore adapter/service code that constructs or owns a raw HTTP client outside the approved registry-gated caller boundary;
12. raw SmartStore URL literals and dynamic URL assembly outside the approved caller boundary are rejected to the extent enforceable by repository checks, with the raw-client ownership rule as the primary control rather than string matching alone.

The implementation mechanism is not frozen here; the observable ownership and call-path invariants are.

---

## 14. M2 acceptance requirements

### 14.1 Allow-list

Runtime M2 endpoint set must be exactly:

- `SMARTSTORE_AUTH_TOKEN`
- `SMARTSTORE_SELLER_ACCOUNT`

No M5 candidate call is allowed.

### 14.2 Token request

Verify:

- POST `/external/v1/oauth2/token`;
- form-urlencoded body;
- required fields present;
- `grant_type=client_credentials`;
- `type=SELF`;
- `account_id` absent;
- no credentials/signatures leak to logs.

### 14.3 Token timeout policy

Verify:

- connect timeout is `5s`;
- read timeout is `30s`;
- a post-transmission read timeout does not get rewritten as credential invalidity or automatic user re-approval;
- timeout evidence is preserved for `AUTH.md` recovery/measurement.

### 14.4 Token success predicate

Fixtures must include:

- valid Bearer token body -> pass;
- 200 missing/empty `access_token` -> fail;
- nonpositive/invalid `expires_in` -> fail;
- case variants of `Bearer` -> pass;
- unsupported/non-Bearer `token_type` -> fail closed as token-type/contract drift;
- predicate failure does not advance `session_generation`.

### 14.5 Session boundary

Provider 200 alone does not make the session current.

Only durable local commit may advance `session_generation`.

### 14.6 Seller account request

Verify:

- GET `/external/v1/seller/account`;
- bearer belongs to current committed session generation;
- no mutation occurs.

### 14.7 Seller account timeout policy

Verify:

- connect timeout is `5s`;
- read timeout is `10s`;
- timeout uses bounded read/auth recovery rules and does not preserve READY solely from persistence.

### 14.8 Seller account success predicate

Fixtures must include:

- 200 + non-empty `accountUid` -> endpoint predicate passes;
- 200 missing/empty `accountUid` -> fail closed;
- malformed 200 schema -> fail closed;
- `accountId` is retained as secondary evidence when available but is not substituted for missing `accountUid`.

### 14.9 Identity comparison

For a bound account:

- matching `accountUid` may satisfy identity proof;
- mismatching `accountUid` -> `AUTH_MISMATCH -> REVIEW_REQUIRED`;
- weak fields and `accountId` do not auto-resolve mismatch.

### 14.10 Permission union and write-scope source

Verify:

`M2_ADOPTED_ENDPOINT_GROUP_UNION = {판매자정보}`

and that separate `상품` permission evidence does not make product endpoints callable.

If M2 records `write_scope`, its permission evidence comes from the `PERMISSIONS_SCOPES.md` provider-admin attestation/introspection path, not from a hidden product API probe.

### 14.11 NOT_ADOPTED fail-closed

Attempt to resolve a candidate M5 endpoint through M2 runtime configuration.

Also attempt a direct/raw SmartStore URL call from feature/adaptor code in a controlled static/test fixture.

Expected:

- local rejection;
- zero provider network calls;
- no raw-URL fallback;
- no raw HTTP client available to ordinary SmartStore feature/adaptor code.

### 14.12 Redirect fail-closed

Inject 3xx/308 for each adopted endpoint.

Expected:

- no automatic follow;
- no credential/body forwarding;
- event retained as contract/error evidence.

---

## 15. Runtime evidence before `verified_at`

`verified_at` remains null until real authorized M2 evidence covers both adopted endpoints.

| Endpoint | Minimum measured evidence |
| --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | method/wire URL, content type, SELF body shape, absent `account_id`, applied timeout policy, HTTP status, token result fields, observed `expires_in`, sanitized trace/evidence reference |
| `SMARTSTORE_SELLER_ACCOUNT` | method/wire URL, applied timeout policy, current session generation, HTTP status, observed `accountUid`, `accountId` when available, expected identity, comparison result, sanitized trace/evidence reference |

Also prove:

- no `NOT_ADOPTED` M5 endpoint was called;
- ordinary SmartStore integration code did not bypass the registry through a raw HTTP client;
- redirects were not automatically followed;
- success predicates executed before READY transitions;
- evidence belongs to the correct credential/session generations;
- secrets and unrelated PII were sanitized.

---

## 16. Change control

Before any endpoint becomes `ADOPTED`:

1. confirm current official method/path/schema;
2. confirm application-mode eligibility;
3. confirm required groups;
4. freeze connect/read timeout policy;
5. freeze a machine-checkable success predicate;
6. freeze endpoint/domain error behavior;
7. freeze redirect behavior;
8. for writes, freeze idempotency/replay and `remote_outcome` reconciliation;
9. freeze read-back/consistency rules;
10. add acceptance tests/evidence requirements;
11. independently audit before implementation use.

Upstream changes do not silently rewrite this matrix.

---

## 17. Open questions

| Question | Status |
| --- | --- |
| Real runtime behavior and latency of both M2 adopted endpoints | `PENDING M2 ACCEPTANCE` |
| Whether M2 timeout values need adjustment after measured latency | `MEASURE, THEN REVIEW` |
| Token remote-success/local-commit-unknown behavior | `OWNED BY AUTH.md / MEASUREMENT REQUIRED` |
| Exact M5 registration endpoint set | `NOT_ADOPTED / M5 DESIGN REQUIRED` |
| Exact M5 required API-group union | `NOT FROZEN` |

---

## 18. Final M2 contract

`ADOPTED_ENDPOINTS = {SMARTSTORE_AUTH_TOKEN, SMARTSTORE_SELLER_ACCOUNT}`

`POST /external/v1/oauth2/token = ADOPTED`

`GET /external/v1/seller/account = ADOPTED`

`registry path = relative to base_url`

`product/category/image endpoints = NOT_ADOPTED for M2`

`M2 adopted group union = {판매자정보}`

`상품 permission observation != endpoint adoption`

`M2 write_scope evidence != hidden product network probe`

`token connect/read timeout = 5s/30s`

`seller-account connect/read timeout = 5s/10s`

`2xx != semantic success until success_predicate passes`

`unsupported token_type != usable token`

`token success != session commit`

`session commit != account identity proof`

`accountUid = required primary identity`

`accountId = secondary evidence when available`

`SmartStore feature/adaptor code != raw HTTP client owner`

`all provider calls -> adopted endpoint registry caller`

`NOT_ADOPTED = no network call`

`future mutation adoption requires timeout + idempotency + read-back + remote-outcome contract before use`

Unknown or unfrozen endpoint behavior remains unavailable until explicitly adopted.