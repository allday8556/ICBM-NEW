# SmartStore Endpoint Matrix

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M2_CONNECT_ADOPTED_SET_FROZEN_FOR_REVIEW` |
| M2 integration mode | `OWN_STORE_SELF` |
| Current adopted endpoint count | `2` |
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

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/780
  - `내스토어 애플리케이션` uses `type=SELF` only.
  - one own-store application is connected to one SmartStore account.
  - SELF tokens for an own-store application can call seller SmartStore data APIs permitted to that application.
- https://github.com/commerce-api-naver/commerce-api/discussions/2425
  - `accountUid` and `accountId` both identify a SmartStore uniquely under current provider guidance.
  - `accountUid` is the Commerce API integration-oriented identifier.
- https://github.com/commerce-api-naver/commerce-api/discussions/3751
  - for `type=SELF`, `account_id` MUST NOT be sent.
  - required token fields belong in the `application/x-www-form-urlencoded` request body.
  - `grant_type=client_credentials` is mandatory.
  - the notice explicitly states that own-store applications use `type=SELF`.

Freshness rule:

- Any upstream Commerce API version change, endpoint path/method change, auth-mode change, request/response-schema change, API-group mapping change, redirect change, or application-mode eligibility change SHALL trigger immediate review.
- `review_due` is a fallback calendar bound when automated upstream change detection is absent or broken.
- `retrieved_at` means documentation was inspected.
- `verified_at` remains null until the adopted M2 endpoints are measured against a real authorized SmartStore account under the M2 acceptance contract.

---

## 1. Purpose

This file is the SmartStore integration endpoint allow-list and contract matrix adopted by ICBM.

It is not a catalog of every endpoint NAVER publishes.

The key rule is:

`documented by provider != adopted by ICBM`

Only an endpoint whose row is `ADOPTED` may be reachable from the corresponding production integration path.

A row marked `NOT_ADOPTED` is informational planning metadata only.

It MUST NOT become callable merely because:

- the provider documents it;
- its URL is known;
- a developer added a raw HTTP call;
- its API group appears enabled;
- another marketplace uses an equivalent capability;
- it is planned for a later milestone.

For M2 CONNECT the adopted set is deliberately limited to two endpoints:

1. issue/reissue an authentication token;
2. perform a protected seller-account read that proves the authenticated SmartStore identity.

Product registration, image upload, category discovery, product read-back, and other marketplace operations are not part of the M2 network-call surface.

---

## 2. Adoption states

| State | Meaning | Network use |
| --- | --- | --- |
| `ADOPTED` | Contract fields required for the current milestone are frozen and approved for implementation/testing. | Allowed only through the adopted endpoint contract. |
| `NOT_ADOPTED` | Endpoint is known/planned but its operational contract is not frozen for the current milestone. | Forbidden. No production or acceptance network call. |

An endpoint may transition:

`NOT_ADOPTED -> ADOPTED`

only after its required contract fields are completed and reviewed.

For a mutating endpoint this includes, at minimum:

- application-mode eligibility;
- required API-group set;
- exact method/path/content type;
- machine-checkable `success_predicate`;
- structured error/domain result contract;
- redirect policy;
- idempotency/replay policy;
- read-back/reconciliation contract;
- remote-outcome rules;
- acceptance evidence plan.

No future milestone may infer those fields from this document's placeholder rows.

---

## 3. Base host and path composition

The provider REST host is:

`https://api.commerce.naver.com/external`

The endpoint documentation normally presents paths without the `/external` host prefix.

ICBM SHALL therefore model these separately:

| Field | Example |
| --- | --- |
| `base_url` | `https://api.commerce.naver.com/external` |
| documented `path` | `/v1/seller/account` |
| final wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |

Implementations MUST NOT maintain competing hard-coded base prefixes that can produce either:

- a missing `/external`; or
- a duplicated `/external/external`.

The endpoint ID and adopted path are canonical integration inputs.

---

## 4. Master endpoint registry

| Endpoint ID | Adoption | Milestone | Method | Documented path | Purpose | App mode | Required API group | Marketplace mutation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | `ADOPTED` | M2 CONNECT | `POST` | `/v1/oauth2/token` | Issue/reissue bearer token | `OWN_STORE_SELF` | `N/A` | No product/business mutation |
| `SMARTSTORE_SELLER_ACCOUNT` | `ADOPTED` | M2 CONNECT | `GET` | `/v1/seller/account` | Protected account identity proof | `OWN_STORE_SELF` | `판매자정보` | No |
| `SMARTSTORE_PRODUCT_CREATE_V2` | `NOT_ADOPTED` | M5 candidate | `POST` | `/v2/products` | Product CREATE | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | Yes |
| `SMARTSTORE_ORIGIN_PRODUCT_READ_V2` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v2/products/origin-products/{originProductNo}` | Product read-back candidate | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_CHANNEL_PRODUCT_READ_V2` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v2/products/channel-products/{channelProductNo}` | Channel listing read-back candidate | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_IMAGE_UPLOAD` | `NOT_ADOPTED` | M5 candidate | `POST` | `/v1/product-images/upload` | Product image upload | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | Remote side effect; contract not frozen |
| `SMARTSTORE_CATEGORY_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories` | Category discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_CATEGORY_READ` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/categories/{categoryId}` | Category validation | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_LIST` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attributes` | Category attribute discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/product-attributes/attribute-values` | Attribute value discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_STANDARD_OPTIONS` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/options/standard-options` | Standard option discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |
| `SMARTSTORE_NOTICE_TYPES` | `NOT_ADOPTED` | M5 candidate | `GET` | `/v1/products-for-provided-notice` | Product-notice type discovery | `TBD_AT_ADOPTION` | `TBD_AT_ADOPTION` | No |

The M5 candidate list is not a promise that every row will be adopted.

Presence in this table does not authorize implementation or runtime invocation.

---

## 5. M2 operation graph

The complete M2 CONNECT network sequence is:

`SMARTSTORE_AUTH_TOKEN`

`-> durable session-generation commit per AUTH.md`

`-> SMARTSTORE_SELLER_ACCOUNT`

`-> account identity comparison/binding per ACCOUNT_IDENTITY.md`

`-> AUTH_READY candidate`

No product endpoint is part of this graph.

The M2 adopted endpoint permission union is therefore:

`{판매자정보}`

The separate planned product write permission baseline in `PERMISSIONS_SCOPES.md` does not change that endpoint union.

Specifically:

- `상품` may be observed as provider permission evidence for future product-write capability;
- `write_scope` may therefore be `READY`, `MISSING`, or `UNKNOWN` during/after M2;
- none of those states makes a product endpoint `ADOPTED` in M2;
- M2 CONNECT MUST NOT issue a product request merely to discover or reconfirm product permission.

This distinction is intentional:

`permission observation != endpoint adoption`

---

## 6. Adopted endpoint contract: `SMARTSTORE_AUTH_TOKEN`

### 6.1 Registry

| Field | Contract |
| --- | --- |
| Endpoint ID | `SMARTSTORE_AUTH_TOKEN` |
| Adoption | `ADOPTED` |
| Milestone | M2 CONNECT |
| Purpose | Issue/reissue Commerce API bearer token |
| Provider method | `POST` |
| Documented path | `/v1/oauth2/token` |
| Base URL | `https://api.commerce.naver.com/external` |
| Wire URL | `https://api.commerce.naver.com/external/v1/oauth2/token` |
| Application mode | `OWN_STORE_SELF` |
| OAuth grant | Client Credentials |
| Bearer auth on this request | No |
| Request content type | `application/x-www-form-urlencoded` |
| Required API group | `N/A` |
| Marketplace mutation | No product/order/business-resource mutation |
| Redirect policy | `NO_FOLLOW` |
| Runtime verification | `PENDING` |

### 6.2 Request predicate

For the M2 own-store mode the request body MUST contain exactly the applicable authentication fields:

| Field | M2 rule |
| --- | --- |
| `client_id` | Required. Current own-store application ID. |
| `timestamp` | Required. Generated under `AUTH.md` timestamp/signature rules. |
| `client_secret_sign` | Required. Generated from the current credential generation under `AUTH.md`. |
| `grant_type` | Required exact value `client_credentials`. |
| `type` | Required exact value `SELF`. |
| `account_id` | **Forbidden** for `OWN_STORE_SELF`. |

The request MUST NOT place these required payload fields only in the URL query string as a substitute for the form body.

A locally detected violation SHALL fail before network I/O where feasible.

### 6.3 Success predicate

HTTP status alone is not sufficient.

The machine-checkable endpoint success predicate is:

`token_endpoint_success =`

`HTTP 200`

`AND response is valid JSON object`

`AND access_token is a non-empty string`

`AND expires_in is a positive integer`

`AND token_type is Bearer (case-insensitive comparison)`

If HTTP 200 is received but this predicate fails:

- semantic success MUST NOT be declared;
- no `session_generation` may be committed from the malformed/incomplete response;
- classify/handle as response-schema or local/provider contract drift under `ERRORS.md`.

Endpoint success means only that a candidate token response was obtained.

It does NOT mean:

- the intended SmartStore account was proven;
- `AUTH_READY` is true;
- product write permission exists;
- the token was durably committed locally.

### 6.4 Session-generation transition

A successful provider response creates a token candidate.

The current ICBM authentication session changes only after the complete local session bundle is durably committed under `AUTH.md`.

Therefore:

`token_endpoint_success != session_generation_committed`

and:

`session_generation_committed != AUTH_READY`

The next protected endpoint call must still prove the account identity.

### 6.5 Provider lifetime behavior

Current provider documentation states:

- default token lifetime: `10,800` seconds / 180 minutes;
- if the same token resource has at least 30 minutes remaining, the existing token can be returned;
- when fewer than 30 minutes remain, a new token can be issued;
- the old token remains usable until its own expiration after a new token is issued.

These facts are governed by `AUTH.md` lifecycle rules and do not make arbitrary repeated POSTs safe.

In particular, first-token remote-success/local-commit-unknown remains an unresolved runtime case under `AUTH.md`.

### 6.6 Error surface

The current endpoint documentation lists:

| HTTP | Provider meaning at endpoint-document level | ICBM handling owner |
| --- | --- | --- |
| `400` | Validation error | `ERRORS.md` + `AUTH.md`; do not guess credential invalidity from status alone |
| `403` | Access/authorization error | `ERRORS.md` + application lifecycle evidence |
| `500` | Temporary internal system error | `ERRORS.md`; bounded recovery only |

Transport/gateway failures remain possible even when not enumerated by the endpoint page.

No undocumented provider code is promoted into a durable mapping here.

### 6.7 Redirect policy

The adopted endpoint response list does not document 3xx/308 as a normal success path.

Therefore:

`redirect_policy = NO_FOLLOW`

Any redirect is contract drift/diagnostic evidence and MUST NOT be silently followed by the generic client.

### 6.8 Retry / replay rule

This POST is an authentication-control operation, not a marketplace product mutation.

Its retry/reissue behavior is owned by `AUTH.md`, including:

- bounded issuance/reissuance;
- session generation atomicity;
- token lifetime window;
- first-token crash uncertainty;
- no false user-reapproval escalation from a purely local lost response.

`ERRORS.md` generic mutation replay rules do not replace the stronger authentication lifecycle contract.

---

## 7. Adopted endpoint contract: `SMARTSTORE_SELLER_ACCOUNT`

### 7.1 Registry

| Field | Contract |
| --- | --- |
| Endpoint ID | `SMARTSTORE_SELLER_ACCOUNT` |
| Adoption | `ADOPTED` |
| Milestone | M2 CONNECT |
| Purpose | Protected account identity proof |
| Provider method | `GET` |
| Documented path | `/v1/seller/account` |
| Base URL | `https://api.commerce.naver.com/external` |
| Wire URL | `https://api.commerce.naver.com/external/v1/seller/account` |
| Application mode | `OWN_STORE_SELF` |
| Authentication | `Authorization: Bearer {access_token}` |
| Required API group | `판매자정보` |
| Marketplace mutation | No |
| Redirect policy | `NO_FOLLOW` |
| Runtime verification | `PENDING` |

### 7.2 Request predicate

The request MUST:

- use `GET`;
- target the adopted path exactly;
- use the current committed `session_generation` bearer token;
- contain no product/business mutation payload;
- execute only after the token candidate has been durably committed as the current session generation;
- preserve the session/credential generation association for the resulting evidence.

A token issuance response that has not been durably committed MUST NOT be used to manufacture durable account-proof evidence.

### 7.3 Endpoint success predicate

The machine-checkable endpoint success predicate is:

`seller_account_endpoint_success =`

`HTTP 200`

`AND response is valid JSON object`

`AND accountUid is a non-empty string`

`AND accountId is a non-empty string`

The endpoint success predicate is deliberately separate from the account-binding predicate.

For an already bound MarketplaceAccount:

`account_identity_proven = seller_account_endpoint_success`

`AND observed.accountUid == expected.provider_account_uid`

For first binding:

- endpoint success supplies the observed identity;
- binding still follows the atomic first-binding contract in `ACCOUNT_IDENTITY.md`;
- successful read alone does not silently bind the account.

### 7.4 AUTH_READY effect

This endpoint is the M2 protected-read proof path.

For an already bound account:

`AUTH_READY =`

`current auth/session invariants hold`

`AND seller_account_endpoint_success`

`AND accountUid matches the canonical binding`

`AND evidence belongs to the current credential/session generations`

A mismatch remains:

`AUTH_MISMATCH -> REVIEW_REQUIRED`

Weak/display fields MUST NOT override an `accountUid` mismatch.

### 7.5 Permission group

This endpoint belongs to the seller-information capability surface and requires the `판매자정보` API group under the adopted M2 contract.

Consequently:

- token issuance success with this endpoint unavailable is not sufficient for `AUTH_READY`;
- failure of this endpoint MUST NOT automatically be rewritten as product `write_scope=MISSING`;
- product group `상품` is not part of the M2 endpoint-call union.

### 7.6 Documented domain error surface

The current endpoint documentation lists the following endpoint-level errors.

| HTTP | Provider code | Provider description | Matrix rule |
| --- | --- | --- | --- |
| `400` | `GENERAL_ERROR` | Unhandled error | Do not assume validation; classify using `ERRORS.md`. |
| `401` | `UNAUTHORIZED` | No access authority | Diagnose auth/permission/context under `ERRORS.md`; no unconditional mapping. |
| `403` | `ROLE_NOT_FOUND` | Role missing | Endpoint-specific provider evidence; final state mapping remains evidence-based. |
| `403` | `PROVISION_NOT_FOUND` | Terms/agreement required | Provider condition; do not relabel credentials invalid. |
| `403` | `INVALID_CHANNEL_STATUS` | Invalid channel status | Provider/account state evidence. |
| `403` | `INVALID_STORE_STATUS` | Invalid store status | Provider/account state evidence. |
| `403` | `INVALID_REPRESENT_STATUS` | Invalid representative status | Provider/account state evidence. |
| `403` | `INVALID_MEMBER_STATUS` | Invalid member status | Provider/account state evidence. |
| `403` | `INVALID_INTERLOCK_STATUS` | Invalid interlock status | Provider/integration state evidence. |
| `403` | `RESOURCE_NOT_AVAILABLE` | Resource unavailable | Provider/resource condition; diagnose before classifying. |
| `404` | `CHANNEL_NOT_FOUND` | Channel not found | Preserve endpoint context; no global NOT_FOUND mapping. |
| `404` | `STORE_NOT_FOUND` | Store not found | Preserve endpoint context; no global NOT_FOUND mapping. |
| `404` | `REPRESENT_NOT_FOUND` | Representative not found | Preserve endpoint context; no global NOT_FOUND mapping. |
| `404` | `MEMBER_NOT_FOUND` | Member not found | Preserve endpoint context; no global NOT_FOUND mapping. |
| `404` | `INTERLOCK_NOT_FOUND` | Interlock data absent | Preserve endpoint context; no global NOT_FOUND mapping. |
| `500` | `PARSING_FAIL` | Invalid JSON syntax | Provider/server or contract evidence; no blind retry assumption. |
| `500` | `SERDES_FAIL` | Serialization/deserialization failure | Classify using current evidence. |
| `500` | `ENCDEC_FAIL` | Encryption/decryption failure | Classify using current evidence. |
| `500` | `GENERAL_ERROR` | Unhandled error | Candidate transient/unknown depending on evidence. |

These codes are endpoint evidence inputs.

They are not a replacement for the canonical classification rules in `ERRORS.md`.

### 7.7 Redirect policy

The current endpoint response contract lists 200/400/401/403/404/500 and does not list 308 as an adopted normal path.

Therefore:

`redirect_policy = NO_FOLLOW`

Any redirect is unexpected contract/provider drift and must be handled without generic automatic redirect following.

### 7.8 Retry rule

This endpoint is read-only.

Retry behavior follows `ERRORS.md` and `AUTH.md`:

- bounded `TRANSIENT` read retry;
- bounded auth recovery when AUTH is positively established;
- no hot loop;
- `UNKNOWN` does not become credential invalidity;
- account mismatch never triggers automatic account switching.

---

## 8. M2 completion contract

The endpoint matrix defines only the network-call surface.

M2 CONNECT is not equivalent to token issuance.

For an already bound account, the minimum successful network sequence is:

| Step | Endpoint | Required result |
| --- | --- | --- |
| 1 | `SMARTSTORE_AUTH_TOKEN` | Token endpoint success predicate passes. |
| 2 | Local auth transaction | Candidate token/session bundle is durably committed as current `session_generation`. |
| 3 | `SMARTSTORE_SELLER_ACCOUNT` | Seller-account endpoint success predicate passes using that session generation. |
| 4 | Identity contract | Observed `accountUid` matches canonical `provider_account_uid`. |
| 5 | Capability state | `auth=READY` may be established. |

For first binding, step 4 is replaced by the explicit atomic first-binding flow in `ACCOUNT_IDENTITY.md`.

Product-write capability remains separate:

`auth READY != write_scope READY != write READY`

No M5 endpoint may be called to complete M2 CONNECT.

---

## 9. Relationship to `PERMISSIONS_SCOPES.md`

The endpoint matrix owns per-endpoint required groups only for adopted endpoints.

For M2:

`required_groups(SMARTSTORE_AUTH_TOKEN) = {}`

`required_groups(SMARTSTORE_SELLER_ACCOUNT) = {판매자정보}`

Therefore:

`M2_ADOPTED_ENDPOINT_GROUP_UNION = {판매자정보}`

This does not repeal the separate planned product-write scope model.

`PERMISSIONS_SCOPES.md` may track whether the current provider application appears to contain the `상품` group for future registration capability.

That evidence has these limits:

`상품 group observed != product endpoint adopted`

`상품 group observed != product write verified`

`상품 group missing != M2 auth endpoint set invalid`

The final M5 write permission union SHALL be computed only after M5 endpoints transition to `ADOPTED` with exact required-group mappings.

---

## 10. Relationship to `ERRORS.md`

Every `ADOPTED` endpoint MUST define a success predicate.

The success predicate is evaluated for every response, including HTTP 2xx.

The sequence is:

`receive response`

`-> parse according to endpoint contract`

`-> evaluate success_predicate`

`-> only then declare semantic endpoint success`

A 2xx response that fails the predicate is not successful.

An endpoint with no frozen success predicate is not eligible for automated execution.

Error mapping uses:

- this endpoint ID;
- response/failure layer;
- current method/path contract;
- endpoint-specific codes where documented;
- `AUTH.md` evidence;
- `PERMISSIONS_SCOPES.md` evidence;
- `ERRORS.md` canonical mapping rules.

No row may add a marketplace-specific global error class.

---

## 11. Redirect policy

Generic automatic redirect following is forbidden for SmartStore integration clients unless an adopted endpoint explicitly permits it.

For both M2 adopted endpoints:

`redirect_policy = NO_FOLLOW`

Therefore a generic client setting equivalent to unconditional redirect following MUST NOT govern the M2 adapter.

This is especially important for future mutation endpoints because HTTP 308 can preserve method/body and replay a write.

Any future endpoint that needs redirect support must freeze:

- exact accepted status;
- allowed host/path target;
- method/body behavior;
- credential-forwarding policy;
- replay/idempotency safety.

before becoming `ADOPTED`.

---

## 12. M5 candidate rows are intentionally incomplete

The M5 rows in the master registry exist only to make future scope visible.

They intentionally do NOT freeze:

- final required API-group set;
- application-mode eligibility;
- success predicate;
- error/domain-code mapping;
- redirect safety;
- idempotency;
- read-back identity;
- consistency window;
- `remote_outcome` reconciliation;
- retry budget;
- cleanup behavior.

Those values MUST be researched and measured when the endpoint is proposed for M5 adoption.

No implementation may substitute a guessed default for a `TBD_AT_ADOPTION` field.

A future M5 adoption PR should convert rows individually or as one bounded registration transaction only when the behavioral objective requires the full group.

---

## 13. Static/repository enforcement requirements

M2 implementation SHALL make endpoint adoption enforceable rather than documentary only.

At minimum the repository/test contract must prove:

1. the SmartStore M2 adapter can resolve only the two `ADOPTED` endpoint IDs required by M2 CONNECT;
2. no M2 path can issue requests to an endpoint whose matrix state is `NOT_ADOPTED`;
3. method/path/base-url values come from one provider endpoint contract rather than duplicated ad hoc strings;
4. `SMARTSTORE_AUTH_TOKEN` enforces form payload requirements, `type=SELF`, and absence of `account_id`;
5. `SMARTSTORE_SELLER_ACCOUNT` requires the current committed bearer session;
6. generic automatic redirects are disabled for the adopted M2 client path;
7. every adopted endpoint has a machine-checkable success predicate;
8. a 2xx response that fails its success predicate fails closed;
9. generation/evidence records contain the endpoint ID used;
10. adding a raw SmartStore URL outside the adopted registry is rejected by static/repository tests where feasible.

The exact code mechanism is intentionally left to the implementation design, but the observable invariants are mandatory.

---

## 14. Acceptance requirements

### 14.1 Endpoint allow-list

Verify the M2 runtime endpoint registry contains exactly:

- `SMARTSTORE_AUTH_TOKEN`;
- `SMARTSTORE_SELLER_ACCOUNT`.

M5 candidate rows MUST NOT be runtime-callable.

### 14.2 Token request shape

Using a controlled fixture and then real non-destructive runtime evidence:

- method is POST;
- final URL is `/external/v1/oauth2/token` under the official host;
- content type is `application/x-www-form-urlencoded`;
- required body fields are present;
- `grant_type=client_credentials`;
- `type=SELF`;
- `account_id` is absent;
- credentials/signatures are not leaked into logs.

### 14.3 Token success predicate

Test at least:

- valid 200 token body -> predicate passes;
- 200 missing `access_token` -> fails closed;
- 200 empty `access_token` -> fails closed;
- 200 invalid/nonpositive `expires_in` -> fails closed;
- 200 unexpected `token_type` -> fails closed;
- failure does not advance `session_generation`.

### 14.4 Session commit boundary

Verify token endpoint success alone does not make the session current.

Only the durable local auth commit may advance `session_generation`.

Crash/unknown cases remain governed by `AUTH.md`.

### 14.5 Seller-account request

Using the committed session generation:

- method is GET;
- final URL is `/external/v1/seller/account` under the official host;
- bearer token belongs to the current committed session generation;
- no product mutation occurs.

### 14.6 Seller-account success predicate

Test at least:

- valid 200 with non-empty `accountUid` and `accountId` -> endpoint predicate passes;
- 200 missing/empty primary identity -> fails closed;
- 200 malformed schema -> fails closed;
- no account identity proof is manufactured from a malformed 2xx.

### 14.7 Bound identity match

For an already bound account:

- matching `accountUid` may satisfy the identity comparison;
- mismatching `accountUid` converges to `AUTH_MISMATCH -> REVIEW_REQUIRED`;
- store name/channel URL or other weak fields do not override the mismatch.

### 14.8 M2 permission union

Verify the adopted endpoint group union is exactly:

`{판매자정보}`

and that the separately observed/planned `상품` permission does not make product endpoints callable during M2.

### 14.9 NOT_ADOPTED fail-closed

Attempt to resolve/call at least one M5 candidate endpoint through the M2 endpoint registry.

Expected result:

- local rejection before network I/O;
- no provider request;
- no silent fallback to raw URL invocation.

### 14.10 Redirect fail-closed

For each adopted M2 endpoint, inject a 3xx/308 fixture.

Verify:

- generic client does not auto-follow;
- no credentials or signed body are forwarded;
- the event is treated as contract/error evidence.

### 14.11 2xx semantic gating

For each adopted endpoint, inject HTTP 200 bodies that violate the endpoint success predicate.

Verify no downstream READY/session/identity transition occurs.

---

## 15. Runtime evidence required before `verified_at`

`verified_at` MUST remain null until real M2 evidence covers both adopted endpoints.

Minimum evidence:

| Endpoint | Required measured evidence |
| --- | --- |
| `SMARTSTORE_AUTH_TOKEN` | exact method/wire URL, form content type, SELF request shape, absence of `account_id`, HTTP/result shape, token fields, observed `expires_in`, trace/evidence reference, sanitized failure handling |
| `SMARTSTORE_SELLER_ACCOUNT` | exact method/wire URL, bearer session generation, HTTP/result shape, observed `accountUid`, observed `accountId`, expected identity, identity comparison result, trace/evidence reference |

Additionally verify:

- no M5 `NOT_ADOPTED` endpoint was called during M2 acceptance;
- redirects were not automatically followed;
- success predicates ran before semantic success transitions;
- evidence was bound to the correct credential/session generations;
- sensitive values were sanitized.

Reading provider docs is insufficient to populate `verified_at`.

---

## 16. Change control

An endpoint adoption change is a contract change.

Before changing a row from `NOT_ADOPTED` to `ADOPTED`:

1. confirm current official method/path/schema;
2. confirm application-mode eligibility;
3. confirm required API groups;
4. define a machine-checkable success predicate;
5. define documented/domain error handling;
6. define redirect behavior;
7. for writes, define idempotency/replay and `remote_outcome` reconciliation;
8. define read-back/consistency rules;
9. add acceptance tests/evidence requirements;
10. independently audit the change before implementation use.

Provider upstream changes do not silently mutate this matrix.

The change flow remains:

`upstream change detection`

`-> diff`

`-> impact analysis`

`-> contract/ADR update`

`-> tests`

`-> approval`

---

## 17. Open questions

### Q1. M2 real runtime evidence

Do the two adopted endpoints behave exactly as documented under the user's real authorized own-store application?

Status:

`PENDING M2 ACCEPTANCE`

### Q2. Token endpoint response-loss behavior

After remote token issuance succeeds but the local process loses the response before durable commit, what exact subsequent issuance behavior is observed?

Status:

`OWNED BY AUTH.md / RUNTIME MEASUREMENT REQUIRED`

### Q3. Future product endpoint set

Which M5 candidate endpoints are actually required for one bounded product-registration transaction, including all prerequisite metadata/image calls and external read-back?

Status:

`NOT_ADOPTED / M5 DESIGN REQUIRED`

### Q4. M5 permission union

What is the exact union of API groups required by the final adopted M5 transaction?

Status:

`NOT FROZEN`

`PERMISSIONS_SCOPES.md` currently records `상품` as the planned product-write baseline, not as a substitute for per-endpoint adoption.

---

## 18. Final M2 endpoint contract

For SmartStore M2 CONNECT:

`ADOPTED_ENDPOINTS = {SMARTSTORE_AUTH_TOKEN, SMARTSTORE_SELLER_ACCOUNT}`

`POST /external/v1/oauth2/token = ADOPTED`

`GET /external/v1/seller/account = ADOPTED`

`all product/category/image endpoints = NOT_ADOPTED for M2`

`M2 adopted API-group union = {판매자정보}`

`상품 permission observation != product endpoint adoption`

`documented endpoint != callable endpoint`

`2xx != semantic success until success_predicate passes`

`token success != session commit`

`session commit != account identity proof`

`account identity proof requires /v1/seller/account + canonical identity contract`

`NOT_ADOPTED = no network call`

`future mutation adoption requires idempotency + read-back + remote-outcome contract before use`

Unknown or unfrozen endpoint behavior remains unavailable until explicitly adopted.