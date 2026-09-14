# SmartStore Permissions and Scope Contract

## Status

- Provider: NAVER SmartStore / Commerce API
- Contract status: `DOCUMENTED_SUPPORTED_WITH_INTROSPECTION_GAP`
- M2 integration mode: `OWN_STORE_SELF`
- OAuth scopes: `NOT_SUPPORTED_BY_PROVIDER`
- Provider permission mechanism: application `API 그룹`
- Programmatic permission introspection: `NO_DOCUMENTED_ENDPOINT_FOUND`
- Runtime permission verification: `PENDING`

## Provenance

Primary upstream documentation:

- source_url:
  - https://apicenter.commerce.naver.com/docs/auth
  - https://apicenter.commerce.naver.com/docs/restriction
  - https://apicenter.commerce.naver.com/docs/commerce-api/current
  - https://apicenter.commerce.naver.com/docs/commerce-api/current/get-account-info-by-account-no-sellers
  - https://apicenter.commerce.naver.com/docs/commerce-api/current/create-product-product
- upstream_version: `2.88.0`
- retrieved_at: `2026-09-14`
- verified_at: `null`
- review_due: `2026-10-14`

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/1013
  - `GW.AUTHN` can occur when the application lacks the required API-group permission.
  - provider guidance directs operators to inspect `내스토어 애플리케이션 -> 애플리케이션 상세 -> API 그룹`.
  - some endpoints can require an additional API group beyond the apparent domain group.
- https://github.com/commerce-api-naver/commerce-api/discussions/1835
  - product-related API calls require the `상품` API group.
  - API groups can be added or removed in the Commerce API Center application settings.
- https://github.com/commerce-api-naver/commerce-api/discussions/1895
  - seller-information API calls require the `판매자정보` API group.
- https://github.com/commerce-api-naver/commerce-api/discussions/1093
  - order-seller APIs require the `주문 판매자` API group.

Freshness rule:

- Any upstream Commerce API version change, API-group model change, endpoint-to-group mapping change, application-management change, permission-introspection API introduction, or authorization error-contract change SHALL trigger immediate review.
- `review_due` is a fallback calendar bound when automated change detection is absent or broken.
- Reading documentation or viewing a portal setting is not the same as proving a write works.
- A portal observation is a point-in-time operator attestation. It is not machine-verifiable proof that the provider configuration remained unchanged after observation.

---

## 1. Purpose

ICBM models marketplace capability in three separate layers:

`auth -> write_scope -> write`

For SmartStore these layers MUST remain independent.

- `auth` proves that the current committed authentication session represents the intended seller account.
- `write_scope` represents ICBM's evidence-backed knowledge of the provider-side permission configuration required for the intended write capability.
- `write` proves that the intended mutation actually succeeds and can be read back correctly.

The following equivalences are forbidden:

`auth READY == write_scope READY`

`write_scope READY == write READY`

`token issued == permission granted`

`permission granted == mutation works`

A `write_scope` status without its evidence provenance is incomplete information.

---

## 2. SmartStore does not use OAuth scope strings

NAVER Commerce API uses OAuth 2.0 Client Credentials Grant, but the official authentication documentation states:

`Scopes: N/A`

The Commerce API does not provide an OAuth `scope` specification.

Therefore ICBM MUST NOT expect, parse, persist, compare, or gate SmartStore permissions using:

- token-response `scope` strings;
- OAuth consent scope names;
- space-delimited OAuth scopes;
- assumptions copied from another marketplace's OAuth model.

The cross-platform ICBM field name `write_scope` is retained as a canonical capability concept.

For SmartStore:

`write_scope = required provider API-group permission state`

not:

`write_scope = OAuth scope string`

---

## 3. Provider permission mechanism: API groups

Official NAVER guidance states that an API call requires permission for the API group containing that API.

The application obtains API-group permissions when the application is registered or modified in Commerce API Center.

Therefore the authoritative provider-side permission object for M2 is the application's API-group configuration.

Permission is application-level provider configuration.

It is not inferred from:

- a locally desired feature list;
- a successful token issuance response;
- a saved ICBM checkbox;
- a store name;
- a bearer-token string;
- a previous successful call made under an unknown historical configuration.

---

## 4. M2 permission groups

The exact endpoint-to-permission mapping SHALL be maintained in `ENDPOINT_MATRIX.md`.

### 4.1 `판매자정보`

Purpose in M2:

- seller-account protected reads used by `ACCOUNT_IDENTITY.md`;
- specifically relevant to `GET /v1/seller/account` and other seller-information APIs.

This group supports the `auth` proof path rather than product-write capability itself.

If the current application cannot use the seller-information read required for account identity proof, `auth` cannot become READY merely because token issuance succeeded.

### 4.2 `상품`

Purpose in M2:

- product reads needed by registration/read-back workflows;
- product registration, modification, and related product-management operations;
- primary provider permission group for SmartStore product-write capability.

Official technical support has explicitly tied product API authorization failures to absence of the `상품` API group.

For the current M2 product-registration baseline:

`required_write_permission_group = 상품`

subject to endpoint-specific additions in `ENDPOINT_MATRIX.md`.

### 4.3 `주문 판매자`

Purpose:

- order-seller APIs and later order-management capability.

It is not automatically required merely to prove M2 product registration capability.

### 4.4 `문의`

Purpose:

- inquiry/Q&A capabilities.

Provider guidance demonstrates that some inquiry endpoints additionally require `주문 판매자` permission.

Therefore:

`endpoint domain != exactly one API group`

An endpoint may require multiple permission groups or additional provider conditions.

---

## 5. `write_scope` state model and evidence envelope

For SmartStore:

`write_scope.status in {READY, MISSING, UNKNOWN}`

The status describes evidence-backed knowledge of declared provider permissions. It does NOT describe actual mutation success.

Because current SmartStore M2 has no documented machine-readable API-group introspection endpoint, a status value MUST NOT be presented without evidence provenance.

The permission state SHALL be treated as an envelope conceptually equivalent to:

```text
write_scope = {
  status: READY | MISSING | UNKNOWN,
  evidence_source,
  evidence_strength,
  observed_at,
  application_fingerprint,
  required_groups,
  observed_groups,
  endpoint_mapping_revision,
  attested_status,
  freshness_policy_max_age_days,
  freshness_status
}
```

The envelope mixes facts fixed when the evidence was recorded with one value that only exists at evaluation time (Issue #32):

- `attested_status` (`READY | MISSING`) is the stable classification made when the evidence was recorded. It is persisted with the evidence record and MUST be consistent with the stored `required_groups` / `observed_groups` (`READY` iff every required group was observed). It is history, not current truth; current truth is `status`, re-derived on every evaluation.
- `freshness_policy_max_age_days` is the evidence-age bound (§8.1) in effect when the evidence was recorded. It is persisted so a later audit can explain the decision.
- `freshness_status` is **derived at evaluation/read time** from `observed_at`, `now` from the injected clock, and the applicable bound (§8.1). It MUST NOT be persisted as authoritative current truth: a stored `FRESH` would become false merely because time passed.
- Evidence records are append-only. Expiry or invalidation never mutates a stored record; it changes only the evaluated `status`.

A bare `READY` label is forbidden in UI, audit evidence, or diagnostic output when it would hide whether the evidence was machine-verified or operator-attested.

### 5.1 `READY`

`write_scope.status=READY` means:

1. the complete required permission set for the target capability is known;
2. positive evidence says the identified SmartStore application contains every required API group;
3. the evidence is bound to the current provider application fingerprint;
4. the endpoint/group mapping revision used by the evidence is current;
5. the evidence has not crossed its configured freshness bound and no known invalidation event occurred;
6. the evidence provenance is surfaced with the status.

For the M2 product-registration baseline, the minimum required set is currently:

`{상품}`

`READY` MUST NOT be assigned merely because:

- authentication succeeded;
- `/v1/seller/account` succeeded;
- a product read happened to succeed historically;
- the user says they probably enabled all groups;
- an ICBM config file lists `상품` as desired;
- a write once succeeded under an unversioned historical application configuration.

When READY comes from manual portal inspection, the evidence MUST remain explicitly classified as operator-attested. It MUST NOT be presented as machine-verified current provider truth.

### 5.2 `MISSING`

`write_scope.status=MISSING` means positive evidence identifies at least one required provider permission as absent.

Acceptable evidence classes include:

- an operator-attested current provider-admin observation that visibly omits a required API group;
- a future official permission-introspection API explicitly reporting the group absent;
- a future provider error contract that uniquely identifies the missing permission group.

The same provenance requirements apply to MISSING. A manually observed absence is still operator-attested, not machine-verified.

Generic `GW.AUTHN`, `UNAUTHORIZED`, or `FORBIDDEN` alone are NOT sufficient to prove MISSING.

### 5.3 `UNKNOWN`

`write_scope.status=UNKNOWN` is required when ICBM cannot prove either READY or MISSING under the adopted evidence policy.

Examples:

- no provider-side permission evidence exists;
- the operator cannot or did not attest the provider-admin configuration;
- evidence provenance is missing;
- the application fingerprint does not match the current application;
- only a generic authorization failure such as `GW.AUTHN` is available;
- endpoint/path correctness is not established;
- auth/session validity is uncertain;
- required endpoint-to-group mapping changed;
- evidence exceeded its configured freshness bound;
- provider behavior materially contradicts the evidence.

`UNKNOWN` is an explicit statement of insufficient evidence, not an error synonym.

---

## 6. Current permission-introspection limitation

The published Commerce API surface reviewed for M2 does not expose a documented endpoint that returns the calling application's configured API-group list.

No documented machine-readable equivalent was found for:

`GET /oauth/scopes`

or:

`GET /application/permissions`

for the M2 own-store integration.

This is a current documentation finding, not a permanent provider guarantee.

If NAVER later introduces an official introspection endpoint, this contract MUST be reviewed before adoption.

Until then, Commerce API Center can provide an operator-visible provider declaration, but that observation remains an operator attestation rather than a machine read-back.

---

## 7. Operator-attested provider-admin observation

The previous shorthand `PROVIDER_ADMIN_READBACK` is deliberately rejected for the current manual flow because it could imply machine measurement.

When a human inspects Commerce API Center and records the configured API groups, ICBM SHALL classify the evidence as:

`evidence_source = OPERATOR_ATTESTED_PROVIDER_ADMIN`

and:

`evidence_strength = OPERATOR_ATTESTED`

not:

`MACHINE_VERIFIED`

A screenshot MAY support the audit trail, but it does not prove that the provider configuration remained unchanged after capture.

A sanitized evidence record SHOULD contain at least:

- internal `marketplace_account_id`;
- provider=`SMARTSTORE`;
- auth mode=`SELF`;
- `application_fingerprint`;
- required API-group set;
- observed API-group set;
- observation timestamp;
- internal operator/audit actor reference when available;
- endpoint-mapping revision;
- upstream documentation version;
- `evidence_source=OPERATOR_ATTESTED_PROVIDER_ADMIN`;
- `evidence_strength=OPERATOR_ATTESTED`;
- `attested_status` (`READY | MISSING`), consistent with the required and observed groups (§5);
- freshness-policy identifier / bound in effect at recording (`freshness_policy_max_age_days`, §8.1);
- sanitized evidence reference when retained.

The evidence MUST NOT contain plaintext:

- `client_secret`;
- bearer tokens;
- complete `client_id` in logs, screenshots, or exported evidence.

### 7.1 Application fingerprint

Requirement 5.1(3) MUST be mechanically checkable.

Permission evidence SHALL bind to a non-reversible keyed fingerprint derived from the canonical provider application identity, including the normalized `client_id`, provider, and auth mode.

Conceptually:

`application_fingerprint = keyed_fingerprint(provider | auth_mode | client_id)`

The exact cryptographic construction is an implementation detail, but it MUST:

- avoid storing the complete `client_id` in exported evidence;
- allow ICBM to determine whether permission evidence belongs to the currently configured application;
- fail closed to `UNKNOWN` if the fingerprint cannot be validated;
- not be treated as an authentication secret or account identity substitute.

If current application fingerprint differs from the evidence fingerprint:

`write_scope.status -> UNKNOWN`

### 7.2 UI and audit presentation

When evidence is operator-attested, UI and audit surfaces MUST expose that fact.

Examples:

- acceptable: `권한 확인됨 (관리자 화면 확인)`
- acceptable: `write_scope=READY / evidence=OPERATOR_ATTESTED`
- forbidden: a generic `READY` badge that is visually indistinguishable from machine-verified provider introspection.

If a future official introspection API exists, machine evidence may use a distinct strength such as `MACHINE_VERIFIED`.

This distinction prevents human report from silently becoming equivalent to measured API proof.

---

## 8. Freshness semantics for manual permission evidence

An operator-attested portal observation is a point-in-time snapshot.

ICBM cannot prove that the provider configuration was unchanged after that observation unless NAVER exposes a machine-verifiable revision/introspection mechanism.

Therefore `fresh` means only:

- the observation is within the configured evidence-age policy;
- it belongs to the current application fingerprint;
- the endpoint/group mapping revision remains current;
- no known invalidation event occurred.

It MUST NOT be described as proof of continuous unchanged provider state.

Known external-edit detection is necessarily incomplete while no provider introspection/change-feed exists. The UI and audit model MUST state this limitation rather than implying perfect change detection.

### 8.1 M2 maximum evidence age (frozen, Issue #32)

The M2 canonical maximum age for operator-attested permission evidence is **30 days**.

```text
canonical A0 max age          = 30 days
production default            = 30 days
optional operational override = 1..30 days (may only tighten)
override < 1 or > 30 days     = invalid configuration, rejected at startup
```

An override may tighten the bound but never extend it beyond the canonical maximum. No configuration keeps operator-attested evidence current indefinitely.

The bound in effect when evidence is recorded is persisted with it (`freshness_policy_max_age_days`, §5). At evaluation the applicable bound is the stricter of the recorded bound and the currently configured bound: tightening the policy applies to existing evidence immediately, and loosening it never extends evidence beyond the bound it was recorded under.

Boundary semantics, with `age = now - observed_at` and `bound` the applicable bound:

```text
age <  bound => FRESH
age =  bound => FRESH
age >  bound => EXPIRED
```

The observation stays valid through the exact bound and expires only after crossing it.

`now` comes from the injected application `Clock` that the capability services already use. The evaluation path MUST NOT read wall-clock time directly (`datetime.now()`, `datetime.utcnow()` or equivalent): the service obtains `now` from the clock and passes it to the pure evaluation function, so expiry is deterministic and testable.

Known invalidation events (§14: application-fingerprint change, required-group change, endpoint-mapping revision change, malformed evidence) still invalidate immediately; they never wait for age expiry.

### 8.2 Expiry converges both READY and MISSING to UNKNOWN

`attested_status` is record-time history; current truth needs current evidence. Once evidence is expired or otherwise invalidated:

```text
attested_status=READY   + EXPIRED -> write_scope.status=UNKNOWN
attested_status=MISSING + EXPIRED -> write_scope.status=UNKNOWN
```

A positively observed absence is not kept as `MISSING` once it is no longer current. When expired `MISSING` evidence converges to `UNKNOWN`, the evidence-dependent `PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT` overlay is removed (`CAPABILITY_MAPPING.md` S6). This does not grant the permission: `write_scope=UNKNOWN`, and `write` is never promoted by it.

The operator-facing A0 surface MUST distinguish expired or invalidated evidence from never-recorded evidence, and MUST NOT present a block that disappeared through expiry as newly granted permission. It communicates the equivalent of `저장된 권한 확인이 만료되어 현재 권한 상태를 확인할 수 없음`, and the API keeps the reason (for example `EXPIRED`) in the evaluation.

---

## 9. Runtime probes are not the same as declared scope

A successful non-mutating API call can prove that a specific authenticated request was effectively allowed.

It does not necessarily prove the complete declared permission set required by a future write transaction.

Reasons include:

- endpoint-specific provider conditions;
- cross-group requirements;
- resource ownership restrictions;
- store/channel/account state;
- agreement/provision requirements;
- an endpoint's permission set differing from another endpoint in the same broad domain.

ICBM MAY record:

`effective_access = PROVEN_FOR(endpoint, session_generation)`

but MUST NOT silently convert it into:

`write_scope.status=READY`

unless the adopted endpoint/permission contract proves equivalence to the complete required permission set.

No such generic equivalence is assumed for M2.

---

## 10. `GW.AUTHN` is ambiguous

NAVER's authentication documentation explains that `401 / GW.AUTHN` can indicate an expired token.

Official provider support also documents cases where the same `GW.AUTHN` occurs because the application lacks a required API-group permission.

Provider support also directs users to verify API-group permission, request path, and token validity.

Therefore:

`GW.AUTHN != proven token expiry`

`GW.AUTHN != proven permission missing`

A single `GW.AUTHN` response MUST NOT rewrite durable truth as either:

- `credentials invalid`; or
- `write_scope.status=MISSING`.

It is an evidence-classification problem first.

---

## 11. Permission-failure diagnosis order

When a target API returns an authorization-like failure, ICBM SHALL diagnose in this order.

### Step 1: establish current authentication truth

Re-evaluate `AUTH.md` invariants using the current committed session generation and account identity proof.

If auth cannot be proven READY, resolve authentication first.

### Step 2: verify endpoint contract

Confirm against current `ENDPOINT_MATRIX.md` / upstream docs:

- host;
- HTTP method;
- path;
- auth mode;
- intended resource;
- required permission groups.

A wrong route or method is not a scope failure.

### Step 3: inspect permission evidence and provenance

If valid current evidence explicitly shows a required group absent:

`write_scope.status -> MISSING`

If valid current evidence shows every required group present:

`write_scope.status -> READY`

The resulting state MUST retain and surface its evidence source/strength.

### Step 4: classify runtime denial separately

If scope status is READY but the call is still denied, do NOT automatically erase or invert the permission declaration.

Investigate:

- account/store/channel state;
- role/provision/agreement conditions;
- resource ownership;
- unsupported API for the application type;
- undocumented provider restriction;
- endpoint mapping drift;
- stale operator-attested evidence.

Operation capability may become `BLOCKED` or `REVIEW_REQUIRED` while the last permission declaration remains recorded with its provenance.

If runtime behavior materially contradicts operator-attested permission evidence, the active scope status SHOULD converge to UNKNOWN pending review rather than continuing to advertise an unqualified READY.

### Step 5: preserve uncertainty

If evidence cannot separate auth, route, permission, and provider-state causes:

`write_scope.status = UNKNOWN`

---

## 12. Relationship between `auth`, `write_scope`, and `write`

### Case A

`auth=READY`

`write_scope.status=READY`

`write_scope.evidence_strength=OPERATOR_ATTESTED | MACHINE_VERIFIED`

`write=UNVERIFIED`

Meaning: intended account is proven; declared permission set has positive evidence with explicit provenance; mutation has not been proven.

### Case B

`auth=READY`

`write_scope.status=MISSING`

`write=BLOCKED`

Meaning: at least one required permission is evidenced absent. ICBM MUST NOT perform a production mutation merely to reconfirm absence.

### Case C

`auth=READY`

`write_scope.status=UNKNOWN`

`write=UNVERIFIED`

Meaning: account proof succeeded but provider permission configuration is not sufficiently evidenced.

A bounded write proof MAY be considered later only under the explicit M5 approval/safety contract.

### Case D

`auth=READY`

`write_scope.status=READY`

`write=BLOCKED`

Meaning: declaration evidence says required groups exist, but measured write is blocked by another condition or runtime behavior.

### Case E

`auth=READY`

`write_scope.status=UNKNOWN`

`write=READY`

This is logically valid if a bounded write and read-back later succeed while provider-declared group configuration remains unproven.

A successful write proves effective write capability for that tested transaction. It does not manufacture provider-declaration evidence retroactively.

---

## 13. M5 bounded canary relationship

M2 permission work MUST NOT mutate merely to discover scope.

The first bounded product CREATE belongs to the later M5 write-capability proof.

The M5 canary may transition:

`write: UNVERIFIED -> READY`

only after:

1. explicit operational approval;
2. current `auth=READY`;
3. `write_scope.status != MISSING`;
4. bounded CREATE;
5. external read-back;
6. expected-vs-observed comparison;
7. durable evidence commit;
8. safe cleanup policy where applicable.

If scope is UNKNOWN, the canary requires explicit acknowledgement that provider permission configuration is not positively introspected.

The canary MUST NOT be silently used as a permission-discovery trick during ordinary M2 CONNECT.

---

## 14. Permission evidence lifecycle

Permission evidence belongs to a provider application configuration, not to a bearer-token string.

Evidence MUST be invalidated or re-reviewed when at least one of the following occurs:

- current `application_fingerprint` differs;
- application type or auth mode changes;
- an operator reports editing API-group configuration;
- required endpoint set changes;
- endpoint-to-group mapping changes;
- upstream permission model changes;
- evidence freshness bound expires;
- runtime behavior materially contradicts the evidence.

A normal bearer-token renewal does NOT by itself invalidate application-level permission evidence.

A `client_secret` rotation for the same provider application also does not by itself prove API groups changed, but auth still must satisfy the new credential/session generation contract before operations resume.

Because external edit detection is incomplete without provider introspection/change-feed, absence of a known edit is not proof that no edit occurred.

---

## 15. Known permission edits force a new auth session

Open question Q2 remains unresolved: current documentation does not establish whether an API-group edit affects an already-issued token immediately or only a later token/session.

ICBM MUST NOT depend on either behavior.

After a known API-group edit, the conservative convergence policy is:

1. `write_scope.status -> UNKNOWN`;
2. invalidate the previous permission evidence for active gating;
3. mark the existing bearer session as stale-for-permission-change for operational use;
4. obtain and atomically commit a new token session generation when provider token policy allows;
5. re-run current account identity proof under that session;
6. obtain fresh permission evidence/attestation;
7. only then allow scope to return to READY or MISSING.

A known permission edit does NOT increment `credential_generation` merely because API groups changed; the client credential bundle may be unchanged.

It does require a fresh `session_generation` before ICBM treats the new permission configuration as operationally adopted.

If provider token issuance timing prevents immediate new-session acquisition, ICBM remains fail-closed for operations that depend on the edited permissions rather than assuming the old session adopted them.

---

## 16. Endpoint-specific additional permissions

The required permission set for an operation is:

`union(required_groups(endpoint_i) for every endpoint_i in the operation transaction)`

A registration flow may use:

- metadata/category reads;
- image-related APIs;
- product CREATE;
- product read-back.

The write-scope requirement is the union of adopted endpoint requirements, not merely the final CREATE endpoint.

`ENDPOINT_MATRIX.md` is responsible for freezing that union.

---

## 17. Unsupported or restricted API groups

Some APIs are unavailable to certain application types, including APIs reserved for Commerce Solution or other programs.

ICBM MUST NOT infer that an API appearing in Commerce API documentation is automatically usable by `OWN_STORE_SELF`.

`ENDPOINT_MATRIX.md` MUST record application-mode eligibility for every adopted endpoint.

If an endpoint is unavailable to M2 application mode, this is a provider capability restriction, not a missing local checkbox or authentication failure.

---

## 18. Evidence safety

Permission evidence MAY include:

- API-group names;
- timestamps;
- documentation/mapping revision;
- non-reversible application fingerprint;
- provider trace ID from a failed probe;
- HTTP status/error code;
- target endpoint identifier;
- operator-attestation metadata.

Permission evidence MUST NOT expose:

- `client_secret`;
- bearer token;
- complete `client_secret_sign`;
- decrypted credential blob;
- complete `client_id` in logs/exported diagnostics;
- unrelated customer/order PII.

---

## 19. Acceptance requirements

M2 SmartStore permission acceptance MUST include at least:

### 19.1 No OAuth-scope assumption

Verify integration code/state does not require a token `scope` field or invent OAuth scope strings.

### 19.2 Seller-information permission path

With valid auth material, prove account identity via the seller-information protected read. Failure prevents auth READY but is not automatically product scope MISSING.

### 19.3 Operator-attested product permission present

Using a current provider-admin observation:

- observed application includes `상품`;
- current application fingerprint matches evidence;
- evidence is recorded as `OPERATOR_ATTESTED`, not `MACHINE_VERIFIED`;
- `write_scope.status -> READY` only under the configured bounded evidence policy;
- UI/audit surfaces show the attested evidence source;
- `write` remains UNVERIFIED.

### 19.4 Operator-attested product permission absent

Using a current provider-admin observation:

- observed application omits `상품`;
- evidence provenance remains operator-attested;
- `write_scope.status -> MISSING`;
- no product mutation is attempted merely to reconfirm absence.

### 19.5 No usable permission evidence

- auth succeeds;
- no valid provider permission evidence is available;
- `write_scope.status -> UNKNOWN`;
- ICBM does not fabricate READY from token success.

### 19.6 Ambiguous `GW.AUTHN`

Using a controlled harness or safe provider condition:

- receive `401/GW.AUTHN`;
- do not immediately label credentials invalid;
- do not immediately label scope MISSING;
- diagnose auth, endpoint contract, and permission evidence separately.

### 19.7 Application-fingerprint mismatch

- begin with READY/MISSING evidence for application fingerprint A;
- configure application identity B;
- old evidence MUST NOT apply;
- active scope converges to UNKNOWN.

### 19.8 Evidence-age expiry

Using the injected clock (no sleep, no wall-clock dependence) and the canonical 30-day bound, once for evidence recorded with `attested_status=READY` and once for `attested_status=MISSING`:

- `observed_at + 30 days - ε` -> `FRESH`;
- `observed_at + 30 days` -> `FRESH`;
- `observed_at + 30 days + ε` -> `EXPIRED` -> `write_scope.status=UNKNOWN`;
- for `MISSING`: the evidence-dependent `SCOPE_INSUFFICIENT` pause is removed and `write` is not promoted;
- the expired evidence record remains stored unchanged;
- the first read after crossing the bound produces exactly one durable capability change and one audit event; later reads produce none.

### 19.9 Known permission edit

- begin with valid auth and scope evidence;
- record a known API-group configuration edit;
- invalidate prior scope evidence;
- mark old session stale-for-permission-change;
- force a new session generation when token policy permits;
- re-prove account identity;
- require fresh permission evidence before scope convergence.

### 19.10 Additional-group endpoint

At least one adopted endpoint with extra/multiple group requirements, if present in M2, must obey the union rule and endpoint matrix.

### 19.11 Scope READY does not imply write READY

Set scope READY with provenance, perform no mutation, and verify write remains UNVERIFIED.

### 19.12 Write success does not rewrite declared-scope history

If a later bounded canary succeeds while scope is UNKNOWN, write may become READY while write_scope remains UNKNOWN until declaration evidence exists.

---

## 20. Runtime evidence required before `verified_at`

`verified_at` MUST remain null until evidence covers at least:

- current application mode (`SELF`);
- seller-information identity proof path;
- actual M2 provider-admin observation/attestation workflow;
- application-fingerprint binding behavior;
- `상품` group present/absent handling;
- one controlled missing-group or equivalent permission-denial case without damaging credentials;
- observed `GW.AUTHN` classification behavior;
- evidence freshness/invalidation flow;
- confirmation that token responses provide no OAuth scope list;
- endpoint permission mapping used by the M2 registration path.

A real product CREATE is not required to verify this document; that belongs to the later bounded write proof.

---

## 21. Open questions

### Q1. Programmatic API-group introspection

Does NAVER provide or plan an official API that returns configured API groups for the calling own-store application?

Current status:

`NO_DOCUMENTED_ENDPOINT_FOUND`

ICBM MUST NOT invent one or scrape undocumented internal endpoints.

### Q2. Permission-change propagation to already issued tokens

If an operator adds/removes an API group while a bearer token is valid, does the change affect that token immediately or only a later token/session?

Current status:

`UNKNOWN`

M2 does not depend on either behavior. A known permission edit forces scope invalidation and a fresh session generation before the changed permission configuration is operationally adopted.

### Q3. Error-code specificity for missing API groups

Generic `GW.AUTHN` can represent missing group permission but is not unique to that cause.

Current status:

`AMBIGUOUS`

M2 MUST NOT map generic `GW.AUTHN` directly to `SCOPE_INSUFFICIENT` without corroborating evidence.

### Q4. Exact M2 endpoint permission union

The final required group set depends on `ENDPOINT_MATRIX.md`.

Current product-write baseline:

`{상품}`

Current status:

`BASELINE_DEFINED / FINAL_UNION_PENDING_ENDPOINT_MATRIX`

### Q5. Operator-attestation maximum age

What maximum age should M2 allow for operator-attested provider-admin permission evidence before it becomes stale?

Current status:

`FROZEN_FOR_M2 = 30 days` (Issue #32; §8.1)

The production default is 30 days; an operational override may only tighten it (1..30 days). Manual evidence is never treated as eternally READY.

---

## 22. Final M2 permission contract

For SmartStore `OWN_STORE_SELF`:

`OAuth scopes = N/A`

`provider permission mechanism = application API groups`

`auth token success != API-group permission proof`

`account identity proof != product write-scope proof`

`manual provider-admin observation = OPERATOR_ATTESTED, not MACHINE_VERIFIED`

`bare write_scope READY without evidence provenance = forbidden`

`write_scope READY = positive permission evidence + current application fingerprint + current mapping + bounded freshness + explicit evidence provenance`

`write_scope MISSING = positive absence evidence + explicit evidence provenance`

`write_scope UNKNOWN = insufficient or stale evidence`

`GW.AUTHN != automatically auth failure`

`GW.AUTHN != automatically missing scope`

`provider permission READY != actual write READY`

`known permission edit -> scope UNKNOWN + fresh session generation required`

`actual write READY requires bounded mutation + external read-back under the later write-capability contract`

`persisted write_scope READY != eternal truth`

`attested_status = persisted record-time history; freshness_status = derived at evaluation from the injected clock, never persisted`

`M2 operator-attestation max age = 30 days; an override may only tighten it (1..30)`

`expired READY or MISSING attestation -> write_scope UNKNOWN; expired MISSING also releases SCOPE_INSUFFICIENT; write is never promoted`

`unknown provider behavior remains UNKNOWN until measured or officially documented`
