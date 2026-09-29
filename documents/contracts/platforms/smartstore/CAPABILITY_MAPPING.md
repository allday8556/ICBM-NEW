# SmartStore Capability Mapping and State-Convergence Contract

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M2_CAPABILITY_CONVERGENCE_FROZEN_FOR_REVIEW` |
| Integration mode | `OWN_STORE_SELF` |
| Capability layers | `auth -> write_scope -> write` |
| Contract freshness | independent axis |
| Human-action workflow | scoped `PAUSED` / `REVIEW_REQUIRED` overlays |
| M2 product write | `UNVERIFIED` — product endpoints remain `NOT_ADOPTED` |
| Upstream version | `2.88.0` |
| Retrieved at | `2026-09-14` |
| Verified at | `null` |
| Review due | `2026-10-14` |

## Provenance

This document converges the already-reviewed SmartStore contracts and does not replace them:

- `ACCOUNT_IDENTITY.md`
- `AUTH.md`
- `PERMISSIONS_SCOPES.md`
- `ERRORS.md`
- `ENDPOINT_MATRIX.md`
- `SOURCES.md`

Stable source/evidence IDs are reused from `SOURCES.md`.

Core provenance rule:

`capability state is derived from current evidence, not from a persisted badge or documentation statement alone`

---

## 1. Purpose

This document defines the final SmartStore M2 relationship among:

- account identity;
- authentication lifecycle;
- provider permission evidence;
- actual write capability;
- endpoint adoption;
- error classification;
- contract freshness;
- scoped human-action workflow;
- UI presentation.

It intentionally uses independent axes plus invariants rather than a Cartesian state table.

---

## 2. Independent axes

### 2.1 `auth`

`auth` answers whether the current committed credential/session generation proves the intended SmartStore account.

M2 states:

- `READY`
- `NOT_READY`
- `AUTH_MISMATCH`
- `NOT_BOUND`

`READY` requires the full `AUTH.md` + `ACCOUNT_IDENTITY.md` proof chain.

### 2.2 `write_scope`

`write_scope.status` remains:

- `READY`
- `MISSING`
- `UNKNOWN`

For SmartStore it means provider API-group permission evidence, not OAuth scope strings.

Its evidence envelope includes at least:

- `evidence_source`;
- `evidence_strength`;
- `observed_at`;
- `application_fingerprint`;
- `required_groups`;
- `observed_groups`;
- `endpoint_mapping_revision`;
- `attested_status` — persisted record-time classification (`READY | MISSING`);
- the freshness-policy bound in effect at recording — persisted;
- `freshness_status` — derived at evaluation from the injected clock, never persisted (`PERMISSIONS_SCOPES.md` §5, §8.1).

### 2.3 `write`

`write.status` remains:

- `UNVERIFIED`
- `READY`
- `BLOCKED`

`READY` requires the operation-specific real mutation plus required external read-back/reconciliation proof.

For product registration this belongs to M5, not M2.

### 2.4 `contract_freshness`

Values:

- `UNRECORDED`
- `CURRENT`
- `STALE`
- `REVIEW_REQUIRED`

This axis describes the freshness of ICBM's adopted provider contract understanding, not runtime capability truth.

`UNRECORDED` is bootstrap-only and means no freshness determination has yet been recorded for this adopted contract. It asserts neither currency, expiry, nor contradiction. Once a state leaves `UNRECORDED`, it MUST NOT return to `UNRECORDED`.

A recorded freshness determination also carries durable `freshness_recorded_at`. The actor who made the determination MUST remain traceable through the append-only audit record; duplicating `recorded_by` in the capability row is optional.

The combination below is valid:

`contract_freshness=STALE + auth=READY`

when runtime auth proof remains valid.

### 2.5 Human-action workflow overlay

`workflow_state` is a scoped action overlay, not a provider error class and not the durable Job state machine.

M2 human-action states:

- `PAUSED`
- `REVIEW_REQUIRED`

### 2.6 `workflow_scope` — M2 frozen enum

A human-action overlay MUST carry a typed `workflow_scope`. It MUST NOT be an adapter-defined free-form string.

For SmartStore M2, the set is closed to:

- `AUTHENTICATION`
- `PRODUCT_REGISTRATION`

Semantics:

- `AUTHENTICATION` covers token/session/account-proof/application-authorization recovery.
- `PRODUCT_REGISTRATION` covers product-registration permission/write readiness only.

A new scope requires architecture/contract review.

Adapters MUST NOT invent provider-specific workflow-scope strings.

UI/domain routing MUST use the enum, not localized labels or string matching.

---

## 3. Fundamental separation rules

The following equivalences are forbidden:

`auth READY == write_scope READY`

`write_scope READY == write READY`

`write READY == contract_freshness CURRENT`

`error_class == workflow_state`

`provider error code == reason_code`

`workflow_scope == provider endpoint string`

`persisted READY == measured READY`

A state on one axis may constrain another only through an explicit invariant.

---

## 4. Authentication invariants

### A1. Current proof required

`auth=READY` requires:

- current committed credential generation;
- current committed session generation;
- adopted protected seller-account read succeeds;
- required `accountUid` exists;
- observed `accountUid == expected.provider_account_uid`;
- proof belongs to current credential/session generations.

### A2. Authentication not READY blocks write READY

`auth != READY -> write != READY`

### A3. Identity mismatch

If strong identity differs:

- `auth=AUTH_MISMATCH`;
- `write!=READY`;
- `workflow_state=REVIEW_REQUIRED`;
- `workflow_scope=AUTHENTICATION`;
- `reason_code=null`;
- no automatic rebinding.

### A4. First binding incomplete

`binding_commit_not_proven -> auth=NOT_BOUND`

A fresh authenticated identity read is required before completing binding.

### A5. Authentication retry exhaustion

Retry-budget exhaustion does not change the measured `error_class`.

When bounded auth recovery is exhausted:

- `auth!=READY`;
- `workflow_state=PAUSED`;
- `workflow_scope=AUTHENTICATION`;
- `reason_code=AUTH_RETRY_LIMIT`.

---

## 5. Permission (`write_scope`) invariants

### S1. Evidence strength is part of permission truth

A bare permission READY presentation that hides strength is forbidden.

At minimum distinguish:

- `OPERATOR_ATTESTED`;
- `MACHINE_VERIFIED` if an official machine introspection mechanism exists later.

Operator-attested evidence is classified as `A0 / OPERATOR_ATTESTED_EVIDENCE` in `SOURCES.md`. It is not R0 measured-runtime evidence and cannot be promoted to R0 without a separate provider measurement.

### S2. Positive missing evidence blocks the affected write

If product-registration permission evidence positively proves a required group absent:

- `write_scope=MISSING`;
- product-registration `write=BLOCKED`;
- `workflow_state=PAUSED`;
- `workflow_scope=PRODUCT_REGISTRATION`;
- `reason_code=SCOPE_INSUFFICIENT`.

Authentication may remain `READY`.

### S3. Unknown is not missing

`write_scope=UNKNOWN != SCOPE_INSUFFICIENT`

Generic `GW.AUTHN`, `403`, timeout, or absent attestation does not prove a missing group.

### S4. Scope READY does not prove write READY

`write_scope=READY != write=READY`

### S5. A successful write does not backfill declared scope

After M5 this combination may be valid:

`auth=READY / write_scope=UNKNOWN / write=READY`

when bounded mutation/read-back proof succeeds while provider-declared permission evidence remains unavailable.

### S6. Expired or invalidated permission evidence converges to UNKNOWN

Permission evidence supports the current `write_scope` only while it is current (`PERMISSIONS_SCOPES.md` §8). Once it expires or is otherwise invalidated:

- `write_scope=UNKNOWN`, whether the evidence originally said `READY` or `MISSING`;
- if the capability held `MISSING` on that evidence, the `PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT` overlay that S2 derived from it is removed in the same convergence;
- `write` is not promoted: the transition means ICBM no longer has current evidence that the permission is absent, not that it became available;
- the stored evidence record is not mutated.

Removing this overlay is not ambiguity erasing a proven reason (§10.1): the reason's positive basis no longer exists. S3 still holds: `UNKNOWN` never produces `SCOPE_INSUFFICIENT`.

### S7. Time-driven convergence reuses the read-path convergence

Evidence expiry is the first capability transition that elapsed time alone can require. M2 adds no periodic scheduler or job for it.

```text
read / projection / attestation read
-> service obtains now from the injected Clock
-> pure evaluation derives current freshness + invalidations
-> converge capability if the derived write_scope differs from the persisted one
-> persist and audit only when durable capability/workflow state actually changes
```

- A read path may therefore cause a convergence write; the read itself is not audited.
- A durable change appends the canonical capability-change event (`MARKETPLACE_CAPABILITY_CHANGED`) through the normal service path, with safe detail sufficient to reconstruct the prior and resulting `write_scope` status/strength, the invalidation reason (for example `EXPIRED`), any overlay removed or added, and the transition time from the injected clock.
- Repeated reads after convergence are idempotent: no further state write and no duplicate transition event.
- A proactive scheduler that surfaces expiry before any read MAY be added later under a separate review; M2 does not require it.

---

## 6. Write-capability invariants

### W1. M2 product write is UNVERIFIED

During M2:

`product_registration.write=UNVERIFIED`

because required product mutation/read-back endpoints are `NOT_ADOPTED`.

No UI, persistence, permission evidence, or operator action may promote M2 product write to READY.

### W2. Write READY requires real operation proof

For product registration, READY requires at least:

- required endpoints are `ADOPTED`;
- `auth=READY`;
- operation-specific preconditions pass;
- bounded real CREATE is explicitly authorized;
- remote result is reconciled/read back;
- expected marketplace identity/state matches;
- durable evidence is retained.

CREATE response success alone is insufficient.

### W3. Unknown remote outcome forbids blind replay

`remote_outcome=UNKNOWN -> no blind mutation replay`

Reconciliation/read-back runs first. Unresolved ambiguity becomes:

- `workflow_state=REVIEW_REQUIRED`;
- `workflow_scope=PRODUCT_REGISTRATION`;
- `reason_code=null`.

---

## 7. Endpoint-adoption invariants

### E1. Adoption is the execution allow-list

`endpoint=NOT_ADOPTED -> network I/O forbidden`

regardless of auth, permission evidence, feature flags, or desired capability.

### E2. Capability evidence cannot adopt an endpoint

The following cannot change adoption:

- operator-attested `상품` permission;
- historical successful calls;
- feature flags;
- desired capability;
- unadopted probes.

Endpoint adoption requires reviewed contract change.

### E3. M2 adopted set

M2 executes exactly:

- `SMARTSTORE_AUTH_TOKEN`
- `SMARTSTORE_SELLER_ACCOUNT`

Product/category/image endpoints remain planning metadata only.

---

## 8. Contract-freshness invariants

### F1. Freshness is not runtime capability truth

Calendar/source age alone does not mechanically invalidate a still-current runtime proof.

`contract_freshness=STALE` does not by itself imply `auth!=READY` or `write!=READY`.

Freshness records the state of ICBM's adopted-contract understanding. It is not provider evidence and therefore does not use A0/R0-style `evidence_strength`.

### F2. UNRECORDED is bootstrap-only

`UNRECORDED` means no freshness determination has yet been recorded for this adopted contract.

It explicitly asserts **none** of the following:

- that the contract is current;
- that a prior freshness determination expired;
- that a material contradiction or evidence conflict exists.

A newly created capability state MUST begin `UNRECORDED`.

`UNRECORDED` blocks every expansion/new-trust decision that requires a current contract, while already-proven behavior may continue unless another independent axis blocks it.

`UNRECORDED` is non-reentrant: after the first reviewed freshness determination, no transition back to `UNRECORDED` is valid.

### F3. STALE has deterministic behavior

`STALE` means a freshness determination existed and has expired, or the adopted upstream version changed so that the existing determination no longer covers the current upstream contract, without a known material contradiction.

Therefore existing runtime-proven behavior may continue under its existing runtime proof.

While STALE, ICBM MUST block expansion/new-trust decisions that depend on the stale contract, including:

- adopting a new endpoint;
- widening the allow-list;
- promoting a previously unverified capability to READY;
- changing retry/replay semantics;
- changing required permission mappings;
- changing provider request/response assumptions;
- expanding application/account modes.

`STALE` itself does not require an extra human safety judgment for already-proven behavior.

### F4. REVIEW_REQUIRED freshness is stronger

`contract_freshness=REVIEW_REQUIRED` means a material contradiction, incompatible upstream change, or evidence conflict exists.

Automatic operations that depend on the disputed invariant MUST stop until review resolves it.

### F5. Freshness behavior matrix

The behavior is normative:

| `contract_freshness` | Expansion / new trust | Existing proven behavior | Behavior depending on disputed invariant |
| --- | --- | --- | --- |
| `CURRENT` | allow | allow | allow |
| `UNRECORDED` | block | allow | allow |
| `STALE` | block | allow | allow |
| `REVIEW_REQUIRED` | block | allow | block |

`UNRECORDED` and `STALE` intentionally have the same execution gate. They remain distinct because their claims are different: `UNRECORDED` means no determination has ever been recorded; `STALE` means a prior determination exists but is no longer current.

### F6. Freshness transition graph is closed

Allowed state-changing transitions are exactly:

```text
UNRECORDED -> CURRENT
CURRENT -> STALE
CURRENT -> REVIEW_REQUIRED
STALE -> CURRENT
STALE -> REVIEW_REQUIRED
REVIEW_REQUIRED -> CURRENT
```

Meaning:

- `UNRECORDED -> CURRENT` requires an explicit reviewed freshness recording;
- `CURRENT -> STALE` is used for actual review expiry or upstream-version change;
- `CURRENT/STALE -> REVIEW_REQUIRED` requires a real contradiction, incompatible upstream change, or evidence conflict;
- `STALE/REVIEW_REQUIRED -> CURRENT` requires reviewed revalidation.

Forbidden transitions include:

```text
UNRECORDED -> STALE
UNRECORDED -> REVIEW_REQUIRED
CURRENT/STALE/REVIEW_REQUIRED -> UNRECORDED
REVIEW_REQUIRED -> STALE
```

A state MUST NOT be moved merely to the "closest" enum value.

### F7. Same-value reviewed recordings are events, not silent no-ops

The following reviewed re-recordings are allowed:

```text
CURRENT -> CURRENT
STALE -> STALE
REVIEW_REQUIRED -> REVIEW_REQUIRED
```

They MUST update `freshness_recorded_at` and MUST append an audit record with the actor, even though the enum value is unchanged.

`UNRECORDED -> UNRECORDED` is forbidden because a successful freshness-recording action cannot truthfully record "not recorded".

The service/action path therefore MUST NOT rely only on `after != before` to decide whether a freshness recording is persisted and audited.

### F8. CURRENT recording owner and provenance

A transition to `CURRENT` is a reviewed statement about ICBM's adopted-contract understanding. It is not a machine measurement of NAVER.

Requirements:

- PR-B owns the typed transition, persistence semantics, and a local operator-authorized recording entry point that performs no SmartStore/provider call;
- the recording action MUST retain `freshness_recorded_at` and append audit provenance containing the actor;
- an operator action records that reviewed contract validation was completed; it MUST NOT be presented as provider-measured evidence;
- PR-A may consume the resulting freshness state but MUST NOT set, infer, seed, or fabricate `CURRENT`;
- PR-D may later provide the UI surface for the PR-B recording action, but PR-D is not required for PR-A to consume an already recorded value;
- no production caller may derive `CURRENT` merely from today's date being before `review_due`;
- no runtime Markdown parsing may establish freshness;
- automatic review-due or provider-drift detection remains a separately owned feature.

### F9. Freshness never invents provider truth

A stale, unrecorded, or disputed source cannot be converted into a favorable guarantee merely to preserve capability.

If runtime proof itself becomes invalid under its owning contract, that capability independently converges away from READY.

---

## 9. `PAUSED` reason-code contract — M2 frozen set

`PAUSED` means the affected scoped capability has a sufficiently understood human-remediable blocking condition.

The M2 reason set is closed to exactly four values:

| reason_code | Evidence requirement | workflow_scope | Expected remediation |
| --- | --- | --- | --- |
| `AUTH_RETRY_LIMIT` | bounded auth recovery exhausted | `AUTHENTICATION` | inspect/correct auth problem, then retry under contract |
| `APPLICATION_REAUTH_REQUIRED` | provider/runtime evidence positively establishes own-store application re-authentication is required | `AUTHENTICATION` | integrated manager completes NAVER provider-UI re-authentication, then fresh token/session/account proof |
| `SCOPE_INSUFFICIENT` | positive current evidence proves required API group absent | `PRODUCT_REGISTRATION` | enable required group(s), refresh permission/session evidence |
| `ACCOUNT_RESTRICTED` | provider evidence positively establishes an account/store restriction affecting the scoped operation | `AUTHENTICATION` or `PRODUCT_REGISTRATION` according to the proven restriction | resolve provider restriction, then re-prove affected capability |

### 9.1 Reason-code rules

- `PAUSED` requires non-null `reason_code` and non-null typed `workflow_scope`.
- reason must be one of the four frozen values.
- new reason or new workflow scope requires architecture review.
- adapters cannot invent provider-specific durable reason/scope strings.
- reason is not a replacement for `error_class`, provider code, trace, or evidence.
- ambiguous provider failures MUST NOT be guessed into a PAUSED reason.

### 9.2 `APPLICATION_REAUTH_REQUIRED` activation gate

The reason exists in the schema now, but its **automatic detection is not yet verified**.

Current provider support establishes the human re-authentication lifecycle, but the exact machine-observable expiry/failure signature remains unknown.

Therefore until `SMARTSTORE-R0-APP-REAUTH` is accepted:

- runtime failures MUST NOT be automatically mapped to `APPLICATION_REAUTH_REQUIRED` from age, generic `GW.AUTHN`, `401`, or token failure alone;
- suspected application re-authentication failures converge to `workflow_state=REVIEW_REQUIRED`, `workflow_scope=AUTHENTICATION`, `reason_code=null`;
- the user may be shown diagnostic guidance that application re-authentication is one hypothesis, but durable state must preserve uncertainty.

After `SMARTSTORE-R0-APP-REAUTH` establishes a trustworthy detection contract, a reviewed mapping may activate automatic convergence to `PAUSED / APPLICATION_REAUTH_REQUIRED`.

### 9.3 Error classes do not become reason codes

Canonical error classes answer what kind of failure evidence exists.

PAUSED reason codes answer which proven human-remediable operational condition blocks the scoped capability.

There is intentionally no one-to-one mapping.

---

## 10. `REVIEW_REQUIRED` contract

`workflow_state=REVIEW_REQUIRED` means automation cannot safely determine or execute the next action without human judgment or additional evidence.

It is the correct destination when a blocker is real but a safe PAUSED reason cannot be proven.

Examples:

- `AUTH_MISMATCH`;
- unresolved generic `GW.AUTHN`;
- suspected-but-unproven application re-authentication requirement;
- provider behavior contradicting permission evidence;
- local credential/security-store failure without a frozen remediation mapping;
- schema/contract drift affecting a safety invariant;
- unresolved mutation `remote_outcome=UNKNOWN`;
- unknown/fatal/conflict conditions requiring diagnosis.

The underlying `error_class`, provider code, trace ID, and evidence remain independently visible.

`error_class=REVIEW_REQUIRED` and `workflow_state=REVIEW_REQUIRED` remain independent axes.

### 10.1 Same-scope overlay convergence

A human-action overlay converges toward the strongest positively established remediation state for the same `workflow_scope`.

For the same scope:

```text
existing REVIEW_REQUIRED
+ later positive evidence proving one frozen PAUSED reason
=> replace with PAUSED/<proven reason>
```

Example:

```text
REVIEW_REQUIRED/AUTHENTICATION
+ AUTH_RECOVERY_EXHAUSTED
=> PAUSED/AUTHENTICATION/AUTH_RETRY_LIMIT
```

The reverse is not automatic:

```text
existing PAUSED/<proven reason>
+ later ambiguous evidence proposing REVIEW_REQUIRED
=> keep the existing PAUSED/<proven reason>
```

Ambiguity MUST NOT erase a previously proven human-remediable reason. This convergence rule does not authorize replacing one proven `PAUSED` reason with another without the owning evidence/remediation contract.

**A3 exception.** A `REVIEW_REQUIRED/AUTHENTICATION` overlay required by `AUTH_MISMATCH` under A3 is not eligible for automatic convergence to `PAUSED`, even when later evidence proves a frozen PAUSED reason. It remains until explicit operator review resolution. For example, `AUTH_MISMATCH` + `REVIEW_REQUIRED/AUTHENTICATION` + a later `AUTH_RECOVERY_EXHAUSTED` keeps the review; it never becomes `PAUSED/AUTHENTICATION/AUTH_RETRY_LIMIT`, whose `RESUME` would bypass the account review A3 requires.

This states the rule the runtime already enforces, under the named test `test_the_review_an_auth_mismatch_requires_is_never_narrowed_away` (Issue #29).

Expiry or invalidation of the evidence a `PAUSED` reason depends on is not ambiguity: S6 governs removal of an evidence-dependent `SCOPE_INSUFFICIENT` overlay.

---

## 11. Default error-to-workflow behavior

| Evidence/class situation | Default behavior | Scope | PAUSED reason |
| --- | --- | --- | --- |
| transient failure, retry budget remains | bounded retry/backoff | affected operation | none |
| `RATE_LIMITED` with trustworthy retry guidance | schedule/backoff | affected operation | none |
| expired-token-like failure and bounded recovery succeeds | fresh committed session + identity proof, continue | `AUTHENTICATION` | none |
| bounded auth recovery exhausted | `PAUSED` | `AUTHENTICATION` | `AUTH_RETRY_LIMIT` |
| suspected application re-auth before R0 detection contract | `REVIEW_REQUIRED` | `AUTHENTICATION` | none |
| positively detected application re-auth after accepted R0 mapping | `PAUSED` | `AUTHENTICATION` | `APPLICATION_REAUTH_REQUIRED` |
| positive missing product permission | write `BLOCKED`, `PAUSED` | `PRODUCT_REGISTRATION` | `SCOPE_INSUFFICIENT` |
| proven provider restriction | scoped `PAUSED` | proven scope | `ACCOUNT_RESTRICTED` |
| identity mismatch | `REVIEW_REQUIRED` | `AUTHENTICATION` | none |
| ambiguous `GW.AUTHN` | diagnose; unresolved -> `REVIEW_REQUIRED` | `AUTHENTICATION` or affected operation | none |
| validation/policy/conflict/duplicate without frozen PAUSED mapping | operation-specific handling; unresolved -> `REVIEW_REQUIRED` | affected operation | none |
| fatal/unknown condition | stop affected automation; `REVIEW_REQUIRED` unless narrower proven pause exists | affected operation | none |
| mutation outcome unknown | reconcile first; unresolved -> `REVIEW_REQUIRED` | `PRODUCT_REGISTRATION` | none |

---

## 12. Compact capability gates

`auth != READY -> write != READY`

`AUTH_MISMATCH -> REVIEW_REQUIRED/AUTHENTICATION`

`write_scope=MISSING -> write=BLOCKED + PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT`

`write_scope=UNKNOWN != SCOPE_INSUFFICIENT`

`write_scope=READY != write=READY`

`endpoint=NOT_ADOPTED -> network I/O forbidden`

`M2 product endpoints NOT_ADOPTED -> product write=UNVERIFIED`

`contract_freshness=UNRECORDED -> existing runtime truth may remain; expansion blocked; no expiry/contradiction claim`

`contract_freshness=STALE -> existing runtime truth may remain; expansion blocked`

`contract_freshness=REVIEW_REQUIRED -> disputed-dependent operations stop`

`UNRECORDED -> CURRENT` is the only valid transition out of bootstrap

`* -> UNRECORDED` is forbidden after bootstrap

same-scope `REVIEW_REQUIRED + proven PAUSED reason -> PAUSED/<reason>`

same-scope proven `PAUSED` is not erased by later ambiguity

`PAUSED -> frozen reason_code + frozen workflow_scope required`

`REVIEW_REQUIRED -> do not fabricate PAUSED reason`

`remote_outcome=UNKNOWN -> blind mutation replay forbidden`

`old credential/session evidence -> cannot establish current auth READY`

`old application-fingerprint/mapping evidence -> write_scope=UNKNOWN`

---

## 13. M2 expected capability projection

Healthy M2 may truthfully be:

`auth=READY / write_scope=READY(OPERATOR_ATTESTED) / write=UNVERIFIED`

or:

`auth=READY / write_scope=UNKNOWN / write=UNVERIFIED`

The second state is still a connected account.

---

## 14. UI presentation contract

The UI exposes authentication, registration permission, and actual registration separately.

§14.3, §14.5, §14.6 and §14.8–§14.11 were completed by the architect's decision on Issue #41 (comment 5667551746). §14.8 is the owning contract home of Issue #34. Every rule here is presentation of server-owned truth. The UI routes on enum values from the read API, never on localized labels or string matching (§2.6).

### 14.1 Visual evidence-strength markers

Permission strength MUST remain visible even if text truncates.

Frozen markers:

- `●` = machine/runtime-proven strong status where the owning contract permits that marker;
- `◐` = operator-attested/limited-strength positive evidence;
- `○` = unverified/unknown/not-yet-proven;
- blocking/review conditions use their normal warning/error treatment and MUST NOT masquerade as a positive marker.

A later design-system icon may replace these glyphs only if the semantic distinction remains machine-checkable and visually distinct.

### 14.2 Default M2 compact example

With operator-attested product permission:

```text
인증      ● 연결됨
등록 권한 ◐ 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

If a future official machine introspection mechanism verifies permission:

```text
등록 권한 ● 권한 확인됨 (자동 확인)
```

Thus UI does not flatten `OPERATOR_ATTESTED` and `MACHINE_VERIFIED` into the same visual trust level.

### 14.3 Authentication line

| Internal condition | UI status |
| --- | --- |
| `auth=READY` | `● 연결됨` |
| `auth=NOT_READY` with no AUTHENTICATION overlay | `○ 연결 확인 전`: non-success; never `연결됨` |
| `auth=NOT_BOUND` | `○ 연결 필요` |
| `auth=AUTH_MISMATCH` | `계정 확인 필요` |
| `PAUSED/AUTHENTICATION/APPLICATION_REAUTH_REQUIRED` | `재인증 필요` |
| `PAUSED/AUTHENTICATION/AUTH_RETRY_LIMIT` | `인증 확인 필요` |
| `PAUSED/AUTHENTICATION/ACCOUNT_RESTRICTED` | `계정 제한 확인 필요` (warning treatment) |
| auth `REVIEW_REQUIRED` | `확인 필요` |
| automatic recovery in progress | non-success `연결 확인 중`; never `연결됨` |

`auth=NOT_READY` without an overlay is a bound account whose current session holds no identity proof yet: after a restart, after a credential rotation, or after an operator resolved a review. It MUST never render as connected.

M2 has no automatic-recovery runtime path; retry budgets are policy-pending (ERRORS.md §25 Q6). An M2 UI therefore never shows `연결 확인 중`, and it MUST NOT invent a recovery state that no runtime path produces.

When several conditions hold, the authentication line shows the first match:

1. `auth=AUTH_MISMATCH`;
2. a typed `PAUSED/AUTHENTICATION` reason;
3. `REVIEW_REQUIRED/AUTHENTICATION`;
4. the raw `auth` axis (`READY`, `NOT_READY` or `NOT_BOUND`).

The PR-B domain invariants already reject `auth=READY` together with an open AUTHENTICATION overlay, so rules 1–3 never compete with `● 연결됨`.

### 14.4 Registration-permission line

| Internal condition | UI status |
| --- | --- |
| `write_scope=READY`, `OPERATOR_ATTESTED` | `◐ 권한 확인됨 (관리자 화면 확인)` |
| `write_scope=READY`, `MACHINE_VERIFIED` | `● 권한 확인됨 (자동 확인)` |
| `write_scope=MISSING` | `권한 부족` |
| `write_scope=UNKNOWN` | `○ 권한 미확인` |

A generic positive marker/text that hides evidence strength is forbidden.

### 14.5 Actual-registration line

| Internal condition | UI status |
| --- | --- |
| `write=UNVERIFIED` | `○ 미확인` |
| `write=READY` | `● 검증됨` |
| `write=BLOCKED` | `차단됨` |

The actual-registration line always projects the `write` axis itself. A `REVIEW_REQUIRED` or `PAUSED` overlay on `PRODUCT_REGISTRATION` never overwrites this label. The human action renders on its own `조치 필요` row (§14.9). This preserves the independent axes of §17 target 16 at the presentation layer.

During M2 product registration MUST show `○ 미확인`, never `검증됨`.

### 14.6 Freshness warning

Freshness is shown separately from capability truth, on its own row.

| `contract_freshness` | UI label | Treatment |
| --- | --- | --- |
| `UNRECORDED` | `API 계약 상태 미확인` | neutral |
| `STALE` | `API 계약 재검토 필요` | warning |
| `REVIEW_REQUIRED` | `API 계약 검토 필요` | error (the stronger review treatment) |
| `CURRENT` | `API 계약 확인됨 (운영자 기록 · YYYY.MM.DD)` | neutral |

- **`UNRECORDED`**: an unreviewed-contract state.
- **`STALE`**: a review-expired or outdated-contract state.
- **`REVIEW_REQUIRED`**: a contradiction or conflict review state.
- **`CURRENT`**: an operator-recorded reviewed state (F8). The date is `freshness_recorded_at`. The label MUST read as an operator recording and never as provider-measured evidence.

No freshness value uses an evidence-strength marker (`●`, `◐`, `○`): freshness is contract governance, not provider evidence (F1).

Freshness presentation MUST NOT overwrite a still-valid `인증: ● 연결됨` unless runtime auth proof itself becomes invalid.

### 14.7 PAUSED reason labels

| reason_code | Korean UI label |
| --- | --- |
| `AUTH_RETRY_LIMIT` | `인증 재시도 한도 초과` |
| `APPLICATION_REAUTH_REQUIRED` | `네이버 애플리케이션 재인증 필요` |
| `SCOPE_INSUFFICIENT` | `필수 API 권한 부족` |
| `ACCOUNT_RESTRICTED` | `계정 제한 확인 필요` |

Localized labels are presentation only; durable truth uses enum values.

### 14.8 Derived-value primacy (Issue #34)

When displaying a derived decision value, the value actually used by the current decision MUST be the primary displayed value. Inputs or historical/configuration values MAY be shown as secondary context, but MUST NOT replace the derived value in a way that makes the current result appear more permissive, more trusted, fresher, or otherwise more favorable than the decision actually is.

The rule is general, not A0-specific. It covers at least:

- the applicable A0 evidence age bound, `applicable_max_age_days = min(recorded bound, configured bound)` (PERMISSIONS_SCOPES §8.1): the effective bound is primary, and the recorded and configured bounds are secondary context;
- evidence strength (S1): a positive permission is never shown without its strength marker;
- the derived A0 `freshness_status`: the current evaluation is primary, never merely the stored observation time or one freshness input;
- each capability axis: every line projects its own server field (§14.3–§14.6), and no line borrows another line's value;
- any future derived capability or policy value with more than one input.

### 14.9 Human-action (`조치 필요`) rows

Each workflow overlay renders on its own `조치 필요` row, scoped by `workflow_scope`:

| Overlay | Row label |
| --- | --- |
| `REVIEW_REQUIRED/<scope>` | `<scope label> · 확인 필요` |
| `PAUSED/<scope>/<reason_code>` | `<scope label> · <§14.7 reason label>` |

- The scope labels are `AUTHENTICATION` → `인증` and `PRODUCT_REGISTRATION` → `상품 등록`.
- With no overlay, no `조치 필요` row is shown.
- An action row never replaces the authentication, registration-permission or actual-registration line. The authentication line applies its own precedence (§14.3).

### 14.10 Diagnostic context

`error_class` and `remote_outcome` are diagnostic context only:

- each is shown only while non-null, as its enum value; a Korean gloss MAY accompany the value;
- neither drives, softens or overwrites any other line;
- the UI never fabricates provider codes, trace ids or other evidence that the read API does not expose.

`remote_outcome` describes mutations. M2 has none, so an M2 UI normally never shows it.

### 14.11 M2 surfaces

The M2 projection appears on these surfaces only. All of them read the capability and attestation read APIs.

1. **설정 › 스마트스토어 › API 관리.** The three-layer projection replaces the prototype's demo status values. Its rows are:
   - 인증, 등록 권한, 실제 등록;
   - API 계약 (§14.6);
   - `조치 필요` (§14.9);
   - diagnostic context (§14.10).
2. **설정 › 공통 › 플랫폼 연동 현황.** The SmartStore row shows the authentication-line label derived from capability truth (§14.3). It never shows a hard-coded `NOT_CONNECTED` projection. Marketplaces without an adopted capability contract, such as Coupang and 11번가, stay unchanged until they adopt one.
3. **The A0 permission card (PR-C).** Its current-truth projection carries the §14.4 evidence-strength marker. PR-C's input semantics do not change.

Displaying any of these surfaces performs zero SmartStore network calls.

---

## 15. M2 CONNECT vs M5 REGISTER

M2 may establish `auth=READY` without product-write proof.

M2 may separately establish `write_scope=READY/MISSING/UNKNOWN` with provenance without probing a product endpoint.

M2 leaves product `write=UNVERIFIED` until M5 adopts the required mutation/read-back endpoints and performs the bounded real proof.

---

## 16. State-convergence examples

### Case 0 — brand-new capability state

```text
contract_freshness=UNRECORDED
auth=NOT_BOUND
write_scope=UNKNOWN
write=UNVERIFIED
```

No expiry or contradiction is claimed. Expansion/new trust remains blocked until an explicit reviewed freshness recording establishes `CURRENT`.

### Case 1 — healthy M2 + operator-attested permission

```text
auth=READY
write_scope=READY / evidence_strength=OPERATOR_ATTESTED
write=UNVERIFIED
contract_freshness=CURRENT
workflow overlay=none
```

UI:

```text
인증      ● 연결됨
등록 권한 ◐ 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

### Case 2 — connected, permission unknown

```text
auth=READY
write_scope=UNKNOWN
write=UNVERIFIED
```

UI remains connected and shows `○ 권한 미확인`.

### Case 3 — product permission positively missing

```text
auth=READY
write_scope=MISSING
write=BLOCKED
workflow_state=PAUSED
workflow_scope=PRODUCT_REGISTRATION
reason_code=SCOPE_INSUFFICIENT
```

Authentication remains connected.

### Case 3a — the missing-permission evidence expires

The Case 3 attestation crosses its age bound (`PERMISSIONS_SCOPES.md` §8.1) and the first read converges (S6, S7):

```text
auth=READY
write_scope=UNKNOWN
write=UNVERIFIED
SCOPE_INSUFFICIENT pause removed
```

The permission is not shown as granted: the A0 surface says the stored confirmation expired and the current permission state is unknown (`○`).

### Case 4 — identity mismatch

```text
auth=AUTH_MISMATCH
write!=READY
workflow_state=REVIEW_REQUIRED
workflow_scope=AUTHENTICATION
reason_code=null
```

### Case 5 — suspected 180-day application re-auth before R0 detection contract

```text
auth=NOT_READY
workflow_state=REVIEW_REQUIRED
workflow_scope=AUTHENTICATION
reason_code=null
```

`APPLICATION_REAUTH_REQUIRED` may be shown only as a hypothesis/help hint, not durable state.

### Case 6 — positively detected application re-auth after accepted R0 mapping

```text
auth=NOT_READY
workflow_state=PAUSED
workflow_scope=AUTHENTICATION
reason_code=APPLICATION_REAUTH_REQUIRED
```

After provider-UI re-authentication, mint/commit a fresh session and repeat identity proof before READY.

### Case 7 — review date passed, runtime auth still proven

```text
contract_freshness=STALE
auth=READY
write_scope=last independently valid value subject to its own evidence freshness
write=existing runtime value
```

Existing proven behavior continues; expansion is blocked until review.

### Case 8 — generic `GW.AUTHN`

```text
error_class=UNKNOWN or contextual class supported by evidence
bounded diagnostics
unresolved -> workflow_state=REVIEW_REQUIRED
reason_code=null
```

Do not guess token expiry, missing scope, or application re-authentication.

---

## 17. Implementation enforcement targets for Claude Code

Tests must cover at least:

1. `auth!=READY` cannot produce `write=READY`;
2. `AUTH_MISMATCH` -> `REVIEW_REQUIRED/AUTHENTICATION`, never auto-rebind;
3. `write_scope=MISSING` -> `write=BLOCKED + PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT`;
4. `write_scope=UNKNOWN` never becomes `SCOPE_INSUFFICIENT` without positive evidence;
5. `workflow_scope` accepts only the frozen enum and is never free text;
6. operator-attested permission uses `◐`/limited-strength semantic marker;
7. machine-verified permission uses distinct `●`/strong semantic marker when supported;
8. `NOT_ADOPTED` endpoint fails before network I/O;
9. M2 product write cannot become READY;
10. `STALE` preserves existing runtime proof and blocks expansion without extra safety judgment;
11. `REVIEW_REQUIRED` freshness stops operations depending on the disputed invariant;
12. `PAUSED` requires one frozen reason and one frozen scope;
13. generic `GW.AUTHN` cannot map directly to `SCOPE_INSUFFICIENT` or `APPLICATION_REAUTH_REQUIRED`;
14. before accepted `SMARTSTORE-R0-APP-REAUTH`, suspected app re-auth maps to REVIEW_REQUIRED, not PAUSED;
15. after accepted app-reauth detection contract, recovery requires provider re-auth + fresh session + fresh identity proof;
16. UI/API keeps auth, permission, write, evidence strength, workflow state/scope, and freshness as separate fields;
17. persisted READY loses to current evidence after restart;
18. `SMARTSTORE-A0-PERMISSION` handling performs zero SmartStore network calls and cannot promote operator attestation into R0/MACHINE_VERIFIED evidence.
19. a new capability state starts `contract_freshness=UNRECORDED`; the four-state behavior matrix and closed transition graph are enforced in code, same-value reviewed recordings update `freshness_recorded_at` and audit provenance, and PR-A cannot fabricate `CURRENT`;
20. same-scope workflow convergence promotes `REVIEW_REQUIRED` to a positively proven frozen `PAUSED` reason while later ambiguity cannot erase an existing proven `PAUSED` reason;
21. operator-attested permission evidence persists `attested_status` consistent with its required/observed groups and the freshness-policy bound in effect at recording, and never persists current `freshness_status`;
22. with the injected clock and the canonical 30-day bound, evidence is `FRESH` just before and exactly at the bound and `EXPIRED` just after it; both `READY` and `MISSING` attestations then converge to `write_scope=UNKNOWN`, and expired `MISSING` removes the evidence-dependent `SCOPE_INSUFFICIENT` pause without promoting `write`;
23. time-driven expiry converges through the existing read path: the first read after the bound produces exactly one durable capability change and one audit event, repeated reads produce none, and the attestation record is never mutated.

Business/state decisions belong server/domain-side, not duplicated in frontend JavaScript.

---

## 18. Runtime and attested evidence required before `verified_at`

Documentation completion does not verify this contract.

Required provider-measured R0 slots from `SOURCES.md` are:

- `SMARTSTORE-R0-TOKEN`;
- `SMARTSTORE-R0-SELLER-ACCOUNT`;
- `SMARTSTORE-R0-FIRST-TOKEN-CRASH`;
- `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`;
- `SMARTSTORE-R0-APP-REAUTH`.

Required operator-attested A0 handling slot is:

- `SMARTSTORE-A0-PERMISSION`.

`SMARTSTORE-A0-PERMISSION` proves only that ICBM correctly stores, projects, invalidates, and limits operator-attested permission evidence. It does not establish NAVER/provider permission truth, machine introspection, or write readiness.

`SMARTSTORE-R0-APP-REAUTH` must establish the trustworthy machine-observable condition, or an explicit provider-supported equivalent, that permits durable `APPLICATION_REAUTH_REQUIRED` convergence. Until then the reason remains schema-reserved but automatic detection is disabled.

Local/runtime acceptance must also demonstrate:

- auth READY happy path;
- restart convergence;
- controlled AUTH_MISMATCH fail-closed behavior;
- bounded auth recovery and `AUTH_RETRY_LIMIT`;
- permission READY/MISSING/UNKNOWN with provenance;
- operator-attested permission storage/projection/invalidation with zero network calls;
- operator-attested evidence expiry at the 30-day bound for both READY and MISSING, including `SCOPE_INSUFFICIENT` release and idempotent convergence audit;
- typed workflow_scope routing;
- M2 write UNVERIFIED enforcement;
- NOT_ADOPTED no-network invariant;
- `UNRECORDED` bootstrap/non-reentrancy and freshness transition enforcement;
- same-value freshness re-recording provenance including `freshness_recorded_at` and actor audit;
- STALE expansion gate and REVIEW_REQUIRED contradiction gate;
- same-scope REVIEW_REQUIRED -> proven PAUSED convergence without ambiguity erasing a proven PAUSED reason;
- visually distinct operator-attested vs machine-verified permission presentation;
- no secret/bearer leakage.

One completed R0 or A0 slot does not automatically verify this document or another owning contract.

---

## 19. Final M2 capability contract

`auth READY = current committed session + current matching account identity proof`

`write_scope = provider API-group evidence + provenance/strength`

`write READY = real operation proof, not permission declaration`

`M2 product write = UNVERIFIED`

`endpoint NOT_ADOPTED = no network I/O`

`workflow_scope in {AUTHENTICATION, PRODUCT_REGISTRATION}`

`contract_freshness UNRECORDED = bootstrap only; no currency/expiry/contradiction claim; keep proven behavior; block expansion`

`contract_freshness STALE = prior determination no longer current; keep existing proven behavior; block expansion`

`contract_freshness REVIEW_REQUIRED = material contradiction/conflict; stop behavior depending on disputed invariant`

`UNRECORDED -> CURRENT only; no transition returns to UNRECORDED`

`freshness_recorded_at + actor audit provenance = required for every reviewed freshness recording, including same-value re-recording`

`PAUSED = known scoped human-remediable blocker + frozen reason_code + frozen workflow_scope`

`REVIEW_REQUIRED = safe next action cannot be determined automatically`

`APPLICATION_REAUTH_REQUIRED automatic mapping disabled until SMARTSTORE-R0-APP-REAUTH is accepted`

`SMARTSTORE-A0-PERMISSION = operator-attested permission handling evidence, never provider runtime truth`

`expired or invalidated READY/MISSING permission evidence -> write_scope UNKNOWN; evidence-dependent SCOPE_INSUFFICIENT released; write never promoted`

`time-driven convergence = existing read path + injected Clock; only durable changes are audited, exactly once`

`error_class != workflow_state != reason_code`

`AUTH_MISMATCH -> REVIEW_REQUIRED/AUTHENTICATION`

`positive missing scope -> PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT`

`bounded auth recovery exhausted -> PAUSED/AUTHENTICATION/AUTH_RETRY_LIMIT`

The UI preserves the same separation and evidence strength:

```text
인증      <auth truth>
등록 권한 <write_scope truth + evidence strength>
실제 등록 <write truth>
```

No layer may be made greener by borrowing proof from another layer.
