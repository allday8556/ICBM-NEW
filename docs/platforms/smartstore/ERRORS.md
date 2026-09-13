# SmartStore Error Classification Contract

## Status

- Provider: NAVER SmartStore / Commerce API
- Contract status: `DOCUMENTED_BASELINE_WITH_ENDPOINT_GAPS`
- M2 integration mode: `OWN_STORE_SELF`
- Canonical error classes: existing ICBM classes only
- Runtime verification: `PENDING`
- Endpoint-specific domain mapping: `PARTIAL / PENDING ENDPOINT_MATRIX`

## Provenance

Primary upstream documentation:

- source_url:
  - https://apicenter.commerce.naver.com/docs/trouble-shooting
  - https://apicenter.commerce.naver.com/docs/restful-api
  - https://apicenter.commerce.naver.com/docs/auth
  - https://apicenter.commerce.naver.com/docs/restriction
  - https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product
  - https://apicenter.commerce.naver.com/docs/commerce-api/current/read-origin-product-product
- upstream_version: `2.88.0`
- retrieved_at: `2026-09-14`
- verified_at: `null`
- review_due: `2026-10-14`

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/1013
  - `GW.AUTHN` can represent a missing API-group permission.
- https://github.com/commerce-api-naver/commerce-api/discussions/1835
  - missing `상품` permission produced `401/GW.AUTHN` for a product API.
- https://github.com/commerce-api-naver/commerce-api/discussions/3676
  - malformed `Authorization` header produced `GW.AUTHN`.
- https://github.com/commerce-api-naver/commerce-api/discussions/3762
  - expired access token produced `401/GW.AUTHN`.
- https://github.com/commerce-api-naver/commerce-api/discussions/3428
  - the same outbound IP intermittently received `GW.IP_NOT_ALLOWED` and success; NAVER later identified a temporary provider-side condition.
- https://github.com/commerce-api-naver/commerce-api/discussions/1649
  - product `BAD_REQUEST` with structured `invalidInputs` was caused by a missing required product-notice field.
- https://github.com/commerce-api-naver/commerce-api/discussions/3529
  - a restricted seller tag was rejected as `BAD_REQUEST`, showing that one HTTP/code family can represent provider policy as well as structural validation.

Freshness rule:

- Any upstream Commerce API version change, gateway error-table change, endpoint response-schema change, error-code semantic change, permission-error change, retry guidance change, or redirect behavior change SHALL trigger immediate review.
- `review_due` is a fallback calendar bound when automated change detection is absent or broken.
- Provider documentation describes known error shapes; it does not prove that every real failure will match a known mapping.
- Unknown or contradictory behavior remains `UNKNOWN` until measured or officially documented.

---

## 1. Purpose

This document maps SmartStore provider/transport failures into the existing ICBM canonical error classes without inventing marketplace-specific global error classes.

The canonical classes are:

- `TRANSIENT`
- `RATE_LIMIT`
- `AUTH`
- `VALIDATION`
- `POLICY`
- `CONFLICT`
- `DUPLICATE`
- `REVIEW_REQUIRED`
- `FATAL`
- `UNKNOWN`

This document does not redefine those classes.

It defines the evidence required to select one of them for SmartStore.

The core safety rule is:

`provider code != root cause`

and:

`error class != replay permission`

A correct classification does not by itself authorize a retry of a mutating request.

---

## 2. Two independent questions

Every failed or ambiguous SmartStore operation SHALL answer two separate questions.

### 2.1 What kind of failure is this?

Represented by:

`error_class`

using the canonical ICBM classes above.

### 2.2 What is known about the remote mutation outcome?

For mutating operations ICBM SHALL separately track:

`remote_outcome in {APPLIED_PROVEN, NOT_APPLIED_PROVEN, UNKNOWN}`

These are not new `error_class` values.

They describe operation-outcome evidence.

Examples:

- a network timeout after a POST may be `error_class=TRANSIENT` while `remote_outcome=UNKNOWN`;
- a deterministic pre-submit local validation failure may be `error_class=VALIDATION` with `remote_outcome=NOT_APPLIED_PROVEN`;
- a successful CREATE followed by matching external read-back may establish `remote_outcome=APPLIED_PROVEN`.

ICBM MUST NOT infer:

`TRANSIENT -> safe to replay`

or:

`failure response -> definitely not applied`

without operation-specific evidence.

---

## 3. Why code-to-cause tables are insufficient

SmartStore error handling is many-to-many.

### 3.1 One provider code can represent multiple causes

`GW.AUTHN` is the clearest example.

Official provider materials show `GW.AUTHN` associated with at least:

- expired bearer token;
- missing API-group permission;
- malformed or invalid `Authorization` header.

Therefore:

`GW.AUTHN != AUTH`

as a universal mapping.

The surrounding evidence determines whether the resulting ICBM class is `AUTH`, `POLICY`, `FATAL`, or `UNKNOWN`.

Likewise, `GW.IP_NOT_ALLOWED` is documented as an IP allow-list failure, but official support has also confirmed a case where the same configured outbound IP intermittently received the error due to a temporary provider-side condition.

Therefore:

`GW.IP_NOT_ALLOWED != always POLICY`

without corroborating current IP/configuration evidence.

### 3.2 One root cause can surface through different codes/statuses

Permission denial is not guaranteed to use one wire representation.

Examples in current provider materials include:

- gateway-level `401/GW.AUTHN` for missing API-group permission;
- endpoint schemas that separately define `403/FORBIDDEN` for permission denial.

Therefore a missing permission must be diagnosed from the complete response/evidence context, not from one fixed code.

The same principle applies to:

- authentication failures;
- missing resources;
- validation/policy failures;
- conflict/duplicate conditions.

### 3.3 One HTTP/code family can contain different ICBM classes

Product APIs use `400/BAD_REQUEST` broadly.

Structured `invalidInputs` can indicate:

- structural/input validation such as missing required fields, invalid enum values, numeric range errors, or incompatible values -> usually `VALIDATION`;
- provider policy such as restricted seller tags -> `POLICY` when the restriction is positively identified;
- undocumented/provider temporary conditions -> potentially `TRANSIENT` or `UNKNOWN` depending on evidence.

Therefore:

`BAD_REQUEST != always VALIDATION`

### 3.4 HTTP success does not always equal operation success

Some Commerce API operations can return per-item failures or asynchronous acceptance semantics.

Therefore generic infrastructure MUST NOT assume:

`2xx -> semantic success`

The endpoint contract must define whether success means:

- completed operation;
- accepted/queued operation;
- partially successful batch;
- response requiring a later result/read-back check.

The later `ENDPOINT_MATRIX.md` SHALL record this per adopted endpoint.

---

## 4. Response/failure layers

ICBM SHALL identify the layer before assigning a final error class.

### 4.1 `TRANSPORT`

No trustworthy provider response was obtained.

Examples:

- DNS failure;
- TCP connection failure;
- TLS failure;
- client-side timeout;
- connection reset before a complete provider response;
- malformed/truncated response that cannot be safely interpreted.

Transport failures are often `TRANSIENT` candidates, but write outcome may still be `UNKNOWN` if the request could have reached NAVER.

### 4.2 `GATEWAY`

Provider error code begins with `GW.`.

Official gateway examples include:

- `GW.AUTHN`
- `GW.IP_NOT_ALLOWED`
- `GW.NOT_FOUND`
- `GW.RATE_LIMIT`
- `GW.QUOTA_LIMIT`
- `GW.PROXY.01` ... `GW.PROXY.05`
- `GW.INTERNAL_SERVER_ERROR`
- `GW.BLOCK.01`
- `GW.BLOCK.02`
- `GW.TIMEOUT.01`
- `GW.TIMEOUT.02`

Gateway responses usually include a `traceId` and should be preserved as sanitized evidence.

### 4.3 `API_SERVER_STANDARD`

The target Commerce API server returned a normal endpoint error shape such as:

- `BAD_REQUEST`
- `UNAUTHORIZED`
- `FORBIDDEN`
- `NOT_FOUND`
- `INTERNAL_SERVER_ERROR`
- `PERMANENT_REDIRECT`

These names are still not sufficient by themselves for all final ICBM classification decisions.

### 4.4 `API_SERVER_DOMAIN`

The endpoint returned a documented domain-specific error code.

Examples from other Commerce API domains include explicit codes for:

- account status;
- role/authorization;
- invalid state;
- duplicate resource;
- changed conditions;
- payment/solution eligibility;
- per-item order failures.

Endpoint-specific domain codes can provide stronger evidence than generic HTTP status.

M2 SHALL adopt only domain codes belonging to endpoints actually present in `ENDPOINT_MATRIX.md`.

### 4.5 `OPERATION_RESULT`

A transport/HTTP request succeeded but the operation result contains nested, item-level, asynchronous, or read-back failure information.

This layer prevents a `2xx` status from erasing semantic failures.

### 4.6 `LOCAL_CONTRACT`

ICBM detected a local protocol or invariant failure before provider truth could be trusted.

Examples:

- wrong method/path generated despite current endpoint contract;
- unsupported content type generated by ICBM;
- malformed authorization header constructed locally;
- response schema drift that the parser cannot safely interpret;
- mismatched credential/session generation;
- forbidden automatic redirect/replay path.

This layer often results in `FATAL`, `REVIEW_REQUIRED`, or `UNKNOWN`, depending on whether the defect is proven.

---

## 5. Required error evidence record

Every material provider/transport failure SHOULD produce a sanitized evidence record sufficient to reproduce the classification decision.

At minimum record:

- provider=`SMARTSTORE`;
- internal `marketplace_account_id`;
- endpoint identifier from the adopted endpoint matrix;
- HTTP method;
- whether the operation is mutating;
- local attempt/request ID;
- credential generation;
- session generation;
- observation timestamp;
- response/failure layer;
- HTTP status when available;
- provider error code when available;
- sanitized provider message when safe;
- sanitized structured details such as `invalidInputs.name/type/message` when safe;
- provider timestamp when available;
- provider `traceId` / `GNCP-GW-Trace-ID` when available;
- rate-limit/quota response headers when relevant;
- `error_class`;
- classification basis;
- `remote_outcome` for mutating operations;
- retry/reconciliation decision;
- evidence reference.

The error record MUST NOT contain plaintext:

- `client_secret`;
- bearer `access_token`;
- `client_secret_sign`;
- decrypted credential blob;
- complete `client_id`;
- unrelated customer/order PII;
- full product payload unless separately sanitized and explicitly needed for debugging.

Trace ID is valuable diagnostic evidence and SHOULD be retained when present.

---

## 6. Classification basis

Error classification SHALL carry provenance/strength metadata separate from `error_class`.

Recommended values:

- `DOCUMENTED_EXACT`
  - current official documentation uniquely maps the observed endpoint/code/context to the class.
- `DOCUMENTED_CONTEXTUAL`
  - official documentation/support establishes the mapping only after additional evidence is considered.
- `ENDPOINT_SPECIFIC`
  - the adopted endpoint contract supplies the mapping.
- `HEURISTIC`
  - a non-authoritative diagnostic hint exists but is not sufficient for durable truth.
- `UNKNOWN`
  - evidence is insufficient or contradictory.

These are evidence-strength descriptors, not additional canonical error classes.

A heuristic MUST NOT silently become durable provider truth.

---

## 7. Default classification algorithm

For every non-successful or semantically ambiguous operation:

### Step 1: determine whether a trustworthy provider response exists

If no:

- classify the failure layer as `TRANSPORT`;
- preserve transmission-phase evidence when the HTTP client can provide it;
- for writes, do not assume non-execution merely because no response arrived.

### Step 2: identify gateway vs API-server vs operation-result error

- `GW.*` -> `GATEWAY`;
- endpoint response schema -> `API_SERVER_STANDARD` or `API_SERVER_DOMAIN`;
- nested/async/per-item result -> `OPERATION_RESULT`.

### Step 3: validate the endpoint contract

Before blaming authentication, permissions, or provider state, verify:

- current endpoint path;
- method;
- content type;
- auth mode;
- required headers;
- required API groups;
- application-mode eligibility.

A local request-contract defect is not provider credential failure.

### Step 4: consult exact documented mappings

Use the narrowest current rule available from:

1. endpoint-specific documented code;
2. gateway documented code;
3. structured fields such as `invalidInputs.type`;
4. current `AUTH.md` evidence;
5. current `PERMISSIONS_SCOPES.md` evidence.

### Step 5: resolve conflicts conservatively

If two plausible classes remain and the evidence does not distinguish them:

`error_class = UNKNOWN`

Do not choose the class that produces the most convenient retry path.

### Step 6: determine remote mutation outcome separately

For mutating requests establish one of:

- `APPLIED_PROVEN`;
- `NOT_APPLIED_PROVEN`;
- `UNKNOWN`.

Provider error classification alone is insufficient to prove this axis.

### Step 7: select retry/reconciliation action

Retry decisions depend on:

- operation mutability;
- error class;
- remote outcome;
- endpoint idempotency/read-back contract;
- retry budget;
- provider rate-limit guidance.

---

## 8. Gateway baseline mappings

The following are M2 baseline rules, subject to endpoint/context evidence.

### 8.1 `GW.AUTHN`

Default:

`error_class = UNKNOWN`

until the cause is corroborated.

Possible contextual mappings include:

- `AUTH`
  - current token is locally expired or provider support/endpoint evidence uniquely confirms token invalidity/expiry;
- `POLICY`
  - current provider permission evidence proves the required API group is absent;
- `FATAL`
  - ICBM constructed a malformed authorization header or otherwise violated a frozen request contract;
- `UNKNOWN`
  - no evidence cleanly separates auth, permission, formatting, route, or provider state.

Generic auth recovery follows `AUTH.md` and MUST remain bounded.

A `GW.AUTHN` from a write does not authorize blind replay after token recovery.

### 8.2 `GW.IP_NOT_ALLOWED`

If current observed outbound IP is positively known to be absent from the provider allow list:

`error_class = POLICY`

If provider configuration says the IP should be allowed and behavior is intermittent/contradictory:

`error_class = UNKNOWN`

or `TRANSIENT` only when current provider evidence establishes a temporary condition.

Do not rewrite application IP configuration automatically from the error response.

### 8.3 `GW.NOT_FOUND`

This means the gateway did not find a registered API for the requested route.

If ICBM generated a route that contradicts the current adopted endpoint contract:

`error_class = FATAL`

If ICBM used the current documented route and the gateway still reports not found:

`error_class = UNKNOWN`

pending contract/provider drift investigation.

This is different from an API-server `NOT_FOUND` for a domain resource.

### 8.4 `GW.RATE_LIMIT`

`error_class = RATE_LIMIT`

The current provider documents per-API/application token-bucket limits and rate-limit response headers.

Retry must be scheduled/backed off; hot-loop retry is forbidden.

### 8.5 `GW.QUOTA_LIMIT`

`error_class = RATE_LIMIT`

Quota exhaustion is distinct from per-second rate limiting but belongs to the same canonical ICBM error class.

The retry horizon may be much longer and must use provider quota-period evidence when available rather than the short rate-limit backoff policy.

### 8.6 `GW.PROXY.*`, `GW.INTERNAL_SERVER_ERROR`, `GW.BLOCK.*`, `GW.TIMEOUT.*`

These are `TRANSIENT` candidates because the documented causes include gateway/service/network failure, circuit open, maintenance, and timeout.

However:

- repeated deterministic behavior may require reclassification/escalation;
- a mutating request may have `remote_outcome=UNKNOWN`;
- `TRANSIENT` MUST NOT trigger blind write replay.

---

## 9. API-server baseline mappings

### 9.1 `BAD_REQUEST`

Do not classify from the code alone.

Inspect:

- endpoint;
- `invalidInputs`;
- structured type/name;
- current provider policy contract;
- message as supporting diagnostic evidence.

Typical mappings:

- missing required field / invalid enum / range / incompatible value -> `VALIDATION`;
- restricted tag/content/provider rule explicitly identified -> `POLICY`;
- temporary/provider error presented through an unexpected 400 shape -> `TRANSIENT` or `UNKNOWN` only with sufficient corroboration;
- unrecognized or contradictory details -> `UNKNOWN`.

The current product documentation explicitly warns that `invalidInputs` can be absent or insufficient and recommends using `message` to understand the error.

ICBM MAY use provider message text as diagnostic input.

ICBM MUST NOT use unversioned free-form Korean text alone as a high-confidence durable mapping unless the endpoint contract adopts that exact provider behavior.

### 9.2 `UNAUTHORIZED`

Candidate class:

`AUTH`

but only when endpoint/context evidence establishes an authentication/token cause.

If permission or application-state causes remain plausible:

`UNKNOWN`

### 9.3 `FORBIDDEN`

Candidate class:

`POLICY`

when the restriction/permission/account-state cause is positively identified.

Generic `FORBIDDEN` without enough evidence remains:

`UNKNOWN`

### 9.4 `NOT_FOUND`

API-server `NOT_FOUND` is domain/resource context, not the same as `GW.NOT_FOUND`.

Examples:

- caller supplied a nonexistent resource ID -> usually `VALIDATION` at the operation boundary;
- a previously known resource disappeared due to concurrent/external change -> potentially `CONFLICT`;
- a required post-CREATE read-back resource is unexpectedly absent -> `REVIEW_REQUIRED` or `UNKNOWN` until reconciliation completes.

No global `NOT_FOUND -> one class` mapping is allowed.

### 9.5 `INTERNAL_SERVER_ERROR`

Candidate class:

`TRANSIENT`

but mutating requests retain independent remote-outcome uncertainty.

Repeated deterministic failure with the same valid request must be escalated rather than retried indefinitely.

### 9.6 `PERMANENT_REDIRECT` / HTTP 308

Current product API documentation includes `308/PERMANENT_REDIRECT` in endpoint response sets.

Generic SmartStore infrastructure MUST NOT blindly auto-follow redirects for mutating requests.

For reads:

- a bounded redirect MAY be followed only when the endpoint contract explicitly permits it and the target is validated as an allowed NAVER host/path.

For writes:

- automatic redirect/replay is forbidden unless the adopted endpoint contract explicitly proves the redirect semantics safe for that endpoint;
- otherwise classify as `REVIEW_REQUIRED` or `FATAL` depending on whether this is expected provider migration vs proven local endpoint drift.

Redirect handling belongs in `ENDPOINT_MATRIX.md`, not in a permissive global HTTP-client switch.

### 9.7 HTTP 405 / 415 and equivalent request-contract failures

When ICBM itself generated an unsupported method or media type contrary to the frozen endpoint contract:

`error_class = FATAL`

because retrying the same request cannot succeed without a code/configuration correction.

### 9.8 HTTP 409 and domain conflicts

Generic 409 with documented conflict semantics maps to:

`CONFLICT`

If an endpoint/domain code positively identifies duplicate creation/resource identity:

`DUPLICATE`

takes precedence over generic `CONFLICT`.

Duplicate classification requires positive evidence; do not infer duplicate merely because a retry followed an uncertain write.

---

## 10. Product `BAD_REQUEST` structured evidence

For product registration/modification, `invalidInputs` is high-value evidence when present.

Typical validation-like types include examples such as:

- `NotEmpty`
- `NotValidEnum`
- `NumberMax`
- incompatible attribute/value constraints
- malformed/invalid resource values

These normally support:

`VALIDATION`

when they describe request data correctness.

A provider restriction type such as a restricted seller tag supports:

`POLICY`

when the provider explicitly identifies the value as disallowed by marketplace policy.

The classifier SHOULD preserve sanitized:

- field/name;
- type;
- message;

because two `BAD_REQUEST` responses can require different operator actions.

The classifier MUST NOT merely expose the raw provider message without mapping which ICBM field/validation owner should handle it.

That ownership mapping will be completed with the product endpoint/capability contract.

---

## 11. Authentication and permission interaction

`ERRORS.md` MUST NOT override the stronger contracts in:

- `AUTH.md`;
- `ACCOUNT_IDENTITY.md`;
- `PERMISSIONS_SCOPES.md`.

### 11.1 Auth recovery

If evidence proves an expired/invalid bearer token:

- classify `AUTH`;
- execute the bounded auth recovery contract;
- establish a new committed session generation;
- re-prove account identity.

For a protected read, the read may then be retried within the bounded policy.

For a write, successful auth recovery does NOT authorize automatic operation replay.

### 11.2 Permission failure

If current evidence positively proves a required API group is absent:

- classify the operation failure as `POLICY`;
- `write_scope -> MISSING` under `PERMISSIONS_SCOPES.md`;
- do not mutate merely to reconfirm the missing permission.

If `GW.AUTHN`/`FORBIDDEN` occurs but permission evidence is insufficient:

- preserve `write_scope=UNKNOWN` when appropriate;
- classify the failure as `UNKNOWN` until cause is separated.

### 11.3 Account mismatch

If the authenticated protected read proves a different `accountUid` than the canonical binding:

- this is the `AUTH_MISMATCH -> REVIEW_REQUIRED` contract from `ACCOUNT_IDENTITY.md`;
- do not reduce it to generic provider `AUTH`.

---

## 12. Retry policy for non-mutating reads

Reads can be retried more aggressively because they do not create marketplace mutations, but retries remain bounded.

Baseline:

- `TRANSIENT`
  - bounded retry with exponential/backoff policy and jitter;
- `RATE_LIMIT`
  - schedule according to rate/quota evidence; do not hot-loop;
- `AUTH`
  - one bounded auth recovery path per `AUTH.md`, then retry the read;
- `VALIDATION`
  - no automatic same-input retry;
- `POLICY`
  - no automatic same-input retry until policy/config evidence changes;
- `CONFLICT`
  - re-read current state before deciding;
- `DUPLICATE`
  - reconcile identities; no blind retry;
- `REVIEW_REQUIRED`
  - stop automatic execution;
- `FATAL`
  - stop until integration/configuration defect is corrected;
- `UNKNOWN`
  - default no blind retry; an endpoint-specific safe diagnostic retry may be allowed only by explicit policy.

Every automatic path has a finite retry budget.

---

## 13. Retry/replay policy for mutating requests

Mutating requests are governed by a stricter rule:

`error_class alone never authorizes replay`

### 13.1 When `remote_outcome=APPLIED_PROVEN`

Do not replay.

Proceed from the confirmed remote state.

### 13.2 When `remote_outcome=NOT_APPLIED_PROVEN`

A retry MAY be considered only if:

- the error class is retryable;
- the endpoint-specific retry/idempotency contract allows it;
- the retry budget permits it;
- all current auth/scope invariants still hold.

### 13.3 When `remote_outcome=UNKNOWN`

Generic automatic replay is forbidden.

Required sequence:

1. attempt the endpoint-specific read-back/reconciliation strategy;
2. search for the expected resource using stable request/business identity where the provider contract permits;
3. compare expected vs observed remote state;
4. if applied, mark `APPLIED_PROVEN` and continue without replay;
5. if non-application can be proven, mark `NOT_APPLIED_PROVEN` and apply the endpoint retry contract;
6. if still unresolved, escalate to safe human review.

A second CREATE issued only because the first timed out is specifically forbidden unless the write contract proves replay safety.

---

## 14. Transport-phase rules

Transport libraries may provide evidence about how far a request progressed.

ICBM SHOULD distinguish when observable:

- failure before connection/request transmission;
- failure while sending;
- failure after request transmission but before complete response;
- complete provider response received.

A connection/DNS/TLS failure that proves no request bytes reached the provider may support:

`remote_outcome=NOT_APPLIED_PROVEN`

for a write.

A timeout/reset after transmission begins must default to:

`remote_outcome=UNKNOWN`

unless endpoint/provider evidence proves otherwise.

Do not fabricate certainty when the HTTP client cannot expose the transmission boundary.

---

## 15. Rate-limit and quota handling

NAVER documents:

- `429/GW.RATE_LIMIT` for request-rate limits;
- token-bucket style per-API/application limits;
- response headers describing replenish rate, burst capacity, and remaining requests;
- `429/GW.QUOTA_LIMIT` for applicable longer-period quota limits.

ICBM SHALL record available rate/quota metadata and schedule retries rather than repeatedly testing the limit.

`RATE_LIMIT` is recoverable timing state, not credential invalidity.

Do not refresh tokens merely because a 429 occurred.

Do not convert a long-period quota limit into a one-second backoff loop.

For writes, rate-limit classification still does not bypass the remote-outcome/replay contract.

---

## 16. Redirect safety

ICBM SHALL disable or constrain generic automatic redirect behavior so that endpoint movement cannot silently change mutation targets.

A redirect target must be checked against:

- allowed NAVER host;
- adopted endpoint path/version;
- expected method preservation;
- endpoint-specific redirect policy.

Unexpected redirect on a mutating endpoint is a contract event, not merely an HTTP convenience.

A redirect response MUST NOT cause credentials or bodies to be forwarded to an unapproved host.

---

## 17. Unknown-code default

An unmapped provider code, undocumented status/code combination, malformed error body, or contradictory evidence SHALL default to:

`error_class = UNKNOWN`

not to the nearest convenient known class.

For UNKNOWN:

- preserve sanitized wire evidence;
- preserve trace ID;
- preserve endpoint/method/session generation;
- do not mutate durable auth/scope truth without corroboration;
- do not blindly retry destructive/mutating operations;
- attempt safe read-only diagnostics/read-back when available;
- escalate to `REVIEW_REQUIRED` operational handling if bounded diagnostics cannot resolve the cause.

The `error_class` may remain `UNKNOWN` while the workflow/action state becomes `REVIEW_REQUIRED`.

This distinction prevents human escalation from falsely claiming a root cause.

---

## 18. Classification changes require new evidence

ICBM MAY reclassify an error when new evidence arrives.

Examples:

- `GW.AUTHN / UNKNOWN` -> `AUTH` after proving the bearer token was expired;
- `GW.AUTHN / UNKNOWN` -> `POLICY` after proving the required API group was absent;
- `GW.IP_NOT_ALLOWED / UNKNOWN` -> `POLICY` after proving the current outbound IP is not configured;
- `BAD_REQUEST / UNKNOWN` -> `VALIDATION` after parsing a documented structural `invalidInputs` type;
- `409 / CONFLICT` -> `DUPLICATE` after an explicit duplicate domain code or read-back proves the same resource already exists.

Reclassification MUST append/retain the evidence trail rather than rewriting history as though the original uncertainty never existed.

---

## 19. Mapping priority and ownership

When multiple signals disagree, use this priority:

1. measured endpoint-specific remote/read-back fact;
2. exact current documented endpoint/domain code semantics;
3. exact current documented gateway semantics plus current auth/scope evidence;
4. structured provider fields (`invalidInputs`, domain data);
5. current sanitized provider message as diagnostic support;
6. historical support cases;
7. heuristic guess.

A lower-priority clue MUST NOT override stronger contradictory evidence.

Error classification owners SHALL be separated from business-data correction owners.

Examples:

- request schema/serialization defect -> integration/request-builder owner;
- product field validation -> product normalization/registration-field owner;
- auth/session defect -> auth owner;
- missing API group -> capability/permission owner;
- marketplace policy restriction -> policy/review owner;
- duplicate/conflict -> reconciliation owner.

This prevents a central error mapper from silently patching unrelated business data.

---

## 20. State convergence implications

The error mapper supplies evidence to capability/workflow state machines; it does not invent new state enums.

Baseline effects:

### `TRANSIENT`

- may remain automatically recoverable within retry budget;
- budget exhaustion must stop automatic retry and surface a safe human-visible state under the final M2 state contract.

### `RATE_LIMIT`

- schedule for later execution;
- do not mark auth/scope invalid;
- persistent unexpected quota behavior may require review.

### `AUTH`

- use `AUTH.md` recovery/state rules;
- repeated deterministic failure must not loop indefinitely.

### `VALIDATION`

- request cannot proceed unchanged;
- surface field-level actionable evidence when available.

### `POLICY`

- do not retry unchanged until provider/account/application/product policy condition changes;
- often requires operator action/review.

### `CONFLICT`

- re-read/reconcile current remote state before further mutation.

### `DUPLICATE`

- identify existing remote resource and use duplicate-conflict/reconciliation policy;
- never create another copy merely because the first attempt was uncertain.

### `REVIEW_REQUIRED`

- stop automatic mutation and preserve evidence for human decision.

### `FATAL`

- stop the affected capability until code/config/contract correction;
- do not burn retry budget on a deterministic integration defect.

### `UNKNOWN`

- preserve uncertainty;
- no destructive retry by default;
- use safe diagnostics/reconciliation;
- escalate if unresolved.

Final `PAUSED` reason-code mapping remains part of the M2 state-contract freeze rather than being invented here.

---

## 21. Evidence and logging safety

Provider errors can echo request-derived values.

Therefore sanitized logging is mandatory even for fields that appear inside an error response.

The system MUST redact or omit:

- credentials/tokens/signatures;
- complete client ID;
- customer/order PII not required for diagnosis;
- raw HTML/detail content;
- full image URLs when they contain sensitive query data;
- entire request payloads by default.

Safe diagnostics MAY include:

- endpoint identifier;
- method;
- HTTP status;
- provider code;
- sanitized message;
- safe `invalidInputs.name/type`;
- trace ID;
- timestamps;
- generation IDs;
- canonical error class;
- classification basis;
- remote outcome;
- retry/reconciliation decision.

Debugging must not turn failure evidence into a credential or PII leak.

---

## 22. Acceptance requirements

M2 SmartStore error handling acceptance MUST include at least the following.

### 22.1 `GW.AUTHN` — expired token

Using a safe expired/invalid token condition without corrupting durable credentials:

- receive `GW.AUTHN` or the measured provider auth failure;
- prove the auth cause using session/expiry evidence;
- classify `AUTH`;
- perform bounded auth recovery;
- for a protected read, retry only within the auth contract;
- do not generalize the mapping to every future `GW.AUTHN`.

### 22.2 `GW.AUTHN` — missing permission

Using controlled permission evidence or a safe harness:

- observe the same/similar authorization wire code;
- prove required API-group absence;
- classify `POLICY`;
- update scope state under `PERMISSIONS_SCOPES.md`;
- do not mislabel credentials invalid.

### 22.3 `GW.AUTHN` — malformed authorization header

Using a local/mocked request boundary rather than damaging real credentials:

- construct the malformed-header condition;
- verify the local request contract catches it where possible;
- if provider behavior is exercised safely, verify the failure is not persisted as credential invalidity;
- classify proven request-builder defect as `FATAL`.

### 22.4 Same cause / different wire representation

Test at least one permission-denial scenario represented as two different wire families in a controlled contract fixture, for example:

- gateway `401/GW.AUTHN`;
- API-server `403/FORBIDDEN`.

Verify both can converge to the same canonical `POLICY` class only when permission cause is positively evidenced.

### 22.5 `GW.IP_NOT_ALLOWED` evidence conflict

Test both:

- proven outbound-IP mismatch -> `POLICY`;
- current allow-list evidence matches but provider response contradicts it -> `UNKNOWN` rather than overwriting config truth.

### 22.6 Product structural validation

Use representative documented `BAD_REQUEST/invalidInputs` fixtures such as:

- missing required field;
- invalid enum;
- numeric maximum;
- incompatible attribute combination.

Verify classification:

`VALIDATION`

and no unchanged automatic retry.

### 22.7 Product policy restriction through `BAD_REQUEST`

Use a controlled restricted-value fixture such as restricted seller-tag semantics.

Verify:

`BAD_REQUEST` does not force `VALIDATION`;

provider policy evidence maps to:

`POLICY`.

### 22.8 Unknown provider code

Inject an unrecognized provider code/status combination.

Verify:

- `error_class=UNKNOWN`;
- raw/sanitized evidence retained;
- no durable auth/scope rewrite;
- no automatic mutating replay.

### 22.9 Rate limit

Use a controlled 429 fixture and, when safe, measured provider behavior.

Verify:

- `RATE_LIMIT` classification;
- rate headers captured when present;
- bounded/scheduled retry;
- no token refresh merely because of 429;
- no hot loop.

### 22.10 Quota limit

Use a controlled `GW.QUOTA_LIMIT` fixture.

Verify longer-period quota semantics are not treated like a one-second rate limit.

### 22.11 Read transient recovery

Inject gateway/server timeout/5xx for a protected read.

Verify bounded `TRANSIENT` retry and retry-budget enforcement.

### 22.12 Write timeout uncertainty

For a mutating request, inject timeout/connection loss after request transmission may have occurred.

Verify:

- `error_class=TRANSIENT` may be retained;
- `remote_outcome=UNKNOWN`;
- no blind replay;
- read-back/reconciliation executes first;
- unresolved result escalates safely.

### 22.13 Proven pre-send transport failure

Inject a transport failure where the harness proves no request was transmitted.

Verify a mutating attempt may establish:

`remote_outcome=NOT_APPLIED_PROVEN`

without falsely claiming provider rejection.

### 22.14 Conflict vs duplicate

Use fixtures for:

- generic 409 conflict;
- explicit duplicate domain evidence.

Verify:

- generic conflict -> `CONFLICT`;
- explicit duplicate -> `DUPLICATE`;
- duplicate is not inferred solely from prior timeout/retry history.

### 22.15 Resource not found contexts

Verify distinct handling for:

- gateway `GW.NOT_FOUND` route failure;
- API-server missing input resource;
- missing post-write read-back resource.

They MUST NOT share one unconditional mapping.

### 22.16 Redirect safety

For a mutating request receiving 308:

- generic client does not silently follow/replay;
- redirect target is validated against endpoint policy;
- no credentials/body forwarded to an unapproved host.

### 22.17 2xx semantic failure/partial result

Use a fixture representing an endpoint where HTTP success does not mean every item/async operation succeeded.

Verify endpoint result parsing can still produce canonical failures and does not stop at HTTP status.

### 22.18 Trace/evidence capture

For gateway and API-server error fixtures:

- capture trace ID when present;
- preserve provider timestamp/code/status;
- sanitize sensitive values;
- link the evidence to the correct session generation and operation attempt.

### 22.19 Classification re-evaluation

Begin with `UNKNOWN`, then add corroborating evidence.

Verify reclassification appends evidence and preserves the original uncertainty event rather than rewriting history.

---

## 23. Runtime evidence required before `verified_at`

`verified_at` MUST remain null until measured evidence covers at least:

- current gateway error body/trace-ID shape;
- one real/safe authentication failure and recovery;
- one real/safe rate-limit or provider-approved equivalent observation, where feasible without abuse;
- real product `BAD_REQUEST` structured evidence from a non-destructive validation test or previously captured sanitized provider response;
- `GW.AUTHN` ambiguity handling;
- current `GW.IP_NOT_ALLOWED` behavior or an explicitly documented unmeasured limitation;
- mutating timeout/reconciliation behavior in a controlled boundary;
- unknown-code default behavior;
- sensitive-data sanitization;
- current product endpoint redirect behavior or an explicitly documented unresolved limitation.

Live destructive errors MUST NOT be manufactured merely to complete this document.

Controlled harnesses/mocks are preferred for unsafe failure injection.

---

## 24. Open questions

### Q1. Exact API error when the 180-day own-store application re-authentication expires

`AUTH.md` records this provider lifecycle, but the exact machine-observable failure contract is still unknown.

Current status:

`UNKNOWN`

Do not map it to `AUTH`, `POLICY`, or `ACCOUNT_RESTRICTED` until measured/documented.

### Q2. Does every pre-service gateway rejection guarantee no mutation reached the target service?

The gateway documentation names causes but does not establish a universal transaction/execution guarantee for all gateway error codes.

Current status:

`DO_NOT_ASSUME`

Write replay therefore continues to use the remote-outcome/idempotency/read-back contract.

### Q3. Exact redirect behavior for adopted M2 product endpoints

Current product docs list `308/PERMANENT_REDIRECT`, but the M2 endpoint matrix must establish whether/when a redirect is expected and which target is safe.

Current status:

`PENDING_ENDPOINT_MATRIX_AND_RUNTIME_EVIDENCE`

### Q4. Endpoint-specific product/domain error catalog

Generic `BAD_REQUEST/UNAUTHORIZED/FORBIDDEN/NOT_FOUND/INTERNAL_SERVER_ERROR` is insufficient for a complete registration diagnosis.

Current status:

`PARTIAL`

Complete only for endpoints adopted in M2.

### Q5. Read-back consistency window after product mutation

A missing immediate read-back might mean non-application, propagation delay, or wrong identity/endpoint.

Current status:

`UNKNOWN`

M5 write proof must measure and define the bounded reconciliation/read-back window before using absence as proof of non-application.

### Q6. Maximum automatic retry budgets by class/endpoint

The architectural rule is bounded retry, but concrete budgets should be frozen with the M2 state/operation policy and endpoint characteristics.

Current status:

`POLICY_PENDING`

---

## 25. Final M2 error contract

For SmartStore:

`HTTP status != canonical error class`

`provider code != root cause`

`same provider code may map to different canonical classes`

`same root cause may arrive through different provider codes/statuses`

`2xx != always semantic operation success`

`BAD_REQUEST != always VALIDATION`

`GW.AUTHN != always AUTH`

`GW.IP_NOT_ALLOWED != always proven POLICY`

`429/GW.RATE_LIMIT or GW.QUOTA_LIMIT = RATE_LIMIT`

`unmapped or contradictory evidence = UNKNOWN`

`UNKNOWN != permission to guess`

`error_class != replay permission`

`TRANSIENT write failure + remote_outcome UNKNOWN != safe retry`

`mutating replay requires endpoint-specific idempotency/read-back proof`

`provider trace/evidence must be preserved safely`

`classification may strengthen only when new evidence arrives`

`final write truth comes from bounded mutation + external read-back, not from error-code optimism`

All unknown provider behavior remains UNKNOWN until measured or officially documented.
