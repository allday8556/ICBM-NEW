# SmartStore Error Classification Contract

## Status

- Provider: NAVER SmartStore / Commerce API
- Contract status: `DOCUMENTED_BASELINE_WITH_ENDPOINT_GAPS`
- M2 integration mode: `OWN_STORE_SELF`
- Canonical error classes: the ADR-0008 aligned taxonomy only (no SmartStore-specific classes)
- Runtime verification: `PENDING`
- Endpoint-specific domain mapping: `PARTIAL / PENDING ENDPOINT_MATRIX`
- Mutation outcome model: `APPLIED_PROVEN / NOT_APPLIED_PROVEN / UNKNOWN`

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

Implementation-library reference, only if ICBM adopts HTTPX:

- https://www.python-httpx.org/quickstart/
  - redirects are not followed by default;
- https://github.com/encode/httpx/blob/master/httpx/_client.py
  - current redirect implementation preserves the method/body for 307/308 while 301/302/303 may change method under defined conditions.

Freshness rule:

- Any upstream Commerce API version change, gateway error-table change, endpoint response-schema change, error-code semantic change, permission-error change, retry guidance change, redirect behavior change, or adopted HTTP-client redirect behavior change SHALL trigger immediate review.
- `review_due` is a fallback calendar bound when automated change detection is absent or broken.
- Provider documentation describes known error shapes; it does not prove that every real failure will match a known mapping.
- Unknown or contradictory behavior remains `UNKNOWN` until measured or officially documented.

---

## 1. Purpose

This document maps SmartStore provider/transport failures into the canonical ICBM error classes of `docs/adr/0008-error-taxonomy-alignment.md` without inventing marketplace-specific global error classes.

The canonical classes this document selects from are:

- `TRANSIENT`
- `RATE_LIMITED`
- `AUTH`
- `VALIDATION`
- `POLICY_BLOCKED`
- `CONFLICT`
- `DUPLICATE`
- `REVIEW_REQUIRED`
- `FATAL`
- `UNKNOWN`

`RATE_LIMITED` and `POLICY_BLOCKED` are the persisted spellings of the Canonical v3.1 §11.3 classes `RATE_LIMIT` and `POLICY` (ADR-0008, decision A). `CONFLICT`, `DUPLICATE`, `REVIEW_REQUIRED` and `FATAL` may be emitted only once the ADR-0008 implementation stage has added them to the runtime enum; until then an adapter MUST NOT emit them or substitute an older class for them (ADR-0008, staging).

`NOT_FOUND` also remains a canonical class (ADR-0008, decision C), but it is never the classification of a SmartStore provider not-found condition: see §9.3 and §10.4.

This document does not redefine those classes.

It defines the evidence required to select one of them for SmartStore.

The core safety rules are:

`provider code != root cause`

`HTTP status != canonical error class`

`error class != replay permission`

`retry-budget exhaustion != automatic error-class reclassification`

A correct classification does not by itself authorize a retry of a mutating request.

---

## 2. Independent axes: cause, remote outcome, workflow action

Every failed or ambiguous SmartStore operation SHALL keep three concepts separate.

### 2.1 Cause classification

Represented by:

`error_class`

using the canonical ICBM classes above.

### 2.2 Remote mutation outcome

For mutating operations ICBM SHALL separately track:

`remote_outcome in {APPLIED_PROVEN, NOT_APPLIED_PROVEN, UNKNOWN}`

These are not new `error_class` values.

They describe evidence about whether the remote mutation happened.

Examples:

- a network timeout after a POST may be `error_class=TRANSIENT` while `remote_outcome=UNKNOWN`;
- a deterministic local validation failure before transport handoff may be `error_class=VALIDATION` with `remote_outcome=NOT_APPLIED_PROVEN`;
- a successful CREATE followed by matching external read-back may establish `remote_outcome=APPLIED_PROVEN`.

ICBM MUST NOT infer:

`TRANSIENT -> safe to replay`

or:

`failure response -> definitely not applied`

without operation-specific evidence.

### 2.3 Workflow/action state

Workflow state describes what automation may do next.

Examples include continuing automatically, pausing, reconciling, or escalating to human review.

Workflow state MUST NOT be treated as a synonym for `error_class`.

The following is a normal combination:

`error_class=UNKNOWN`

`workflow_state=REVIEW_REQUIRED`

It means the root cause remains unknown while automation has safely stopped for human review.

### 2.4 Meaning of canonical `error_class=REVIEW_REQUIRED`

The canonical class name `REVIEW_REQUIRED` also exists on the cause axis and can therefore be confused with the workflow state of the same name.

For this document:

- `error_class=REVIEW_REQUIRED` is reserved for a provider/domain/endpoint condition whose semantics themselves positively establish that human judgment or manual resolution is required and no narrower canonical cause class applies;
- `workflow_state=REVIEW_REQUIRED` means automation has stopped and escalated to a human, regardless of the underlying cause class.

M2 currently has **no generic SmartStore wire-code baseline that is automatically mapped to `error_class=REVIEW_REQUIRED`**.

Therefore unresolved diagnostics normally preserve the measured class, often `UNKNOWN` or `TRANSIENT`, while workflow state may become `REVIEW_REQUIRED`.

Do not manufacture `error_class=REVIEW_REQUIRED` merely because a retry budget was exhausted or a person needs to look at the problem.

---

## 3. Relationship to `RegistrationAttempt.ambiguous_result`

Canonical registration state already contains `RegistrationAttempt.ambiguous_result`.

M2 SHALL treat that boolean as a compatibility projection of the richer `remote_outcome` evidence axis, not as an independent truth field.

Until the canonical schema is explicitly revised:

`RegistrationAttempt.ambiguous_result = (remote_outcome == UNKNOWN)`

Therefore:

- `APPLIED_PROVEN` -> `ambiguous_result=false`;
- `NOT_APPLIED_PROVEN` -> `ambiguous_result=false`;
- `UNKNOWN` -> `ambiguous_result=true`.

The boolean intentionally cannot distinguish applied from proven-not-applied; `remote_outcome` carries that distinction.

Implementations MUST NOT update `ambiguous_result` and `remote_outcome` independently in ways that can disagree.

If a later ADR promotes `remote_outcome` into the canonical persisted registration schema, it should supersede or derive the boolean rather than creating two writable sources of truth.

This document does not itself modify the canonical schema.

---

## 4. Why code-to-cause tables are insufficient

SmartStore error handling is many-to-many.

### 4.1 One provider code can represent multiple causes

`GW.AUTHN` is the clearest example.

Official provider materials show `GW.AUTHN` associated with at least:

- expired bearer token;
- missing API-group permission;
- malformed or invalid `Authorization` header.

Therefore:

`GW.AUTHN != AUTH`

as a universal mapping.

The surrounding evidence determines whether the resulting ICBM class is `AUTH`, `POLICY_BLOCKED`, `FATAL`, or `UNKNOWN`.

Likewise, `GW.IP_NOT_ALLOWED` is documented as an IP allow-list failure, but official support has also confirmed a case where the same configured outbound IP intermittently received the error due to a temporary provider-side condition.

Therefore:

`GW.IP_NOT_ALLOWED != always POLICY_BLOCKED`

without corroborating current IP/configuration evidence.

### 4.2 One root cause can surface through different codes/statuses

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

### 4.3 One HTTP/code family can contain different ICBM classes

Product APIs use `400/BAD_REQUEST` broadly.

Structured `invalidInputs` can indicate:

- structural/input validation such as missing required fields, invalid enum values, numeric range errors, or incompatible values -> usually `VALIDATION`;
- provider policy such as restricted seller tags -> `POLICY_BLOCKED` when the restriction is positively identified;
- undocumented/provider temporary conditions -> potentially `TRANSIENT` or `UNKNOWN` depending on evidence.

Therefore:

`BAD_REQUEST != always VALIDATION`

### 4.4 HTTP success does not equal semantic success

Some Commerce API operations can return per-item failures or asynchronous acceptance semantics.

Therefore generic infrastructure MUST NOT assume:

`2xx -> semantic success`

Every endpoint adopted by ICBM MUST define a machine-checkable `success_predicate` in `ENDPOINT_MATRIX.md` or an endpoint-specific contract referenced by that matrix.

The predicate must define whether success means:

- completed operation;
- accepted/queued operation;
- partially successful batch;
- response requiring a later result/read-back check;
- another explicit endpoint-specific condition.

**An endpoint with no frozen success predicate is not eligible for M2/M5 adoption.**

For every response, including 2xx:

1. parse the endpoint response under the frozen schema;
2. evaluate the endpoint `success_predicate`;
3. only then declare semantic operation success.

If the response cannot be parsed/evaluated safely:

- do not treat HTTP success as business success;
- classify the schema/contract uncertainty under the evidence rules below;
- for a mutation, preserve `remote_outcome=UNKNOWN` unless read-back proves otherwise.

This makes `OPERATION_RESULT` evaluation a mandatory success gate, not an optional error-handling afterthought.

---

## 5. Response/failure layers

ICBM SHALL identify the layer before assigning a final error class.

### 5.1 `TRANSPORT`

No trustworthy provider response was obtained.

Examples:

- DNS resolution failure;
- TCP connection-establishment failure;
- TLS handshake failure;
- client-side timeout;
- connection reset before a complete provider response;
- malformed/truncated response that cannot be safely interpreted.

Transport failures are often `TRANSIENT` candidates, but write outcome may still be `UNKNOWN` if the request could have reached NAVER.

### 5.2 `GATEWAY`

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

### 5.3 `API_SERVER_STANDARD`

The target Commerce API server returned a normal endpoint error shape such as:

- `BAD_REQUEST`
- `UNAUTHORIZED`
- `FORBIDDEN`
- `NOT_FOUND`
- `INTERNAL_SERVER_ERROR`
- `PERMANENT_REDIRECT`

These names are still not sufficient by themselves for all final ICBM classification decisions.

### 5.4 `API_SERVER_DOMAIN`

The endpoint returned a documented domain-specific error code.

Endpoint-specific domain codes can provide stronger evidence than generic HTTP status.

M2 SHALL adopt only domain codes belonging to endpoints actually present in `ENDPOINT_MATRIX.md`.

### 5.5 `OPERATION_RESULT`

A transport/HTTP request succeeded but the parsed operation result contains nested, item-level, asynchronous, partial, or read-back failure information.

This layer participates in the mandatory endpoint `success_predicate` evaluation for every response, including 2xx.

### 5.6 `LOCAL_CONTRACT`

ICBM detected a local protocol or invariant failure before provider truth could be trusted.

Examples:

- wrong method/path generated despite current endpoint contract;
- unsupported content type generated by ICBM;
- malformed authorization header constructed locally;
- response schema drift that the parser cannot safely interpret;
- mismatched credential/session generation;
- forbidden automatic redirect/replay path;
- an adopted endpoint missing a success predicate.

This layer often results in `FATAL` or `UNKNOWN`, depending on whether the defect is proven.

Human escalation is represented separately by workflow state.

---

## 6. Required error evidence record

Every material provider/transport failure SHOULD produce a sanitized evidence record sufficient to reproduce the classification decision.

At minimum record:

- provider=`SMARTSTORE`;
- internal `marketplace_account_id`;
- endpoint identifier from the adopted endpoint matrix;
- HTTP method;
- whether the operation is mutating;
- local attempt/request ID;
- registration attempt reference when applicable;
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
- endpoint success-predicate revision;
- `error_class`;
- classification basis;
- `remote_outcome` for mutating operations;
- derived `ambiguous_result` when applicable;
- retry/reconciliation decision;
- workflow/action state when escalated;
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

## 7. Classification basis

Error classification SHALL carry provenance/strength metadata separate from `error_class`.

Recommended values:

- `DOCUMENTED_EXACT`
  - current official documentation uniquely maps the observed endpoint/code/context to the class.
- `DOCUMENTED_CONTEXTUAL`
  - official documentation/support establishes the mapping only after additional evidence is considered.
- `ENDPOINT_SPECIFIC`
  - the adopted endpoint contract supplies the mapping.
- `MEASURED_RECONCILIATION`
  - external read-back/reconciliation supplies stronger operation-outcome evidence.
- `HEURISTIC`
  - a non-authoritative diagnostic hint exists but is not sufficient for durable truth.
- `UNKNOWN`
  - evidence is insufficient or contradictory.

These are evidence-strength descriptors, not additional canonical error classes.

A heuristic MUST NOT silently become durable provider truth.

---

## 8. Default response/classification algorithm

The algorithm runs for **every provider response**, including 2xx, and for transport failures.

### Step 0: require an adopted endpoint contract

Before sending or interpreting the request, the endpoint MUST have in `ENDPOINT_MATRIX.md`:

- method/path;
- mutability;
- auth mode;
- required API groups;
- application-mode eligibility;
- response schema/reference;
- success predicate;
- redirect policy;
- idempotency/replay policy;
- reconciliation/read-back strategy for mutations where applicable.

If required contract data is absent, the endpoint is not eligible for automatic execution.

### Step 1: determine whether a trustworthy provider response exists

If no:

- classify the failure layer as `TRANSPORT`;
- preserve transport-phase evidence when trustworthy;
- for writes, do not assume non-execution merely because no response arrived.

### Step 2: parse and evaluate semantic success

If a response exists:

- identify gateway vs API-server response;
- parse under the endpoint contract;
- evaluate the endpoint `success_predicate` even when HTTP status is 2xx;
- inspect nested/async/per-item operation result when defined.

Only a passed predicate may become semantic success.

### Step 3: validate the request/endpoint contract

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

1. measured endpoint-specific remote/read-back fact;
2. endpoint-specific documented code/semantic result;
3. gateway documented code;
4. structured fields such as `invalidInputs.type`;
5. current `AUTH.md` evidence;
6. current `PERMISSIONS_SCOPES.md` evidence;
7. provider message only as supporting diagnostic evidence.

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

### Step 7: derive compatibility ambiguity state

When a `RegistrationAttempt` exists:

`ambiguous_result = (remote_outcome == UNKNOWN)`

No independent conflicting write to the boolean is permitted.

### Step 8: select retry/reconciliation/workflow action

The action depends on:

- operation mutability;
- error class;
- remote outcome;
- endpoint idempotency/read-back contract;
- retry budget;
- provider rate-limit guidance;
- current auth/scope invariants.

---

## 9. Gateway baseline mappings

The following are M2 baseline rules, subject to endpoint/context evidence.

### 9.1 `GW.AUTHN`

Default:

`error_class = UNKNOWN`

until the cause is corroborated.

Possible contextual mappings include:

- `AUTH`
  - current token is locally expired or provider support/endpoint evidence uniquely confirms token invalidity/expiry;
- `POLICY_BLOCKED`
  - current provider permission evidence proves the required API group is absent;
- `FATAL`
  - ICBM constructed a malformed authorization header or otherwise violated a frozen request contract;
- `UNKNOWN`
  - no evidence cleanly separates auth, permission, formatting, route, or provider state.

Generic auth recovery follows `AUTH.md` and MUST remain bounded.

A `GW.AUTHN` from a write does not authorize blind replay after token recovery.

### 9.2 `GW.IP_NOT_ALLOWED`

If current observed outbound IP is positively known to be absent from the provider allow list:

`error_class = POLICY_BLOCKED`

If provider configuration says the IP should be allowed and behavior is intermittent/contradictory:

`error_class = UNKNOWN`

or `TRANSIENT` only when current provider evidence establishes a temporary condition.

Do not rewrite application IP configuration automatically from the error response.

### 9.3 `GW.NOT_FOUND`

If ICBM generated a route that contradicts the current adopted endpoint contract:

`error_class = FATAL`

If ICBM used the current documented route and the gateway still reports not found:

`error_class = UNKNOWN`

pending contract/provider drift investigation.

This is different from an API-server `NOT_FOUND` for a domain resource.

### 9.4 `GW.RATE_LIMIT`

`error_class = RATE_LIMITED`

Retry must be scheduled/backed off; hot-loop retry is forbidden.

### 9.5 `GW.QUOTA_LIMIT`

`error_class = RATE_LIMITED`

Quota exhaustion is distinct from per-second rate limiting but belongs to the same canonical ICBM error class.

The retry horizon may be much longer and must use provider quota-period evidence when available rather than the short rate-limit backoff policy.

### 9.6 `GW.PROXY.*`, `GW.INTERNAL_SERVER_ERROR`, `GW.BLOCK.*`, `GW.TIMEOUT.*`

These are `TRANSIENT` candidates because the documented causes include gateway/service/network failure, circuit open, maintenance, and timeout.

However:

- a mutating request may have `remote_outcome=UNKNOWN`;
- `TRANSIENT` MUST NOT trigger blind write replay;
- repeated failure consumes the bounded retry budget but **does not by count alone change the error class**.

If retry budget is exhausted with no new causal evidence:

- keep `error_class=TRANSIENT`;
- stop automatic retry;
- surface the final workflow state defined by the M2 state contract, commonly human review/pause.

Reclassification to `FATAL`, `POLICY_BLOCKED`, or another class requires new evidence proving that class; frequency alone is not proof.

---

## 10. API-server baseline mappings

### 10.1 `BAD_REQUEST`

Do not classify from the code alone.

Inspect:

- endpoint;
- `invalidInputs`;
- structured type/name;
- current provider policy contract;
- message as supporting diagnostic evidence.

Typical mappings:

- missing required field / invalid enum / range / incompatible value -> `VALIDATION`;
- restricted tag/content/provider rule explicitly identified -> `POLICY_BLOCKED`;
- temporary/provider error presented through an unexpected 400 shape -> `TRANSIENT` or `UNKNOWN` only with sufficient corroboration;
- unrecognized or contradictory details -> `UNKNOWN`.

The current product documentation explicitly warns that `invalidInputs` can be absent or insufficient and recommends using `message` to understand the error.

ICBM MAY use provider message text as diagnostic input.

ICBM MUST NOT use unversioned free-form Korean text alone as a high-confidence durable mapping unless the endpoint contract adopts that exact provider behavior.

### 10.2 `UNAUTHORIZED`

Candidate class:

`AUTH`

but only when endpoint/context evidence establishes an authentication/token cause.

If permission or application-state causes remain plausible:

`UNKNOWN`

### 10.3 `FORBIDDEN`

Candidate class:

`POLICY_BLOCKED`

when the restriction/permission/account-state cause is positively identified.

Generic `FORBIDDEN` without enough evidence remains:

`UNKNOWN`

### 10.4 `NOT_FOUND`

API-server `NOT_FOUND` is domain/resource context, not the same as `GW.NOT_FOUND`.

Examples:

- caller supplied a nonexistent resource ID -> usually `VALIDATION` at the operation boundary;
- a previously known resource disappeared due to concurrent/external change -> potentially `CONFLICT`;
- a required post-CREATE read-back resource is unexpectedly absent -> keep cause/outcome uncertain until reconciliation completes.

A missing read-back resource does not by itself prove `NOT_APPLIED_PROVEN`; consistency timing must be handled by the M5 reconciliation contract.

No global `NOT_FOUND -> one class` mapping is allowed.

### 10.5 `INTERNAL_SERVER_ERROR`

Candidate class:

`TRANSIENT`

but mutating requests retain independent remote-outcome uncertainty.

Repeated deterministic failure stops at the bounded retry budget.

It becomes another class only if new evidence proves a different cause.

### 10.6 `PERMANENT_REDIRECT` / HTTP 308

Current product API documentation includes `308/PERMANENT_REDIRECT` in endpoint response sets.

HTTP 308 is materially dangerous for mutations because redirect semantics preserve the original HTTP method and request body. An automatic redirect can therefore transmit the same CREATE/UPDATE request body to the redirect target.

Generic SmartStore infrastructure MUST NOT blindly auto-follow redirects for mutating requests.

For reads:

- a bounded redirect MAY be followed only when the endpoint contract explicitly permits it and the target is validated as an allowed NAVER host/path.

For writes:

- generic automatic redirect is forbidden;
- same-origin target alone is not sufficient to make replay safe;
- endpoint contract must explicitly define whether the 308 is expected, the exact allowed target pattern, and whether replay preserves operation safety;
- absent that explicit contract, preserve `remote_outcome=UNKNOWN` if prior application cannot be excluded and stop for reconciliation/review.

Redirect handling belongs in `ENDPOINT_MATRIX.md`, not in a permissive global HTTP-client switch.

Implementation guard:

- the SmartStore adapter/client configuration MUST keep automatic redirect following disabled for mutating requests;
- repository/static tests SHOULD reject a generic redirect-following configuration in the SmartStore mutation path;
- if ICBM uses HTTPX, `follow_redirects=True` MUST NOT be enabled for the generic SmartStore mutation client; any endpoint-specific redirect handling must occur in explicit adapter logic after target validation.

### 10.7 HTTP 405 / 415 and equivalent request-contract failures

When ICBM itself generated an unsupported method or media type contrary to the frozen endpoint contract:

`error_class = FATAL`

because retrying the same request cannot succeed without a code/configuration correction.

### 10.8 HTTP 409 and domain conflicts

Generic 409 with documented conflict semantics maps to:

`CONFLICT`

If an endpoint/domain code positively identifies duplicate creation/resource identity:

`DUPLICATE`

takes precedence over generic `CONFLICT`.

Duplicate classification requires positive evidence; do not infer duplicate merely because a retry followed an uncertain write.

---

## 11. Product `BAD_REQUEST` structured evidence

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

`POLICY_BLOCKED`

when the provider explicitly identifies the value as disallowed by marketplace policy.

The classifier SHOULD preserve sanitized:

- field/name;
- type;
- message;

because two `BAD_REQUEST` responses can require different operator actions.

The classifier MUST NOT merely expose the raw provider message without mapping which ICBM field/validation owner should handle it.

That ownership mapping will be completed with the product endpoint/capability contract.

---

## 12. Authentication and permission interaction

`ERRORS.md` MUST NOT override the stronger contracts in:

- `AUTH.md`;
- `ACCOUNT_IDENTITY.md`;
- `PERMISSIONS_SCOPES.md`.

### 12.1 Auth recovery

If evidence proves an expired/invalid bearer token:

- classify `AUTH`;
- execute the bounded auth recovery contract;
- establish a new committed session generation;
- re-prove account identity.

For a protected read, the read may then be retried within the bounded policy.

For a write, successful auth recovery does NOT authorize automatic operation replay.

### 12.2 Permission failure

If current evidence positively proves a required API group is absent:

- classify the operation failure as `POLICY_BLOCKED`;
- `write_scope -> MISSING` under `PERMISSIONS_SCOPES.md`;
- do not mutate merely to reconfirm the missing permission.

If `GW.AUTHN`/`FORBIDDEN` occurs but permission evidence is insufficient:

- preserve `write_scope=UNKNOWN` when appropriate;
- classify the failure as `UNKNOWN` until cause is separated.

### 12.3 Account mismatch

If the authenticated protected read proves a different `accountUid` than the canonical binding:

- this is the `AUTH_MISMATCH -> REVIEW_REQUIRED` workflow contract from `ACCOUNT_IDENTITY.md`;
- do not reduce it to generic provider `AUTH`;
- do not infer that the canonical `error_class` must also equal `REVIEW_REQUIRED` unless the state contract explicitly establishes that mapping.

---

## 13. Retry policy for non-mutating reads

Reads can be retried more aggressively because they do not create marketplace mutations, but retries remain bounded.

Baseline:

- `TRANSIENT`
  - bounded retry with backoff and jitter;
- `RATE_LIMITED`
  - schedule according to rate/quota evidence; do not hot-loop;
- `AUTH`
  - one bounded auth recovery path per `AUTH.md`, then retry the read;
- `VALIDATION`
  - no automatic same-input retry;
- `POLICY_BLOCKED`
  - no automatic same-input retry until policy/config evidence changes;
- `CONFLICT`
  - re-read current state before deciding;
- `DUPLICATE`
  - reconcile identities; no blind retry;
- `REVIEW_REQUIRED`
  - only when this is genuinely the cause class under an endpoint/domain contract; automatic execution stops;
- `FATAL`
  - stop until integration/configuration defect is corrected;
- `UNKNOWN`
  - default no blind retry; an endpoint-specific safe diagnostic retry may be allowed only by explicit policy.

Every automatic path has a finite retry budget.

Budget exhaustion stops automation but does not itself rewrite the error class.

---

## 14. Retry/replay policy for mutating requests

Mutating requests are governed by a stricter rule:

`error_class alone never authorizes replay`

### 14.1 When `remote_outcome=APPLIED_PROVEN`

Do not replay.

Proceed from the confirmed remote state.

### 14.2 When `remote_outcome=NOT_APPLIED_PROVEN`

A retry MAY be considered only if:

- the error class is retryable;
- the endpoint-specific retry/idempotency contract allows it;
- the retry budget permits it;
- all current auth/scope invariants still hold.

`NOT_APPLIED_PROVEN` itself does not mean the request should automatically be retried.

### 14.3 When `remote_outcome=UNKNOWN`

Generic automatic replay is forbidden.

Required sequence:

1. attempt the endpoint-specific read-back/reconciliation strategy;
2. search for the expected resource using stable request/business identity where the provider contract permits;
3. compare expected vs observed remote state;
4. if applied, mark `APPLIED_PROVEN` and continue without replay;
5. if non-application can be positively proven, mark `NOT_APPLIED_PROVEN` and apply the endpoint retry contract;
6. if still unresolved, keep `error_class` unchanged and escalate workflow state safely.

A second CREATE issued only because the first timed out is specifically forbidden unless the write contract proves replay safety.

---

## 15. `NOT_APPLIED_PROVEN` is whitelist-only

`NOT_APPLIED_PROVEN` is a high-confidence safety claim.

It MUST NOT be assigned merely because an exception name sounds like a connection failure.

The runtime may assign `NOT_APPLIED_PROVEN` only through an explicitly reviewed whitelist whose instrumentation proves the request could not have reached the provider application layer.

### 15.1 Baseline whitelist

The following may support `NOT_APPLIED_PROVEN` when the stated boundary is positively observed:

1. **Local pre-submit rejection**
   - request failed local schema/invariant validation before being handed to the network transport.

2. **DNS resolution failure**
   - provider host was not resolved and no connection to the provider/proxy path was established for this attempt.

3. **TCP connect failure before connection establishment**
   - a new connection attempt failed before TCP establishment;
   - no existing/reused connection was involved.

4. **TLS handshake failure before HTTP request transmission**
   - a new connection completed TCP but failed TLS negotiation before application HTTP request bytes could be sent;
   - instrumentation must positively identify this phase.

A proxy/tunnel topology requires equivalent evidence about the provider-bound application request, not merely a generic client exception name.

### 15.2 Explicit non-whitelist / UNKNOWN cases

The following default to:

`remote_outcome=UNKNOWN`

for mutating requests unless stronger endpoint/read-back evidence later proves otherwise:

- any read timeout;
- any write timeout after transport handoff;
- generic request timeout with unknown phase;
- `ConnectionResetError` or equivalent reset after a connection exists;
- send/write exception where partial transmission cannot be excluded;
- response-read failure after sending;
- truncated/malformed provider response;
- any failure on a pooled/reused connection where the library does not prove the current request transmitted zero bytes;
- HTTP/2 stream reset after request initiation;
- proxy error after the provider-bound request may have been forwarded;
- cancellation after transport handoff;
- any unfamiliar transport exception not explicitly in the reviewed whitelist.

**A pooled-connection failure is never promoted to `NOT_APPLIED_PROVEN` from the exception type alone.**

### 15.3 Harness evidence vs production observability

A test harness can prove a no-send condition by construction.

That does not mean the production HTTP stack exposes the same boundary.

Acceptance therefore has two separate obligations:

- test the logical whitelist with controlled fixtures;
- verify which whitelist predicates the actual production transport can observe reliably.

If production instrumentation cannot establish a whitelist predicate:

`remote_outcome=UNKNOWN`

is mandatory.

### 15.4 Reconciliation can strengthen outcome later

A transport failure initially marked `UNKNOWN` may later become:

- `APPLIED_PROVEN` through positive external read-back; or
- `NOT_APPLIED_PROVEN` only through an endpoint-specific reconciliation rule that positively proves non-application.

Simple immediate absence is insufficient unless M5 has already frozen the endpoint's bounded consistency/read-back window.

---

## 16. Rate-limit and quota handling

NAVER documents:

- `429/GW.RATE_LIMIT` for request-rate limits;
- token-bucket style per-API/application limits;
- response headers describing replenish rate, burst capacity, and remaining requests;
- `429/GW.QUOTA_LIMIT` for applicable longer-period quota limits.

ICBM SHALL record available rate/quota metadata and schedule retries rather than repeatedly testing the limit.

`RATE_LIMITED` is recoverable timing state, not credential invalidity.

Do not refresh tokens merely because a 429 occurred.

Do not convert a long-period quota limit into a one-second backoff loop.

For writes, rate-limit classification still does not bypass the remote-outcome/replay contract.

---

## 17. Redirect safety

ICBM SHALL disable generic automatic redirect behavior for SmartStore mutation paths so endpoint movement cannot silently replay mutation bodies.

### 17.1 Why HTTP 308 is specifically dangerous

HTTP 308 preserves the original HTTP method and request body when the redirect is followed.

Therefore a redirected product CREATE can remain a product CREATE with the same body.

This differs from redirect cases where some clients convert methods to GET.

The danger is not merely credential forwarding; it is duplicate mutation replay.

### 17.2 Required contract

A redirect target must be checked against:

- allowed NAVER host;
- adopted endpoint path/version;
- expected method preservation;
- endpoint-specific redirect policy;
- remote-outcome/idempotency safety.

Unexpected redirect on a mutating endpoint is a contract event, not merely an HTTP convenience.

A redirect response MUST NOT cause credentials or bodies to be forwarded to an unapproved host.

Same-origin redirect does not by itself make a repeated mutation safe.

### 17.3 Implementation guard

Repository/static acceptance MUST verify that the generic SmartStore mutation adapter cannot silently follow redirects.

If the selected HTTP library provides a global `follow_redirects`/equivalent option, the mutation adapter MUST keep it disabled.

If HTTPX is selected, current behavior is:

- redirect following is disabled by default;
- when redirect following is enabled, 308 retains the original method and body.

Therefore `follow_redirects=True` is forbidden on the generic SmartStore mutation client.

Any permitted redirect must be processed by explicit endpoint-aware adapter logic after validating the target and replay semantics.

---

## 18. Unknown-code default

An unmapped provider code, undocumented status/code combination, malformed error body, missing success predicate, or contradictory evidence SHALL default to:

`error_class = UNKNOWN`

not to the nearest convenient known class.

For UNKNOWN:

- preserve sanitized wire evidence;
- preserve trace ID;
- preserve endpoint/method/session generation;
- do not mutate durable auth/scope truth without corroboration;
- do not blindly retry destructive/mutating operations;
- attempt safe read-only diagnostics/read-back when available;
- escalate workflow state to `REVIEW_REQUIRED` if bounded diagnostics cannot resolve the cause.

The `error_class` remains `UNKNOWN` unless new evidence proves another canonical class.

This distinction prevents human escalation from falsely claiming a root cause.

---

## 19. Classification changes require new evidence

ICBM MAY reclassify an error when new evidence arrives.

Examples:

- `GW.AUTHN / UNKNOWN` -> `AUTH` after proving the bearer token was expired;
- `GW.AUTHN / UNKNOWN` -> `POLICY_BLOCKED` after proving the required API group was absent;
- `GW.IP_NOT_ALLOWED / UNKNOWN` -> `POLICY_BLOCKED` after proving the current outbound IP is not configured;
- `BAD_REQUEST / UNKNOWN` -> `VALIDATION` after parsing a documented structural `invalidInputs` type;
- `409 / CONFLICT` -> `DUPLICATE` after an explicit duplicate domain code or read-back proves the same resource already exists.

Reclassification MUST append/retain the evidence trail rather than rewriting history as though the original uncertainty never existed.

Retry-count growth or budget exhaustion alone is not new causal evidence.

Therefore:

`TRANSIENT + retry_budget_exhausted`

normally remains:

`error_class=TRANSIENT`

while the workflow state changes to stop automation/escalate.

---

## 20. Mapping priority and ownership

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

## 21. State convergence implications

The error mapper supplies evidence to capability/workflow state machines; it does not invent new state enums.

### `TRANSIENT`

- may remain automatically recoverable within retry budget;
- budget exhaustion stops automatic retry;
- do not change class merely because the count reached a threshold;
- workflow may pause/escalate while class remains `TRANSIENT`.

### `RATE_LIMITED`

- schedule for later execution;
- do not mark auth/scope invalid;
- persistent unexpected quota behavior may require workflow review without falsifying the class.

### `AUTH`

- use `AUTH.md` recovery/state rules;
- repeated deterministic failure must not loop indefinitely.

### `VALIDATION`

- request cannot proceed unchanged;
- surface field-level actionable evidence when available.

### `POLICY_BLOCKED`

- do not retry unchanged until provider/account/application/product policy condition changes;
- often requires operator action/review.

### `CONFLICT`

- re-read/reconcile current remote state before further mutation.

### `DUPLICATE`

- identify existing remote resource and use duplicate-conflict/reconciliation policy;
- never create another copy merely because the first attempt was uncertain.

### `REVIEW_REQUIRED` as error class

- use only when the provider/domain/endpoint semantics positively establish human judgment/manual resolution as the cause category and no narrower class applies;
- no generic SmartStore baseline wire code is assigned here yet.

### `FATAL`

- stop the affected capability until code/config/contract correction;
- do not burn retry budget on a proven deterministic integration defect.

### `UNKNOWN`

- preserve uncertainty;
- no destructive retry by default;
- use safe diagnostics/reconciliation;
- workflow may become `REVIEW_REQUIRED` while class remains `UNKNOWN`.

Final `PAUSED` reason-code mapping remains part of the M2 state-contract freeze rather than being invented here.

---

## 22. Evidence and logging safety

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
- derived ambiguity flag;
- retry/reconciliation decision;
- workflow state.

Debugging must not turn failure evidence into a credential or PII leak.

---

## 23. Acceptance requirements

M2 SmartStore error handling acceptance MUST include at least the following.

### 23.1 `GW.AUTHN` — expired token

Using a safe expired/invalid token condition without corrupting durable credentials:

- receive `GW.AUTHN` or the measured provider auth failure;
- prove the auth cause using session/expiry evidence;
- classify `AUTH`;
- perform bounded auth recovery;
- for a protected read, retry only within the auth contract;
- do not generalize the mapping to every future `GW.AUTHN`.

### 23.2 `GW.AUTHN` — missing permission

Using controlled permission evidence or a safe harness:

- observe the same/similar authorization wire code;
- prove required API-group absence;
- classify `POLICY_BLOCKED`;
- update scope state under `PERMISSIONS_SCOPES.md`;
- do not mislabel credentials invalid.

### 23.3 `GW.AUTHN` — malformed authorization header

Using a local/mocked request boundary rather than damaging real credentials:

- construct the malformed-header condition;
- verify the local request contract catches it where possible;
- if provider behavior is exercised safely, verify the failure is not persisted as credential invalidity;
- classify proven request-builder defect as `FATAL`.

### 23.4 Same cause / different wire representation

Test at least one permission-denial scenario represented as two different wire families in a controlled contract fixture, for example:

- gateway `401/GW.AUTHN`;
- API-server `403/FORBIDDEN`.

Verify both can converge to the same canonical `POLICY_BLOCKED` class only when permission cause is positively evidenced.

### 23.5 `GW.IP_NOT_ALLOWED` evidence conflict

Test both:

- proven outbound-IP mismatch -> `POLICY_BLOCKED`;
- current allow-list evidence matches but provider response contradicts it -> `UNKNOWN` rather than overwriting config truth.

### 23.6 Product structural validation

Use representative documented `BAD_REQUEST/invalidInputs` fixtures such as:

- missing required field;
- invalid enum;
- numeric maximum;
- incompatible attribute combination.

Verify classification:

`VALIDATION`

and no unchanged automatic retry.

### 23.7 Product policy restriction through `BAD_REQUEST`

Use a controlled restricted-value fixture such as restricted seller-tag semantics.

Verify:

`BAD_REQUEST` does not force `VALIDATION`;

provider policy evidence maps to:

`POLICY_BLOCKED`.

### 23.8 Unknown provider code

Inject an unrecognized provider code/status combination.

Verify:

- `error_class=UNKNOWN`;
- raw/sanitized evidence retained;
- no durable auth/scope rewrite;
- no automatic mutating replay.

### 23.9 Rate limit and quota

Use controlled fixtures and safe measured behavior when available.

Verify:

- `RATE_LIMITED` classification;
- rate/quota headers captured when present;
- bounded/scheduled retry;
- no token refresh merely because of 429;
- no hot loop;
- long-period quota is not treated like a one-second rate limit.

### 23.10 Read transient recovery

Inject gateway/server timeout/5xx for a protected read.

Verify bounded `TRANSIENT` retry and retry-budget enforcement.

When budget is exhausted without new causal evidence:

- class remains `TRANSIENT`;
- workflow leaves automatic retry.

### 23.11 Write timeout uncertainty

For a mutating request, inject timeout/connection loss after request transmission may have occurred.

Verify:

- `error_class=TRANSIENT` may be retained;
- `remote_outcome=UNKNOWN`;
- `ambiguous_result=true` when a RegistrationAttempt exists;
- no blind replay;
- read-back/reconciliation executes first;
- unresolved result escalates workflow safely without changing the cause merely for convenience.

### 23.12 Whitelisted pre-send non-application

Test separately:

- local pre-submit rejection;
- DNS resolution failure;
- new TCP connect failure before establishment;
- new-connection TLS handshake failure before HTTP request transmission.

For each case, the harness/transport instrumentation must prove the corresponding whitelist predicate before allowing:

`remote_outcome=NOT_APPLIED_PROVEN`

and:

`ambiguous_result=false`.

### 23.13 Non-whitelisted transport uncertainty

Test at least:

- pooled/reused connection reset;
- generic timeout with unknown phase;
- send failure after transport handoff.

Verify all default to:

`remote_outcome=UNKNOWN`

for writes, even if the exception name contains `Connection` or `Connect` language.

### 23.14 Production transport observability

Verify the actual HTTP stack exposes enough phase information to satisfy every production whitelist predicate used by ICBM.

A fixture proving a no-send path is not sufficient by itself.

If the production client cannot positively establish the predicate, production behavior must remain:

`remote_outcome=UNKNOWN`.

### 23.15 Conflict vs duplicate

Use fixtures for:

- generic 409 conflict;
- explicit duplicate domain evidence.

Verify:

- generic conflict -> `CONFLICT`;
- explicit duplicate -> `DUPLICATE`;
- duplicate is not inferred solely from prior timeout/retry history.

### 23.16 Resource not found contexts

Verify distinct handling for:

- gateway `GW.NOT_FOUND` route failure;
- API-server missing input resource;
- missing post-write read-back resource.

They MUST NOT share one unconditional mapping.

Immediate read-back absence MUST NOT become `NOT_APPLIED_PROVEN` until the M5 consistency window proves that inference safe.

### 23.17 Redirect safety / 308 mutation preservation

For a mutating request receiving 308:

- generic client does not silently follow;
- method/body preservation is treated as duplicate-mutation risk;
- redirect target is validated against endpoint policy;
- no credentials/body are forwarded to an unapproved host;
- same-origin redirect alone does not authorize replay;
- repository/static guard catches an accidental generic auto-follow configuration.

If HTTPX is used, acceptance SHALL include a guard against `follow_redirects=True` on the generic mutation client.

### 23.18 Mandatory success predicate / 2xx semantic failure

For every adopted endpoint fixture:

- prove an endpoint success predicate exists;
- reject automatic adoption/execution if the predicate is missing.

Use at least one 2xx fixture containing partial/per-item/asynchronous failure semantics.

Verify:

- HTTP 2xx alone does not mark success;
- response is parsed;
- success predicate is evaluated;
- semantic failure produces the appropriate canonical class/evidence;
- mutating outcome remains independent until read-back/result contract proves it.

### 23.19 `RegistrationAttempt.ambiguous_result` compatibility

For each remote outcome verify:

- `APPLIED_PROVEN -> ambiguous_result=false`;
- `NOT_APPLIED_PROVEN -> ambiguous_result=false`;
- `UNKNOWN -> ambiguous_result=true`.

Verify the two fields cannot be independently persisted in contradictory forms.

### 23.20 Error class vs workflow `REVIEW_REQUIRED`

Exercise an unresolved unknown provider code.

Verify:

- `error_class=UNKNOWN` remains unchanged;
- workflow state may become `REVIEW_REQUIRED` after bounded diagnostics;
- no fake `error_class=REVIEW_REQUIRED` is written merely because a human is needed.

If an endpoint-specific provider condition is later mapped to canonical `error_class=REVIEW_REQUIRED`, add a separate fixture proving those provider semantics.

### 23.21 Trace/evidence capture

For gateway and API-server error fixtures:

- capture trace ID when present;
- preserve provider timestamp/code/status;
- sanitize sensitive values;
- link evidence to correct session generation and operation attempt.

### 23.22 Classification re-evaluation

Begin with `UNKNOWN`, then add corroborating evidence.

Verify reclassification appends evidence and preserves the original uncertainty event rather than rewriting history.

Also verify retry-count growth by itself cannot reclassify `TRANSIENT` to `FATAL`.

---

## 24. Runtime evidence required before `verified_at`

`verified_at` MUST remain null until measured evidence covers at least:

- current gateway error body/trace-ID shape;
- one real/safe authentication failure and recovery;
- one real/safe rate-limit or provider-approved equivalent observation, where feasible without abuse;
- real product `BAD_REQUEST` structured evidence from a non-destructive validation test or previously captured sanitized provider response;
- `GW.AUTHN` ambiguity handling;
- current `GW.IP_NOT_ALLOWED` behavior or an explicitly documented unmeasured limitation;
- mutating timeout/reconciliation behavior in a controlled boundary;
- actual production transport observability for every enabled `NOT_APPLIED_PROVEN` whitelist predicate;
- unknown-code default behavior;
- sensitive-data sanitization;
- current product endpoint redirect behavior or an explicitly documented unresolved limitation;
- endpoint success-predicate enforcement for all adopted M2 endpoints;
- `RegistrationAttempt.ambiguous_result` derivation consistency.

Live destructive errors MUST NOT be manufactured merely to complete this document.

Controlled harnesses/mocks are preferred for unsafe failure injection.

---

## 25. Open questions

### Q1. Exact API error when the 180-day own-store application re-authentication expires

`AUTH.md` records this provider lifecycle, but the exact machine-observable failure contract is still unknown.

Current status:

`UNKNOWN`

Do not map it to `AUTH`, `POLICY_BLOCKED`, or `ACCOUNT_RESTRICTED` until measured/documented.

### Q2. Does every pre-service gateway rejection guarantee no mutation reached the target service?

The gateway documentation names causes but does not establish a universal transaction/execution guarantee for all gateway error codes.

Current status:

`DO_NOT_ASSUME`

Write replay therefore continues to use the remote-outcome/idempotency/read-back contract.

### Q3. Exact redirect behavior for adopted M2 product endpoints

Current product docs list `308/PERMANENT_REDIRECT`, but the M2 endpoint matrix must establish whether/when a redirect is expected and which target is safe.

Current status:

`PENDING_ENDPOINT_MATRIX_AND_RUNTIME_EVIDENCE`

Generic mutation auto-follow remains forbidden regardless.

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

Budget exhaustion changes automation/workflow behavior, not the error class by itself.

### Q7. Which production transport exceptions satisfy the `NOT_APPLIED_PROVEN` whitelist?

Logical whitelist categories are defined in this document, but the selected production HTTP transport must prove which exact exception/telemetry combinations reliably establish those pre-send boundaries.

Current status:

`IMPLEMENTATION_MEASUREMENT_REQUIRED`

Do not map exception class names directly without that verification.

### Q8. Canonical schema evolution from `ambiguous_result` to `remote_outcome`

Current compatibility rule derives the canonical boolean from `remote_outcome`.

A future ADR may choose to persist the richer enum directly in `RegistrationAttempt`.

Current status:

`COMPATIBILITY_MAPPING_DEFINED / SCHEMA_CHANGE_NOT_REQUIRED_FOR_THIS_DOC`

---

## 26. Final M2 error contract

For SmartStore:

`HTTP status != canonical error class`

`provider code != root cause`

`same provider code may map to different canonical classes`

`same root cause may arrive through different provider codes/statuses`

`every adopted endpoint requires a success predicate`

`2xx != semantic success until success_predicate passes`

`BAD_REQUEST != always VALIDATION`

`GW.AUTHN != always AUTH`

`GW.IP_NOT_ALLOWED != always proven POLICY_BLOCKED`

`429/GW.RATE_LIMIT or GW.QUOTA_LIMIT = RATE_LIMITED`

`unmapped or contradictory evidence = UNKNOWN`

`UNKNOWN != permission to guess`

`error_class != workflow_state`

`error_class=UNKNOWN + workflow_state=REVIEW_REQUIRED is valid`

`error_class=REVIEW_REQUIRED is reserved for positively established provider/domain human-judgment semantics`

`error_class != replay permission`

`retry-budget exhaustion != automatic reclassification`

`TRANSIENT write failure + remote_outcome UNKNOWN != safe retry`

`NOT_APPLIED_PROVEN = whitelist-only or positive endpoint reconciliation proof`

`pooled/reused connection failure != NOT_APPLIED_PROVEN by exception name`

`RegistrationAttempt.ambiguous_result = (remote_outcome == UNKNOWN)` until a later canonical schema ADR changes it`

`HTTP 308 can preserve method/body and therefore can replay a mutation`

`generic mutation redirect following = forbidden`

`mutating replay requires endpoint-specific idempotency/read-back proof`

`provider trace/evidence must be preserved safely`

`classification may strengthen only when new causal evidence arrives`

`final write truth comes from bounded mutation + external read-back, not from error-code optimism`

All unknown provider behavior remains UNKNOWN until measured or officially documented.
