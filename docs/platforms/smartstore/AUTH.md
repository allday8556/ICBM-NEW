# SmartStore Authentication Lifecycle Contract

## Status

- Provider: NAVER SmartStore / Commerce API
- Contract status: `DOCUMENTED_SUPPORTED_WITH_RUNTIME_GAPS`
- M2 target integration mode: `OWN_STORE_SELF`
- Authentication protocol: OAuth 2.0 Client Credentials Grant
- Token type: Bearer access token
- Refresh token: not documented / not used by the current Commerce API client-credentials flow
- Access-token runtime verification: `PENDING`
- Application re-authentication runtime verification: `PENDING`

## Provenance

Primary upstream documentation:

- source_url:
  - https://apicenter.commerce.naver.com/docs/auth
  - https://apicenter.commerce.naver.com/docs/commerce-api/current/exchange-sellers-auth
  - https://apicenter.commerce.naver.com/docs/solution-doc/3000/%EA%B8%B0%EB%B3%B8-%EC%97%B0%EB%8F%99-%EC%9A%94%EC%86%8C-%EA%B0%80%EC%9D%B4%EB%93%9C
- upstream_version: `2.88.0`
- retrieved_at: `2026-09-14`
- verified_at: `null`
- review_due: `2026-10-14`

Secondary official technical-support evidence:

- https://github.com/commerce-api-naver/commerce-api/discussions/3339
  - `내 스토어 애플리케이션` uses `type=SELF`
  - token response does not provide seller `account_id`
- https://github.com/commerce-api-naver/commerce-api/discussions/3557
  - `내 스토어 애플리케이션` re-authentication is a web/manual action
  - application authentication validity is 180 days from authentication
- https://github.com/commerce-api-naver/commerce-api/discussions/1564
  - application secret reissue immediately invalidates the old secret
- https://github.com/commerce-api-naver/commerce-api/discussions/357
  - token-signature timestamp must be millisecond Unix time
  - request timestamp is valid only within the documented time window
  - system clock should be synchronized
- https://github.com/commerce-api-naver/commerce-api/discussions/3751
  - token requests must use the strict request-body format
  - `SELF` MUST NOT include `account_id`
  - `grant_type=client_credentials` is required

Freshness rule:

- Any upstream Commerce API version change, token endpoint schema change, authentication-policy change, application re-authentication policy change, or account-mode change SHALL trigger immediate review.
- `review_due` is a fallback calendar bound in case change detection is absent or broken.
- Reading upstream documentation is not runtime verification.

---

## 1. Purpose

This document defines how ICBM establishes, stores, renews, invalidates, and recovers SmartStore authentication.

`ACCOUNT_IDENTITY.md` defines **which remote account the current authentication represents**.

This document defines **how that authentication is maintained safely over time**.

The contracts are coupled:

`token issuance success != AUTH_READY`

`valid bearer token != account identity proof`

`AUTH_READY = valid current authentication session + current-session account identity proof + required auth invariants`

---

## 2. M2 integration mode

### 2.1 Current M2 baseline

M2 SHALL target NAVER Commerce API `내 스토어 애플리케이션` behavior using:

- `grant_type=client_credentials`
- `type=SELF`
- no `account_id` parameter in the token request

This mode represents a single SmartStore connected to the current own-store application.

The token response itself does not prove which seller account is represented.

Therefore account proof MUST remain delegated to `ACCOUNT_IDENTITY.md` using the authenticated protected read:

`GET /v1/seller/account`

### 2.2 SELLER mode is not interchangeable

NAVER also documents `type=SELLER` for Commerce Solution scenarios.

That mode requires seller-specific `account_id` and has different lifecycle and authorization semantics.

ICBM MUST NOT silently switch between `SELF` and `SELLER`.

Supporting `SELLER` later requires a separate ADR/contract review covering at least:

- seller subscription / authorization lifecycle;
- account binding;
- SELLER token issuance;
- per-seller token lifetime tracking;
- revocation / unsubscribe behavior;
- capability and permission mapping.

For M2, `OWN_STORE_SELF` is the frozen baseline unless superseded by an explicit ADR.

---

## 3. Authentication protocol

SmartStore Commerce API authentication is OAuth 2.0 Client Credentials Grant for server-to-server integration.

The token endpoint is:

`POST https://api.commerce.naver.com/external/v1/oauth2/token`

The request MUST use:

`Content-Type: application/x-www-form-urlencoded`

For the M2 `SELF` baseline, the request body contract is:

- `client_id`
- `timestamp`
- `client_secret_sign`
- `grant_type=client_credentials`
- `type=SELF`

`account_id` MUST NOT be included for `SELF`.

Authentication requests MUST NOT place these required values in the URL query string.

ICBM SHALL treat the provider request shape as a strict protocol contract, not a best-effort format.

---

## 4. Electronic signature

NAVER does not require the plaintext application secret to be transmitted directly in the token request.

A signature is generated from:

- application `client_id`;
- current millisecond Unix `timestamp`;
- application `client_secret`.

The signature generation contract follows the provider documentation:

1. concatenate `client_id`, `_`, and `timestamp`;
2. bcrypt using `client_secret` as the salt value according to NAVER's documented format;
3. Base64 encode the resulting bcrypt value;
4. send the result as `client_secret_sign`.

The timestamp used in the token request MUST be the same timestamp used to create the signature.

ICBM MUST generate signatures only when needed and MUST NOT persist generated signatures as reusable credentials.

---

## 5. Clock requirement

Token issuance depends on a current timestamp.

Official technical guidance states that:

- timestamps must be millisecond Unix time;
- timestamps cannot be in the future relative to provider time;
- stale timestamps outside the provider's documented validity window are rejected;
- NTP/system clock synchronization is recommended.

Therefore system clock validity is part of the authentication invariant.

ICBM SHALL NOT respond to timestamp/signature failures by generating an unbounded stream of new signatures.

A repeated timestamp-related failure must surface diagnostics indicating possible clock skew.

Clock skew is not evidence that credentials are invalid.

---

## 6. Token response contract

The documented successful token response contains:

- `access_token`
- `expires_in`
- `token_type`

The access token is used as:

`Authorization: Bearer {access_token}`

The token response does not provide the authenticated seller identity.

ICBM MUST NOT derive canonical account identity from token issuance alone.

---

## 7. Access-token lifetime

Current upstream guidance documents:

- base token lifetime: 180 minutes / 10,800 seconds;
- an expired token cannot be used for API calls;
- a new token may be issued when the remaining lifetime is below 30 minutes;
- the previous token remains valid until its own expiry when a new token is issued;
- SELLER token lifetime is tracked separately per seller account in SELLER mode.

ICBM MUST derive each token's local expiry from the actual returned `expires_in` value rather than hard-coding exactly 10,800 seconds.

The 180-minute value is provider policy context, while `expires_in` is the per-response runtime fact.

---

## 8. No refresh-token lifecycle

The current Commerce API client-credentials documentation does not define a `refresh_token` response or refresh-token grant flow.

Provider guidance for expired/invalid authentication is to call the token-issuance API again using the client credentials.

Therefore M2 SHALL model SmartStore authentication as:

`durable application credentials -> short-lived bearer token -> token reissuance`

not:

`access token -> rotating refresh token -> refreshed access token`

Consequences:

- there is no documented rotating refresh token to consume;
- there is no refresh-token replacement transaction;
- a crash cannot lose a newly rotated refresh token because no such artifact exists in the current contract;
- automatic access-token reissuance does not require interactive user consent while the application credentials and application authorization remain valid.

This does NOT mean SmartStore authentication is permanently user-action-free.

The separate 180-day application re-authentication lifecycle remains human-driven.

---

## 9. Application-level authentication lifecycle

`내 스토어 애플리케이션` has a lifecycle distinct from the 3-hour bearer token.

Official technical support states that application authentication is valid for 180 days from authentication and re-authentication is performed after web login in Commerce API Center by the store's integrated manager.

Therefore ICBM SHALL distinguish:

### Access-token renewal

- short-lived;
- automatic;
- based on existing application credentials;
- no interactive approval expected during normal renewal.

### Application re-authentication

- long-lived provider authorization lifecycle;
- approximately 180-day validity according to current provider guidance;
- requires integrated-manager human action in the provider UI;
- cannot be treated as ordinary automatic token renewal.

If application re-authentication becomes required, ICBM MUST fail closed rather than repeatedly attempting bearer-token issuance forever.

The M2 state-convergence contract MUST represent this as a distinct human-action condition.

It MUST NOT be misclassified as `AUTH_MISMATCH`.

It also MUST NOT be mislabeled `ACCOUNT_RESTRICTED` unless the provider actually reports an account restriction.

The frozen human-remediation reason is `APPLICATION_REAUTH_REQUIRED` as defined by `CAPABILITY_MAPPING.md`. However, until `SMARTSTORE-R0-APP-REAUTH` is accepted and establishes a trustworthy machine-observable detection contract, automatic assignment of that reason remains disabled; unresolved suspected application re-authentication must fail closed without fabricating the reason.

---

## 10. Credential classes

ICBM SHALL classify SmartStore authentication artifacts as follows.

### Durable credential-grade secrets

- `client_secret`
- any provider-issued replacement `client_secret`

### Durable sensitive authentication metadata

- `client_id`
- configured authentication mode (`SELF`)
- provider/application identifiers needed to bind the credential bundle

Although `client_id` is not a bearer token, ICBM SHALL protect it together with the credential bundle because NAVER treats application authentication information as sensitive operational data.

### Short-lived credential-grade secrets

- bearer `access_token`

### Ephemeral derived authentication material

- `client_secret_sign`
- request `timestamp`

Derived signatures SHALL NOT be persisted for reuse.

---

## 11. Storage contract

The M1 credential-storage standard applies to SmartStore.

Credential-grade material MUST NOT be stored in plaintext application configuration, logs, ordinary SQLite text columns, crash dumps, or diagnostic exports.

ICBM SHALL store durable credential material as an encrypted credential blob.

The encryption key or wrapping key MUST be protected by an OS-protected credential/key facility rather than stored next to the ciphertext.

At minimum the protected authentication record must atomically bind:

- marketplace account reference;
- provider=`SMARTSTORE`;
- auth mode=`SELF`;
- `client_id`;
- encrypted `client_secret`;
- credential generation;
- metadata required to validate ownership/version of the record.

Bearer access tokens, when persisted, MUST receive the same credential-grade protection.

---

## 12. Bearer-token persistence baseline is provisional

Provider documentation describes a renewal window, but the exact real behavior relevant to restart recovery is still pending measurement in §24.3.

There is a security/availability tradeoff:

- persisting a bearer token permits restart reuse but leaves encrypted bearer material at rest;
- not persisting a bearer token reduces at-rest exposure but requires safe, predictable reissuance behavior after every restart.

Until runtime evidence proves that non-persistent recovery is safe across the relevant token-lifetime states, M2 SHALL use secure persistence of the current committed token bundle as a **provisional fail-safe baseline**.

This is not a claim that persistence is the permanently preferred security design.

The persistence decision MUST be reviewed after §24.3 renewal-window measurements. If evidence shows that a non-persistent strategy can reliably re-establish a valid session without violating provider issuance constraints or creating an unsafe retry pattern, an ADR/contract update MAY replace this baseline to reduce bearer-token-at-rest exposure.

ICBM MUST NOT remove persistence merely because client credentials are available; the provider's issuance behavior outside the renewal window must first be measured or explicitly guaranteed upstream.

While persistence remains the active baseline, a token bundle SHALL contain at least:

- encrypted `access_token`;
- `token_type`;
- response `expires_in`;
- calculated `expires_at`;
- credential generation;
- session generation;
- token-response commit timestamp;
- provider/mode binding.

The token bundle is credential-grade state and must be committed atomically.

---

## 13. Authentication generations

`ACCOUNT_IDENTITY.md` requires account-identity evidence to belong to the current authentication/session generation.

This document defines those generations.

### 13.1 `credential_generation`

A monotonically changing logical generation for the committed durable application credential bundle.

It changes when the trusted credential basis changes, including at least:

- configured application credentials are replaced;
- `client_secret` is replaced/reissued and ICBM commits the replacement;
- authentication mode changes;
- the credential bundle is explicitly re-bound to a different marketplace account contract.

Changing `credential_generation` invalidates all previous bearer-token and identity-proof evidence for READY purposes.

### 13.2 `session_generation`

A logical generation for the currently committed bearer-token session.

A successful token-issuance response does not become the current session merely because NAVER returned HTTP success.

The new session becomes current only after the token bundle is durably and atomically committed.

Every committed token-issuance response SHALL establish a new `session_generation`, even if a provider happens to return a token value equal to a previous token value.

The generation tracks the ICBM trust transaction, not token-string uniqueness.

### 13.3 Identity proof binding

Every account-identity proof SHALL record:

- `credential_generation`;
- `session_generation`;
- observed `accountUid`;
- proof timestamp;
- protected-read result/evidence reference.

`AUTH_READY` requires the proof generations to equal the current committed generations.

Formally:

`proof.credential_generation == current.credential_generation`

and

`proof.session_generation == current.session_generation`

A proof from an older token session MUST NOT make a newer token session READY.

---

## 14. Token acquisition transaction

Token issuance is a distributed transaction between NAVER and local durable state.

ICBM SHALL model these phases explicitly:

1. load current committed credential generation;
2. generate timestamp/signature;
3. call token endpoint;
4. validate HTTP/result schema;
5. construct candidate token bundle;
6. atomically persist candidate token bundle as the new session generation;
7. only after durable commit, perform `GET /v1/seller/account` identity proof;
8. only after identity proof matches the canonical account may AUTH converge to READY.

A token response received in memory is not sufficient durable evidence.

`token_response_received != session_committed`

---

## 15. Renewal policy

ICBM SHALL use the provider's returned `expires_in` and a conservative renewal policy inside the documented less-than-30-minute renewal window.

The exact proactive renewal margin is an ICBM operational policy and MUST be configuration/contract driven rather than inferred from a hard-coded provider guarantee.

Requirements:

- do not intentionally request replacement tokens outside the provider-supported renewal window;
- serialize token renewal per credential/account binding;
- do not permit multiple local workers to independently create competing token-session commits;
- after a replacement token is committed, re-run account identity proof for the new session generation;
- old persisted token material must be retired only after the new committed session is durable.

For the current M2 topology, renewal serialization depends on ADR-0006 plus ADR-0002: one ICBM process owns a data directory and that process has a single in-process JobWorker. That topology prevents multiple local workers from independently issuing and committing competing sessions for the same owned data directory.

This dependency is explicit and MUST NOT be treated as a permanent concurrency guarantee.

If the worker/process topology is changed so that multiple workers or processes can issue tokens for the same credential/account binding, the topology change MUST introduce a cross-worker single-flight/lease mechanism equivalent in safety purpose to M1 `SingleFlightAuth` before the change is accepted.

ICBM SHALL NOT depend on undocumented provider behavior for concurrent token issuance.

---

## 16. `GW.AUTHN` fallback

NAVER recommends reissuing a token after a `401` response with gateway code `GW.AUTHN`, because the token may be expired.

ICBM SHALL support bounded automatic recovery.

However authentication recovery and operation replay are separate decisions.

### For non-mutating protected reads

A single automatic token-reissue attempt followed by a single retry MAY be performed.

If the newly committed token passes identity proof, the read may be retried.

### For mutating requests

The generic auth layer MUST NOT blindly replay a write merely because token reissuance succeeded.

Write replay must follow the marketplace operation's idempotency/read-back contract.

This prevents an authentication helper from accidentally duplicating a mutation when remote execution status is uncertain.

### Loop guard

Automatic auth recovery must be bounded.

A fresh-token attempt that continues to produce authentication failure MUST NOT enter an infinite reissue/retry loop.

When the configured retry budget is exhausted, state converges to a safe non-READY/human-visible condition such as the already defined `PAUSED / AUTH_RETRY_LIMIT` contract.

---

## 17. Crash/restart convergence

Persisted state strings are not truth.

Restart handling SHALL be based on credential, token, expiry, generation, and identity-proof invariants.

### Case A: valid committed token survives restart

This case describes the current provisional persistence baseline from §12. If §24.3 evidence later supports an approved non-persistent bearer strategy, this case and the implementation contract MUST be revised before that strategy is adopted.

- load encrypted credential/token bundles;
- verify local bundle integrity;
- verify token not locally expired;
- do NOT trust persisted `AUTH_READY` by itself;
- call `GET /v1/seller/account` again;
- if identity matches the canonical account under the same generations, converge to READY.

### Case B: token expired during downtime

- use the current committed application credential generation;
- request a new access token;
- atomically commit a new session generation;
- perform fresh account identity proof;
- READY only after proof matches.

No user approval is normally required for this short-lived token recovery.

### Case C: crash during planned renewal before provider response

No local session transition occurred.

The previous committed token remains canonical.

Retry may occur according to normal renewal rules.

### Case D: provider returned a new token, crash occurred before local commit

The candidate token is uncommitted and MUST NOT be assumed current after restart.

The previous committed token remains the only locally trusted session.

Current provider guidance states that the old token remains valid until its own expiry after normal replacement, which makes planned renewal recoverable while the old token is still valid.

If no usable previous committed token exists, ICBM has an externally successful / locally uncommitted token transaction.

This is an uncertainty state, not evidence of credential failure.

ICBM must remain non-READY until a valid token session can be re-established and committed.

### Case E: first-ever token issuance succeeded remotely, crash occurred before first local commit

This is a special distributed-commit uncertainty.

There is no previous committed token to fall back to.

Current documentation does not explicitly guarantee what an immediate second token request returns while an unobserved active token may still exist outside the documented replacement window.

Therefore ICBM MUST NOT invent a recovery guarantee.

This behavior is a required M2 runtime verification case.

Until measured, convergence is fail-closed and non-READY.

It MUST NOT be escalated directly to "user re-approval required" merely because the local process lost the response.

### Case F: crash after local token commit but before identity proof

The token session exists, but AUTH is not READY.

After restart:

- load the committed session;
- perform account identity proof;
- READY only after proof succeeds and matches.

### Case G: crash after identity proof but before READY state persistence

On restart, the persisted READY string is irrelevant.

Re-evaluate invariants and perform a current protected read before READY convergence.

---

## 18. Secret rotation

NAVER allows the application secret to be reissued.

Official guidance states that the previous application secret becomes unusable immediately after reissue.

Therefore a secret replacement is a credential-generation boundary.

ICBM SHALL implement replacement as an atomic credential-bundle transition.

The new secret MUST NOT be partially installed while an old `client_id`/metadata binding remains inconsistently committed.

On a successfully committed local secret replacement:

- increment `credential_generation`;
- invalidate previous token session for READY purposes;
- invalidate previous account-identity proof;
- acquire/commit a token under the new credential generation;
- re-prove account identity.

If an external secret rotation occurs outside ICBM, ICBM may not discover it until token issuance is attempted.

A still-valid bearer token is not proof that the persisted application secret can mint the next token.

When reissuance later fails deterministically due to rejected application credentials, ICBM must surface a human-action authentication failure rather than looping indefinitely.

---

## 19. Partial credential writes

A partially persisted credential replacement is not a credential generation.

The same M1 invariant applies:

`credential_commit_not_proven -> previous_committed_generation_or_unconfigured`

ICBM MUST NOT construct a hybrid credential bundle from fields written across different generations.

Examples of forbidden hybrid state:

- new `client_secret` + old generation metadata;
- new `client_id` + old secret;
- updated account binding + old credential generation;
- new token session attributed to an uncommitted credential generation.

Atomicity applies to the logical credential bundle, not just each database column.

---

## 20. Failure classification

Authentication failures MUST be classified before deciding whether to retry.

### Automatically recoverable candidates

- expired bearer token;
- transient network failure;
- provider transient server failure;
- rate-limited condition when the documented retry window is known and bounded.

These map to the M2 automatic-recovery classes such as `TRANSIENT` or `RATE_LIMITED`.

### Human/system action required candidates

- application secret was externally reissued and local secret is obsolete;
- provider application re-authentication is due/required;
- application is inactive/disabled;
- repeated deterministic signature failure after implementation/config validation;
- retry limit exhausted;
- credential bundle cannot be decrypted because the expected OS-protected key is unavailable;
- `AUTH_MISMATCH` from account identity proof.

These MUST NOT continue automatic infinite recovery.

### Not enough evidence to classify as credential failure

- one network timeout during token issuance;
- token response received but local commit outcome unknown;
- provider returned an undocumented error shape once;
- clock skew suspected;
- temporary IP/rate gateway error.

Unknown is a valid result.

ICBM must not turn uncertainty into a false statement such as "credentials invalid".

---

## 21. Application request-format safety

NAVER has strengthened validation of token-issuance request format.

ICBM MUST maintain a single canonical request builder for the token endpoint.

For M2 `SELF` requests it SHALL enforce:

- POST body, not query parameters;
- `application/x-www-form-urlencoded`;
- exact `grant_type=client_credentials`;
- exact `type=SELF`;
- no `account_id` field;
- required `client_id`, `timestamp`, and `client_secret_sign` fields.

Call sites MUST NOT hand-build variant token requests.

The request contract should be static-testable against the platform capability mapping once that document exists.

---

## 22. Sensitive-data and logging rules

The following MUST never appear in plaintext logs, telemetry, screenshots, PR evidence, exported diagnostics, or exception messages:

- `client_secret`;
- complete `client_id`;
- complete `access_token`;
- complete `client_secret_sign`;
- decrypted credential blobs.

Safe diagnostics MAY include:

- token endpoint HTTP status;
- provider error code;
- provider trace ID;
- credential generation;
- session generation;
- token expiry timestamp;
- remaining lifetime;
- whether identity proof passed;
- masked `client_id` where application-level disambiguation is required;
- masked provider account UID where needed for human review.

Logging/evidence fields SHALL follow an allowlist model. A newly introduced authentication field is not safe to log merely because it is absent from the explicit deny list.

Evidence must prove behavior without exposing the credential itself.

---

## 23. Relationship to `ACCOUNT_IDENTITY.md`

Authentication and account identity are separate gates.

The required sequence is:

`credential bundle`

`-> token issuance / committed session generation`

`-> GET /v1/seller/account`

`-> accountUid comparison`

`-> AUTH_READY`

The following are forbidden shortcuts:

`token issued -> READY`

`token persisted -> READY`

`token valid by local expiry -> READY`

`store display name matches -> READY`

A bearer token may be technically valid while representing the wrong intended account.

Only current-session identity proof closes that gap.

---

## 24. Acceptance requirements

M2 SmartStore auth acceptance MUST include the short-horizon CONNECT cases in this section. Long-horizon provider observations are also recorded here, but §24.3 and §24.16 are explicitly non-gating for the M2 milestone when their real provider conditions are not yet practically observable.

`M2 acceptance requirements != full AUTH contract verification requirements`

Passing the M2-gating cases in §24 does not imply that §25 has been satisfied or that this contract's `verified_at` may be populated.

### 24.1 Initial happy path

- load valid committed application credentials;
- generate valid signature;
- obtain token;
- verify response schema;
- atomically commit token session;
- call `GET /v1/seller/account`;
- identity matches expected `provider_account_uid`;
- AUTH becomes READY.

### 24.2 Token lifetime measurement

Record from the real response:

- `expires_in`;
- calculated expiry;
- observed token type;
- issuance timestamp.

Confirm runtime behavior is consistent with current documented policy.

### 24.3 Long-horizon renewal-window behavior and persistence-policy review

Measure:

- behavior while token has more than 30 minutes remaining;
- behavior after remaining lifetime enters the documented renewal window;
- whether provider returns a new token or reuses an existing token value;
- whether the old token remains usable until its documented expiry;
- behavior when ICBM has valid application credentials but intentionally does not reuse the existing bearer token;
- whether an immediate safe session can be re-established after restart at representative points in the token lifetime.

ICBM MUST NOT assume undocumented behavior here.

This measurement is required for full AUTH contract verification and for deciding whether the provisional persistence baseline in §12 should change. It MAY be executed in a separate long-horizon observation session and is not, by itself, a blocker for `M2 ACCEPTED`.

Until the measurement and review are complete, secure bearer-token persistence remains the provisional M2 baseline and the renewal-window behavior remains documentation-derived rather than runtime-verified.

The results of this measurement MUST trigger an explicit review of the provisional bearer-token persistence baseline in §12.

If non-persistent recovery is demonstrated to be reliable and bounded, the review SHALL compare the reduced at-rest attack surface against any availability/rate-limit cost and decide whether persistence remains justified. Until that review is recorded, secure persistence remains the M2 baseline.

### 24.4 No-refresh-token proof

Capture the real token response schema and confirm that M2 receives no refresh-token lifecycle artifact.

If upstream later introduces one, this contract requires review before adoption.

### 24.5 `GW.AUTHN` recovery

- use an expired/invalid test token condition without corrupting durable application credentials;
- receive the expected authentication failure;
- perform one bounded token reissuance;
- commit new session;
- re-prove identity;
- recover to READY.

The test MUST NOT intentionally damage the real application secret merely to trigger an auth failure.

### 24.6 Retry-loop guard

Induce a safe repeatable failure using a controlled harness/mocked boundary where possible.

Verify:

- retry count is bounded;
- state leaves automatic retry after the budget;
- no runaway token requests occur.

### 24.7 Restart with valid persisted token

This acceptance case validates the current provisional persistence baseline; it is not evidence that persistence is permanently required.

- commit token session;
- restart process;
- load/decrypt token bundle;
- perform fresh protected account read;
- converge to READY only after current identity proof.

### 24.8 Restart after token expiry

- simulate/measure expired token state;
- restart;
- reissue token using application credentials;
- commit new session generation;
- re-prove identity;
- converge correctly.

### 24.9 Crash before token response

Verify no local generation transition is committed.

### 24.10 Crash after token response but before token commit

Verify the returned candidate token is not treated as committed after restart.

If a previous committed token remains valid, verify it remains the recovery base.

### 24.11 First-token uncertain commit

Exercise the special case where remote issuance may have succeeded but no first local token commit exists.

Measure immediate reissue behavior.

This case is required before claiming crash-safe unattended recovery.

### 24.12 Crash after token commit before identity proof

Verify AUTH remains non-READY until identity proof runs.

### 24.13 Credential replacement atomicity

Test complete old bundle and complete new bundle transitions.

Verify no hybrid state can become READY.

### 24.14 Secret rotation behavior

Using a safe controlled credential-rotation plan, verify:

- old secret rejection after provider reissue;
- local credential generation replacement;
- old token/proof invalidation for READY purposes;
- new token acquisition;
- identity proof.

This test requires explicit operational approval because it changes live provider credentials.

### 24.15 Clock-skew handling

Use a controlled test boundary rather than changing the workstation's real system time when possible.

Verify timestamp errors are diagnosed and bounded rather than causing retry storms.

### 24.16 Long-horizon application re-authentication lifecycle

The application re-authentication lifecycle is a required long-horizon AUTH evidence target, but observing the real 180-day condition is not a blocker for the M2 CONNECT milestone while that condition is not practically observable.

`SMARTSTORE-R0-APP-REAUTH` MAY therefore remain `BLOCKED_BY_TIME` after `M2 ACCEPTED`.

Until that slot is accepted:

- `APPLICATION_REAUTH_REQUIRED` remains schema-reserved;
- automatic detection of that durable reason remains disabled;
- generic `401`, `GW.AUTHN`, token failure, or application age MUST NOT be promoted into `APPLICATION_REAUTH_REQUIRED`;
- unresolved suspected application re-authentication remains fail-closed under the capability/state contract.

When a trustworthy real/provider-supported condition later becomes available, capture evidence of:

- where the provider displays application authentication validity/deadline;
- current 180-day behavior;
- what machine-visible API error/state appears when re-authentication is actually required;
- the correct ICBM human-action state transition and safe recovery path.

Do not manufacture the condition merely to close M2. Do not wait for production expiry without a monitoring/reminder strategy.

---

## 25. Runtime evidence required before full-contract `verified_at`

§24 defines M2 acceptance cases and identifies the long-horizon observations that may be deferred from the milestone. This section defines the stricter **full AUTH contract verification** boundary.

`M2 ACCEPTED does not imply AUTH.md verified_at != null.`

`verified_at` MUST remain null until real SmartStore measurements cover at least:

- token request method/path/content type;
- real successful response schema;
- actual `expires_in`;
- current M2 token mode (`SELF`);
- restart reuse of a committed token under the provisional persistence baseline;
- renewal-window behavior and recorded persistence-policy review (`SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`);
- account identity proof under the committed session generation;
- bounded recovery from a safe auth-failure condition;
- first-token uncertain-commit behavior or an explicitly documented unresolved limitation;
- application re-authentication's trustworthy machine-visible condition and safe recovery behavior (`SMARTSTORE-R0-APP-REAUTH`).

Therefore the following may be a valid, honest post-M2 state:

```text
M2 status = ACCEPTED
AUTH.md verified_at = null
SMARTSTORE-R0-TOKEN-REISSUE-WINDOW = PENDING/DEFERRED
SMARTSTORE-R0-APP-REAUTH = BLOCKED_BY_TIME
```

Evidence must be sanitized and must not expose credential material.

---

## 26. Open runtime questions

The following MUST remain explicit unknowns until measured or documented by upstream.

### Q1. Immediate reissue after a lost first issuance response

If NAVER accepted the first token issuance but ICBM crashed before persisting it, what does an immediate second token request return while the first remote token is still early in its lifetime?

Current contract status: `UNKNOWN`.

This affects unattended crash recovery availability, not account-integrity safety.

### Q2. Exact provider-visible state/error when 180-day application re-authentication becomes mandatory

Current support guidance confirms the human re-authentication lifecycle, but the exact machine-observable API failure contract must be measured/documented before final state mapping.

Current contract status: `UNKNOWN`.

### Q3. Concurrent token issuance behavior

M2 will serialize local issuance regardless, but provider behavior under concurrent requests is not required as a safety assumption.

Current contract status: `DO_NOT_DEPEND_ON`.

---

## 27. Final M2 authentication contract

For SmartStore `OWN_STORE_SELF`:

`application credentials = durable root authentication material`

`access token = short-lived bearer session`

`refresh token = not part of the current documented flow`

`token renewal = client-credentials token reissuance`

`normal token renewal != user re-approval`

`180-day application re-authentication = separate human-action lifecycle`

`new token response != committed session`

`committed session != AUTH_READY`

`AUTH_READY = current committed credential generation + current committed session generation + fresh matching account identity proof + satisfied auth invariants`

`persisted READY string != truth`

`credential/session proof wins over persisted state`

`bearer-token persistence = provisional M2 baseline pending §24.3 measurement and review`

All unknown provider behavior must remain UNKNOWN until measured rather than being promoted into implementation assumptions.