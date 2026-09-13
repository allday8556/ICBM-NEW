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

---

## 1. Purpose

ICBM models marketplace capability in three separate layers:

`auth`

`-> write_scope`

`-> write`

For SmartStore these layers MUST remain independent.

- `auth` proves that the current committed authentication session represents the intended seller account.
- `write_scope` represents whether the provider-side application permission configuration is known to contain the permission set required for the intended write capability.
- `write` proves that the intended mutation actually succeeds and can be read back correctly.

The following equivalences are forbidden:

`auth READY == write_scope READY`

`write_scope READY == write READY`

`token issued == permission granted`

`permission granted == mutation works`

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

For SmartStore, however:

`write_scope = required provider API-group permission state`

not:

`write_scope = OAuth scope string`

---

## 3. Provider permission mechanism: API groups

Official NAVER guidance states that an API call requires permission for the API group containing that API.

The application obtains API-group permissions when the application is registered or modified in Commerce API Center.

Therefore the authoritative provider-side permission object for the M2 SmartStore integration is the application's API-group configuration.

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

The exact endpoint-to-permission mapping will be maintained in `ENDPOINT_MATRIX.md`.

This document defines the permission categories needed to reason safely about M2.

### 4.1 `판매자정보`

Purpose in M2:

- required for seller-account protected reads used by `ACCOUNT_IDENTITY.md`;
- specifically relevant to `GET /v1/seller/account` and other seller-information APIs.

This group supports the `auth` proof path rather than product-write capability itself.

If the current application cannot use the seller-information read required for account identity proof, `auth` cannot become READY merely because token issuance succeeded.

### 4.2 `상품`

Purpose in M2:

- product reads needed by registration/read-back workflows;
- product registration, modification, and related product-management operations;
- primary provider permission group for the SmartStore product-write capability.

Official technical support has explicitly tied product API authorization failures to absence of the `상품` API group.

For M2 product registration:

`required_write_permission_group = 상품`

subject to endpoint-specific additions captured later in `ENDPOINT_MATRIX.md`.

### 4.3 `주문 판매자`

Purpose:

- order-seller APIs and later order-management capability.

This group is not automatically required merely to prove M2 product registration capability.

It becomes required when an enabled ICBM capability uses endpoints mapped to this group.

### 4.4 `문의`

Purpose:

- inquiry/Q&A capabilities.

The provider FAQ documents that some inquiry APIs additionally require `주문 판매자` permission.

Therefore ICBM MUST NOT assume:

`endpoint domain == exactly one API group`

An endpoint may require multiple permission groups or additional provider conditions.

The endpoint matrix is the final per-endpoint permission truth adopted by ICBM.

---

## 5. `write_scope` state model

For SmartStore:

`write_scope in {READY, MISSING, UNKNOWN}`

These values describe knowledge of the provider permission configuration.

They do NOT describe actual mutation success.

### 5.1 `READY`

`write_scope=READY` means:

1. the complete required permission set for the target write capability is known;
2. positive evidence exists that the exact SmartStore application currently has every required API group;
3. the evidence applies to the current provider application identity;
4. the evidence is fresh under this document's freshness rules.

For the M2 product-registration baseline, the minimum write permission set is currently:

`{상품}`

If `ENDPOINT_MATRIX.md` later establishes additional required groups for any endpoint used by the registration transaction, the required set expands accordingly.

`READY` MUST NOT be assigned merely because:

- authentication succeeded;
- `/v1/seller/account` succeeded;
- a product read happened to succeed historically;
- the user says they probably enabled all groups;
- an ICBM config file lists `상품` as desired;
- a write once succeeded under an unversioned historical application configuration.

### 5.2 `MISSING`

`write_scope=MISSING` means there is positive evidence that at least one required provider permission is absent.

Examples of acceptable evidence:

- current provider application settings visibly omit a required API group;
- a future provider permission-introspection API explicitly reports the required group absent;
- a future provider error contract uniquely and explicitly identifies the missing permission group.

Current generic `GW.AUTHN`, `UNAUTHORIZED`, or `FORBIDDEN` responses are NOT by themselves sufficient to prove `MISSING`.

### 5.3 `UNKNOWN`

`write_scope=UNKNOWN` is required when ICBM cannot prove either READY or MISSING.

Examples:

- no current provider-side permission read-back evidence exists;
- the provider application settings cannot be inspected programmatically;
- only a generic authorization failure such as `GW.AUTHN` is available;
- endpoint/path correctness is not yet established;
- auth/session validity is uncertain;
- permission evidence belongs to a different application;
- required endpoint-to-group mapping changed after the evidence was recorded;
- evidence exceeded its freshness bound;
- provider behavior conflicts with recorded permission evidence.

`UNKNOWN` is not an error synonym.

It is an explicit statement that the evidence is insufficient.

---

## 6. Current permission-introspection limitation

The current published Commerce API surface reviewed for M2 does not expose a documented endpoint that returns the calling application's configured API-group list.

Therefore SmartStore currently has no documented machine-readable equivalent of:

`GET /oauth/scopes`

or:

`GET /application/permissions`

for the M2 own-store integration.

This is a documentation finding, not a permanent provider guarantee.

If NAVER later introduces an official introspection endpoint, this contract MUST be reviewed before ICBM adopts it.

Until then, the strongest direct provider-declaration evidence is application configuration read-back in Commerce API Center.

---

## 7. Provider-admin permission read-back

A provider-admin read-back MAY be used as positive permission evidence.

The evidence MUST identify the application without leaking sensitive authentication data.

A sanitized permission-evidence record SHOULD contain at least:

- internal `marketplace_account_id`;
- provider=`SMARTSTORE`;
- auth mode=`SELF`;
- internal application reference or non-reversible application fingerprint;
- required API-group set;
- observed API-group set;
- observation timestamp;
- upstream documentation version used for endpoint/group mapping;
- evidence source=`PROVIDER_ADMIN_READBACK`;
- sanitized evidence reference when retained.

The record MUST NOT contain plaintext:

- `client_secret`;
- bearer tokens;
- complete `client_id` in logs, screenshots, or exported evidence.

A masked or non-reversible application identifier may be shown when needed to avoid confusing two applications.

Portal read-back proves provider-declared permission configuration.

It does not prove a write transaction works.

---

## 8. Runtime probes are not the same as declared scope

A successful non-mutating API call can prove that a specific authenticated request was effectively allowed.

It does not necessarily prove the complete declared permission set required by a future write transaction.

Reasons include:

- endpoint-specific provider conditions;
- cross-group requirements;
- resource ownership restrictions;
- store/channel/account state;
- agreement/provision requirements;
- an endpoint's permission set differing from another endpoint in the same broad domain.

Therefore ICBM MAY record a separate effective-access observation such as:

`effective_access = PROVEN_FOR(endpoint, session_generation)`

but MUST NOT silently convert that observation into:

`write_scope=READY`

unless the adopted endpoint/permission contract proves that the observation is equivalent to the complete required provider permission set.

No such generic equivalence is assumed for M2.

---

## 9. `GW.AUTHN` is ambiguous

NAVER's authentication documentation explains that `401 / GW.AUTHN` can indicate an expired token.

Official provider support also documents cases where the same `GW.AUTHN` response occurs because the application's required API-group permission is absent.

Provider support additionally directs users to verify:

- API-group permission;
- request path;
- presence and validity of the authentication token.

Therefore:

`GW.AUTHN != proven token expiry`

and:

`GW.AUTHN != proven permission missing`

A single `GW.AUTHN` response MUST NOT cause ICBM to rewrite durable truth as either:

- `credentials invalid`; or
- `write_scope=MISSING`.

It is an evidence-classification problem first.

---

## 10. Permission-failure diagnosis order

When a target API returns an authorization-like failure, ICBM SHALL diagnose in this order.

### Step 1: establish current authentication truth

Re-evaluate the `AUTH.md` invariants.

Use the current committed session generation and account identity proof.

If `auth` cannot be proven READY, resolve authentication first.

### Step 2: verify the endpoint contract

Confirm against current `ENDPOINT_MATRIX.md` / upstream docs:

- host;
- HTTP method;
- path;
- auth mode;
- intended resource;
- required permission groups.

A wrong route or wrong method is not a scope failure.

### Step 3: inspect provider permission evidence

If current provider-admin evidence explicitly shows a required group missing:

`write_scope -> MISSING`

If it explicitly shows all required groups present:

`write_scope -> READY`

subject to evidence freshness.

### Step 4: classify runtime denial separately

If scope is READY but the call is still denied, do NOT erase the permission evidence automatically.

Investigate endpoint/resource/provider-state causes such as:

- account/store/channel state;
- role/provision/agreement conditions;
- resource ownership;
- unsupported API for the application type;
- undocumented provider restriction;
- endpoint mapping drift.

The operation capability may become `BLOCKED` or `REVIEW_REQUIRED` while provider-declared scope remains READY.

### Step 5: preserve uncertainty

If no evidence cleanly separates auth, route, permission, and provider-state causes:

`write_scope = UNKNOWN`

not a guessed MISSING/READY value.

---

## 11. Relationship between `auth`, `write_scope`, and `write`

### Case A

`auth=READY`

`write_scope=READY`

`write=UNVERIFIED`

Meaning:

- intended account is proven;
- declared permission set is positively evidenced;
- actual mutation has not yet been proven.

This is the normal pre-canary state.

### Case B

`auth=READY`

`write_scope=MISSING`

`write=BLOCKED`

Meaning:

- correct account is authenticated;
- at least one required provider permission is known absent;
- ICBM MUST NOT attempt a production mutation merely to reconfirm the missing permission.

### Case C

`auth=READY`

`write_scope=UNKNOWN`

`write=UNVERIFIED`

Meaning:

- account proof succeeded;
- provider permission configuration is not positively known;
- ICBM MUST NOT display or persist a false READY scope state.

A bounded write proof MAY be considered later only under the explicit approval/safety contract for the M5 canary.

### Case D

`auth=READY`

`write_scope=READY`

`write=BLOCKED`

Meaning:

- the declared permission set exists;
- actual write is blocked by another condition or by measured runtime behavior.

Scope MUST NOT be rewritten to MISSING without evidence that a required permission was actually removed.

### Case E

`auth=READY`

`write_scope=UNKNOWN`

`write=READY`

This state is logically possible if a bounded write and read-back later succeed while provider-declared group configuration still cannot be read back.

A successful write proves effective write capability for that tested transaction.

It does not retroactively manufacture provider-declaration evidence.

Therefore the layers remain separate.

---

## 12. M5 bounded canary relationship

M2 permission work MUST NOT perform a mutation merely to determine scope.

The first bounded product CREATE belongs to the later M5 write-capability proof.

The M5 canary is the formal transition candidate for:

`write: UNVERIFIED -> READY`

only after:

1. explicit operational approval;
2. current `auth=READY`;
3. `write_scope != MISSING`;
4. bounded CREATE;
5. external read-back of the created listing/resource;
6. expected-vs-observed comparison;
7. durable evidence commit;
8. safe cleanup policy where applicable.

If `write_scope=UNKNOWN`, the M5 canary requires explicit acknowledgement that provider permission configuration is not positively introspected.

The canary MUST NOT be silently used as a permission-discovery trick during ordinary M2 CONNECT.

---

## 13. Permission evidence lifecycle

Permission evidence belongs to a provider application configuration, not to a bearer token string.

The evidence MUST be invalidated or re-reviewed when at least one of the following occurs:

- `client_id` / provider application identity changes;
- application type or auth mode changes;
- operator reports editing the API-group configuration;
- required endpoint set changes;
- endpoint-to-group mapping changes;
- upstream permission model changes;
- evidence freshness bound expires;
- runtime behavior materially contradicts the evidence.

A normal bearer-token renewal does NOT by itself invalidate application-level permission evidence.

A `client_secret` rotation for the same provider application also does not automatically prove the API groups changed.

However auth must still satisfy the new credential/session generation contract before permission evidence is used for an operation.

---

## 14. Application edits and state convergence

Provider API-group configuration can be edited outside ICBM.

Therefore persisted `write_scope=READY` is not eternal truth.

After a known application permission edit:

`write_scope -> UNKNOWN`

until fresh provider-declaration evidence is obtained.

If fresh read-back shows a required group removed:

`write_scope -> MISSING`

If fresh read-back shows all required groups present:

`write_scope -> READY`

ICBM MUST NOT preserve READY solely because the previous evidence was once valid.

---

## 15. Endpoint-specific additional permissions

Official provider guidance demonstrates that some inquiry endpoints can require `주문 판매자` in addition to inquiry-related permission.

Therefore `PERMISSIONS_SCOPES.md` defines permission semantics, while `ENDPOINT_MATRIX.md` MUST define the exact required group set per adopted endpoint.

The required permission set for an operation is:

`union(required_groups(endpoint_i) for every endpoint_i in the operation transaction)`

For example, a registration flow may use:

- metadata/category reads;
- image-related APIs;
- product CREATE;
- product read-back.

The write-scope requirement is the union of the adopted endpoint requirements, not merely the HTTP method of the final CREATE.

---

## 16. Unsupported or restricted API groups

NAVER documents that some API groups/endpoints are unavailable to certain application types, including APIs reserved for Commerce Solution or other programs.

Therefore ICBM MUST NOT infer that an API appearing in the overall Commerce API documentation is automatically usable by `OWN_STORE_SELF`.

`ENDPOINT_MATRIX.md` must record application-mode eligibility for every endpoint ICBM adopts.

If an endpoint is unavailable to the M2 application type:

- this is not a missing local checkbox;
- this is not an authentication failure;
- it is a provider capability restriction.

The capability must be marked unsupported/blocked under the final state contract rather than causing retry loops.

---

## 17. Evidence safety

Permission evidence MAY include:

- API-group names;
- timestamps;
- documentation version;
- masked/non-reversible application reference;
- provider trace ID from a failed probe;
- HTTP status/error code;
- target endpoint identifier;
- operator/admin read-back confirmation.

Permission evidence MUST NOT expose:

- `client_secret`;
- bearer token;
- complete `client_secret_sign`;
- decrypted credential blob;
- complete `client_id` in logs/exported diagnostics;
- customer/order PII unrelated to proving permission state.

Permission review must prove configuration without turning audit artifacts into credentials.

---

## 18. Acceptance requirements

M2 SmartStore permission acceptance MUST include at least the following.

### 18.1 No OAuth-scope assumption

Verify that SmartStore integration code/state does not require a token `scope` field and does not invent OAuth scope strings.

### 18.2 Seller-information permission path

With current valid auth material:

- prove account identity via the seller-information protected read;
- confirm that failure of this path prevents AUTH READY;
- do not misclassify it as product `write_scope=MISSING` without product-permission evidence.

### 18.3 Product permission present

Using sanitized current provider-admin evidence:

- observed application includes `상품`;
- required M2 product-write permission set is satisfied;
- `write_scope -> READY`;
- `write` remains `UNVERIFIED` until the bounded canary.

### 18.4 Product permission absent

Using sanitized current provider-admin evidence:

- observed application omits `상품`;
- `write_scope -> MISSING`;
- no product mutation is attempted merely to reconfirm absence.

### 18.5 No fresh provider permission evidence

- auth and identity proof succeed;
- application API-group configuration cannot be positively read back;
- `write_scope -> UNKNOWN`;
- ICBM does not fabricate READY from token success.

### 18.6 Ambiguous `GW.AUTHN`

Using a controlled harness or safe provider condition:

- receive `401/GW.AUTHN`;
- verify ICBM does not immediately label credentials invalid;
- verify ICBM does not immediately label write scope MISSING;
- diagnosis checks auth, endpoint contract, and permission evidence separately.

### 18.7 Permission evidence invalidation

- begin with fresh `write_scope=READY` evidence;
- simulate/record known API-group configuration edit or provider application change;
- previous READY becomes non-authoritative;
- converge to `UNKNOWN` until fresh read-back.

### 18.8 Additional-group endpoint

At least one adopted endpoint with multiple/extra group requirements, if present in the M2 endpoint set, must be represented by the union rule and tested against the endpoint matrix.

### 18.9 Scope READY does not imply write READY

- set scope evidence to READY;
- do not perform a mutation;
- verify `write` remains `UNVERIFIED`.

### 18.10 Write success does not rewrite declared-scope history

If a later bounded canary succeeds while scope evidence is UNKNOWN:

- `write` may become READY under the canary contract;
- `write_scope` remains UNKNOWN until provider-declaration evidence is obtained.

---

## 19. Runtime evidence required before `verified_at`

`verified_at` MUST remain null until measured evidence covers at least:

- current application mode (`SELF`);
- real seller-information identity proof path;
- current provider-admin API-group read-back method;
- `상품` group presence/absence behavior;
- one controlled missing-group or equivalent permission-denial case without damaging credentials;
- observed `GW.AUTHN` classification behavior;
- permission-evidence freshness/invalidation flow;
- confirmation that token responses provide no OAuth scope list;
- endpoint permission mapping used by the M2 registration path.

A real product CREATE is not required to verify this document and belongs to the later bounded write proof.

---

## 20. Open questions

### Q1. Programmatic API-group introspection

Does NAVER provide or plan an official API that returns the configured API groups for the calling own-store application?

Current status:

`NO_DOCUMENTED_ENDPOINT_FOUND`

ICBM MUST NOT invent one or scrape undocumented internal endpoints.

### Q2. Permission-change propagation to already issued tokens

If an operator adds/removes an API group while a bearer token is still valid, does the change affect that token immediately or only a subsequently issued token/session?

Current status:

`UNKNOWN`

M2 MUST NOT depend on either behavior until measured or documented.

After a known permission edit, the conservative policy is:

- invalidate permission evidence;
- re-establish current auth/session proof as needed;
- obtain fresh permission read-back before claiming scope READY.

### Q3. Error-code specificity for missing API groups

Current provider documentation/support demonstrates that generic `GW.AUTHN` can represent missing group permission, but it is not unique to that cause.

Current status:

`AMBIGUOUS`

M2 MUST NOT map generic `GW.AUTHN` directly to `SCOPE_INSUFFICIENT` without corroborating evidence.

### Q4. Exact M2 endpoint permission union

The final required group set depends on the exact endpoints adopted in `ENDPOINT_MATRIX.md`.

Current product-write baseline:

`{상품}`

Current status:

`BASELINE_DEFINED / FINAL_UNION_PENDING_ENDPOINT_MATRIX`

---

## 21. Final M2 permission contract

For SmartStore `OWN_STORE_SELF`:

`OAuth scopes = N/A`

`provider permission mechanism = application API groups`

`auth token success != API-group permission proof`

`account identity proof != product write-scope proof`

`write_scope READY = fresh positive evidence that the current provider application contains the complete required API-group set`

`write_scope MISSING = positive evidence that at least one required group is absent`

`write_scope UNKNOWN = insufficient evidence to prove READY or MISSING`

`GW.AUTHN != automatically auth failure`

`GW.AUTHN != automatically missing scope`

`provider permission READY != actual write READY`

`actual write READY requires bounded mutation + external read-back under the later write-capability contract`

`persisted write_scope READY != eternal truth`

`provider/admin evidence + current endpoint mapping wins over stale persisted state`

Unknown provider behavior remains UNKNOWN until measured or officially documented.