# SmartStore Capability Mapping and State-Convergence Contract

## Status

| Field | Value |
| --- | --- |
| Provider | NAVER SmartStore / Commerce API |
| Contract status | `M2_CAPABILITY_CONVERGENCE_FROZEN_FOR_REVIEW` |
| Integration mode | `OWN_STORE_SELF` |
| Capability layers | `auth -> write_scope -> write` |
| Contract freshness | independent axis |
| Human-action workflow | `PAUSED` / `REVIEW_REQUIRED` are scoped overlays, not provider error classes |
| M2 product write | `UNVERIFIED` — product endpoints remain `NOT_ADOPTED` |
| Upstream version | `2.88.0` |
| Retrieved at | `2026-09-14` |
| Verified at | `null` |
| Review due | `2026-10-14` |

## Provenance

This document is a convergence contract over the already-reviewed SmartStore contracts. It does not replace them.

Owning contracts:

- `ACCOUNT_IDENTITY.md`
- `AUTH.md`
- `PERMISSIONS_SCOPES.md`
- `ERRORS.md`
- `ENDPOINT_MATRIX.md`
- `SOURCES.md`

Stable source IDs are reused from `SOURCES.md` rather than inventing another taxonomy.

Primary provider/protocol source IDs relevant to this convergence contract include:

- `NAVER-P0-CURRENT`
- `NAVER-P0-AUTH`
- `NAVER-P0-RESTRICTION`
- `NAVER-P0-TROUBLESHOOTING`
- `NAVER-P0-TOKEN`
- `NAVER-P0-SELLER-ACCOUNT`
- `OAUTH-S0-RFC6749`

Supporting provider source IDs include the claim-specific P1 evidence recorded in `SOURCES.md`, especially:

- `NAVER-P1-ACCOUNT-UID-2425`
- `NAVER-P1-APP-REAUTH-3557`
- `NAVER-P1-GW-AUTHN-GROUP-1013`
- `NAVER-P1-PRODUCT-GROUP-1835`
- `NAVER-P1-SELLERINFO-GROUP-1895`

Runtime evidence required before this document may set `verified_at` is defined in §18.

Core provenance rule:

`capability state is derived from current evidence, not from a persisted badge or documentation statement alone`

---

## 1. Purpose

This document is the SmartStore M2 convergence point for:

- account identity;
- authentication lifecycle;
- provider-declared permission evidence;
- actual write capability;
- endpoint adoption;
- error classification;
- contract freshness;
- human-action workflow state;
- UI presentation.

It answers one question:

> Given all current SmartStore evidence, what may ICBM truthfully claim, what may it execute automatically, what must it block, and what must the user see?

This document deliberately does **not** enumerate every Cartesian product of all states.

Instead it defines independent axes plus **invariants and forbidden combinations**.

That keeps the contract extensible without creating hundreds of combination rows.

---

## 2. Independent axes

The following concepts MUST remain independent.

### 2.1 `auth`

`auth` answers:

> Does the current committed authentication/session generation prove the intended SmartStore account?

SmartStore M2 uses these meaningful states:

- `READY`
- `NOT_READY`
- `AUTH_MISMATCH`
- `NOT_BOUND`

`READY` is established only by the full `ACCOUNT_IDENTITY.md` + `AUTH.md` proof chain.

A stored `READY` string is never sufficient by itself.

### 2.2 `write_scope`

`write_scope.status` remains:

- `READY`
- `MISSING`
- `UNKNOWN`

For SmartStore this is **provider API-group permission evidence**, not an OAuth scope string.

The status is incomplete without its evidence envelope, including:

- `evidence_source`;
- `evidence_strength`;
- `observed_at`;
- `application_fingerprint`;
- `required_groups`;
- `observed_groups`;
- `endpoint_mapping_revision`;
- `freshness_status`.

### 2.3 `write`

`write.status` remains:

- `UNVERIFIED`
- `READY`
- `BLOCKED`

`READY` means the intended write capability has been proven through the operation-specific mutation and required external read-back/reconciliation contract.

For product registration, that proof belongs to M5, not M2.

### 2.4 `contract_freshness`

`contract_freshness` is the freshness of ICBM's adopted understanding of the provider contract.

Values:

- `CURRENT`
- `STALE`
- `REVIEW_REQUIRED`

It is not the same as runtime capability truth.

Therefore this is valid:

`contract_freshness=STALE + auth=READY`

when runtime auth proof is still valid and no disputed contract change invalidates that proof.

### 2.5 Human-action workflow overlay

`workflow_state` is an **action overlay scoped to the affected capability/operation**.

This document freezes two human-action states:

- `PAUSED`
- `REVIEW_REQUIRED`

It does not replace global durable Job states from the canonical architecture.

It also MUST NOT be collapsed into one global account state that hides independently valid capabilities.

For example, product registration can be paused for missing scope while authentication remains READY.

---

## 3. Fundamental separation rules

The following equivalences are forbidden:

`auth READY == write_scope READY`

`write_scope READY == write READY`

`write READY == contract_freshness CURRENT`

`error_class == workflow_state`

`provider error code == reason_code`

`persisted READY == measured READY`

A state on one axis MAY constrain another axis through an explicit invariant, but it MUST NOT silently overwrite the other axis's evidence.

---

## 4. Authentication invariants

### A1. Current proof required

`auth=READY` requires all current authentication invariants, including:

- committed credential generation;
- committed session generation;
- successful adopted protected account read;
- non-empty primary identity `accountUid`;
- observed identity equal to expected `provider_account_uid`;
- identity proof bound to current credential/session generations.

### A2. Authentication not READY blocks write READY

If:

`auth != READY`

then:

`write != READY`

A marketplace mutation capability cannot remain READY while the current authenticated account is unproven.

### A3. Identity mismatch

If:

`observed.accountUid != expected.provider_account_uid`

then:

- `auth=AUTH_MISMATCH`;
- `write!=READY`;
- affected automation stops;
- `workflow_state=REVIEW_REQUIRED`;
- no `PAUSED reason_code` is fabricated;
- no automatic rebinding occurs.

`AUTH_MISMATCH` is a proven identity conflict but the root operational cause may still be ambiguous.

### A4. First binding incomplete

If binding commit is not proven:

`auth=NOT_BOUND`

An incomplete binding is a setup/recovery state, not evidence that credentials are invalid.

A fresh authenticated identity read is required before completing the binding.

### A5. Authentication retry exhaustion does not change cause

Retry-budget exhaustion MUST NOT reclassify `error_class` merely because many attempts failed.

When bounded auth recovery is exhausted:

- `auth!=READY`;
- `workflow_state=PAUSED` for the auth capability;
- `reason_code=AUTH_RETRY_LIMIT`.

The measured `error_class` remains whatever the evidence supports.

---

## 5. Permission (`write_scope`) invariants

### S1. Provenance is part of the permission state

A bare `write_scope=READY` presentation is forbidden when it hides evidence strength.

At minimum UI/audit logic must be able to distinguish:

- `OPERATOR_ATTESTED`;
- `MACHINE_VERIFIED` when a future official introspection mechanism exists.

### S2. Positive missing evidence blocks the affected write

If:

`write_scope.status=MISSING`

for a permission set required by an operation, then:

- the affected `write.status=BLOCKED`;
- the affected workflow is `PAUSED`;
- `reason_code=SCOPE_INSUFFICIENT`;
- unrelated capabilities such as a still-valid account identity read MAY remain READY.

### S3. Unknown permission is not missing permission

If:

`write_scope.status=UNKNOWN`

then ICBM MUST NOT claim `SCOPE_INSUFFICIENT`.

`UNKNOWN` means insufficient permission evidence.

It may block a future operation whose policy requires declared permission proof, but it is not evidence that the provider permission is absent.

### S4. `write_scope=READY` does not prove write

Even when every required API group is positively observed:

`write_scope.status=READY != write.status=READY`

Real mutation capability is proven separately.

### S5. A successful write does not backfill declared scope

The following combination is valid in principle after M5:

`auth=READY`

`write_scope.status=UNKNOWN`

`write.status=READY`

if the real bounded write/read-back proof succeeded but provider-declared permission provenance is unavailable.

ICBM MUST preserve both facts instead of manufacturing declared-scope evidence from write success.

---

## 6. Write-capability invariants

### W1. M2 product write is not verified

During M2:

`product_registration.write.status=UNVERIFIED`

because product mutation/read-back endpoints remain `NOT_ADOPTED` in `ENDPOINT_MATRIX.md`.

No UI, database state, or operator action may promote M2 product write to READY.

### W2. `write=READY` requires operation proof

For product registration, READY requires at minimum the later M5 contract:

- endpoint is `ADOPTED`;
- auth is READY;
- operation-specific preconditions pass;
- bounded real CREATE is executed under explicit approval/execution policy;
- remote result is reconciled/read back;
- expected marketplace identity/state matches;
- evidence is retained.

CREATE response success alone is insufficient.

### W3. `remote_outcome=UNKNOWN` forbids blind replay

For a mutation with unknown remote outcome:

- `write` MUST NOT be promoted based on the ambiguous attempt;
- generic automatic replay is forbidden;
- reconciliation/read-back runs first;
- unresolved ambiguity becomes `workflow_state=REVIEW_REQUIRED`.

This rule does not create a PAUSED reason code.

---

## 7. Endpoint-adoption invariants

### E1. Adoption is an execution allow-list

`ENDPOINT_MATRIX.md` is authoritative for whether an endpoint may be executed.

If:

`endpoint.adoption=NOT_ADOPTED`

then:

`network I/O = forbidden`

regardless of `auth`, `write_scope`, or any optimistic local configuration.

### E2. Capability evidence cannot adopt an endpoint

The following do not change endpoint adoption:

- operator-attested `상품` permission;
- a historical successful product call;
- a configured feature flag;
- a desired capability;
- an error response from an unadopted probe.

Endpoint adoption requires reviewed contract change.

### E3. M2 adopted set

M2 executes exactly the adopted CONNECT endpoints:

- `SMARTSTORE_AUTH_TOKEN`
- `SMARTSTORE_SELLER_ACCOUNT`

The product/category/image endpoints listed for M5 remain planning metadata only.

---

## 8. Contract-freshness invariants

### F1. Freshness is not runtime capability truth

A calendar review date or provider-documentation age does not mechanically invalidate a still-current runtime proof.

Therefore:

`contract_freshness=STALE`

does not by itself imply:

`auth!=READY`

or:

`write!=READY`

### F2. STALE blocks expansion decisions

While an affected contract is STALE, ICBM MUST NOT make a new expansion decision that depends on the stale understanding, including:

- adopting a new endpoint;
- widening the endpoint allow-list;
- promoting a previously unverified capability to READY;
- changing retry/replay semantics;
- changing required permission mappings;
- changing provider request/response assumptions;
- expanding supported application/account mode.

Existing runtime-proven behavior MAY continue only when no disputed/stale safety invariant makes that behavior unsafe.

### F3. REVIEW_REQUIRED freshness is stronger than ordinary staleness

`contract_freshness=REVIEW_REQUIRED` is used when there is a material contradiction, incompatible upstream change, or evidence conflict affecting the contract.

Affected automatic operations that depend on the disputed invariant MUST stop until review resolves the contract.

This is not the same as merely reaching `review_due`.

### F4. Freshness never invents provider truth

A stale source cannot be converted to favorable provider guarantees merely to preserve capability.

If a runtime capability itself can no longer be proven under the owning runtime contract, that capability converges independently away from READY.

---

## 9. `PAUSED` reason-code contract — M2 frozen set

`PAUSED` means:

> the affected capability/operation has a sufficiently understood blocking condition with a known class of remediation, so automatic work stops without pretending the entire account is invalid.

For SmartStore M2, the `PAUSED reason_code` set is **closed to exactly these four values**:

| reason_code | Evidence requirement | Scope | Expected remediation |
| --- | --- | --- | --- |
| `AUTH_RETRY_LIMIT` | bounded auth recovery exhausted; cause class itself is retained separately | authentication | inspect auth evidence/trace; correct proven issue before retry |
| `APPLICATION_REAUTH_REQUIRED` | provider behavior/evidence positively establishes own-store application re-authentication is required | authentication/application authorization | integrated manager completes NAVER provider-UI re-authentication, then fresh token/session/account proof |
| `SCOPE_INSUFFICIENT` | positive current evidence identifies one or more required API groups as absent | affected capability, e.g. product registration | enable required API group(s), obtain fresh permission evidence/session as required |
| `ACCOUNT_RESTRICTED` | provider evidence positively establishes an account/store restriction relevant to the operation | affected account/operation according to provider evidence | resolve provider restriction, then re-prove affected capability |

### 9.1 Reason-code rules

- `reason_code` MUST be non-null when `workflow_state=PAUSED`.
- `reason_code` MUST be one of the four frozen values above during M2.
- a new PAUSED reason requires architecture/contract review; adapters MUST NOT invent provider-specific reason strings as durable state;
- `reason_code` is not a replacement for `error_class`, provider error code, or evidence details;
- generic `GW.AUTHN`, `403`, timeout, malformed response, or retry exhaustion outside the defined auth case MUST NOT be guessed into one of these reasons.

### 9.2 Why the error classes do not become reason codes

The canonical error classes remain:

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

They answer **what kind of failure evidence exists**.

PAUSED reason codes answer **which proven human-remediable operational condition currently blocks this scoped capability**.

Therefore there is intentionally no one-to-one mapping.

---

## 10. `REVIEW_REQUIRED` contract

`workflow_state=REVIEW_REQUIRED` means:

> automation cannot safely determine or execute the next action without human judgment or additional evidence.

This is the correct destination when the blocker is real but a safe PAUSED reason cannot yet be proven.

Examples:

- `AUTH_MISMATCH`;
- unresolved generic `GW.AUTHN` after safe diagnostics;
- provider behavior contradicts current permission evidence;
- local credential/security-store failure whose exact remediation is not yet safely classified;
- application inactive/disabled behavior without a frozen provider mapping;
- schema/contract drift affecting a safety invariant;
- mutation `remote_outcome=UNKNOWN` that reconciliation cannot resolve;
- unknown/fatal/conflict conditions requiring diagnosis.

`workflow_state=REVIEW_REQUIRED` MUST NOT be given a fake PAUSED reason merely to make the UI more specific.

The underlying `error_class`, provider code, trace ID, and evidence remain independently visible.

### 10.1 `error_class=REVIEW_REQUIRED` remains distinct

As defined in `ERRORS.md`:

- `error_class=REVIEW_REQUIRED` is a cause-axis classification when provider/domain semantics themselves positively require human judgment;
- `workflow_state=REVIEW_REQUIRED` is an action state.

These are independent.

A normal valid combination is:

`error_class=UNKNOWN + workflow_state=REVIEW_REQUIRED`

---

## 11. Error-class to workflow behavior

This table defines default **action behavior**, not automatic root-cause rewriting.

| Evidence/class situation | Default behavior | PAUSED reason? |
| --- | --- | --- |
| transient network/provider failure, retry budget remains | bounded retry/backoff under owning operation contract | none |
| `RATE_LIMIT` with trustworthy retry guidance/budget | schedule/backoff; do not create human pause merely because delayed | none |
| expired-token-like auth failure and bounded recovery succeeds | commit new session, fresh identity proof, continue | none |
| bounded auth recovery exhausted | `PAUSED` for auth | `AUTH_RETRY_LIMIT` |
| provider positively requires 180-day application re-authentication | `PAUSED` for auth | `APPLICATION_REAUTH_REQUIRED` |
| positive current permission evidence shows required group missing | affected write `BLOCKED`; `PAUSED` | `SCOPE_INSUFFICIENT` |
| provider positively establishes account restriction | scoped `PAUSED` | `ACCOUNT_RESTRICTED` |
| account identity mismatch | `REVIEW_REQUIRED` | none |
| generic/ambiguous `GW.AUTHN` | diagnose auth/endpoint/scope evidence; unresolved -> `REVIEW_REQUIRED` | none |
| `VALIDATION` / `POLICY` / `CONFLICT` / `DUPLICATE` without a frozen PAUSED mapping | operation-specific handling; unresolved/human decision -> `REVIEW_REQUIRED` | none |
| `FATAL` local/provider contract defect | stop affected automation; `REVIEW_REQUIRED` unless a narrower proven PAUSED condition applies | none by default |
| `UNKNOWN` | preserve uncertainty; safe diagnostics/reconciliation; unresolved -> `REVIEW_REQUIRED` | none |
| write `remote_outcome=UNKNOWN` | reconcile before any replay; unresolved -> `REVIEW_REQUIRED` | none |

Retry-budget exhaustion outside the specifically frozen `AUTH_RETRY_LIMIT` case does **not** itself manufacture another PAUSED reason.

---

## 12. Capability gates and forbidden combinations

The following compact rules replace a full combination matrix.

### G1

`auth != READY -> write != READY`

### G2

`AUTH_MISMATCH -> workflow_state=REVIEW_REQUIRED`

### G3

`write_scope=MISSING -> affected write=BLOCKED + PAUSED/SCOPE_INSUFFICIENT`

### G4

`write_scope=UNKNOWN != SCOPE_INSUFFICIENT`

### G5

`write_scope=READY != write=READY`

### G6

`endpoint=NOT_ADOPTED -> network I/O forbidden`

### G7

`M2 product endpoint NOT_ADOPTED -> product write=UNVERIFIED`

### G8

`contract_freshness=STALE -> existing runtime truth may remain, expansion decisions blocked`

### G9

`contract_freshness=REVIEW_REQUIRED -> affected operations depending on disputed invariant stop`

### G10

`workflow_state=PAUSED -> reason_code required and frozen`

### G11

`workflow_state=REVIEW_REQUIRED -> do not fabricate PAUSED reason_code`

### G12

`error_class does not directly determine replay permission`

### G13

`remote_outcome=UNKNOWN -> blind mutation replay forbidden`

### G14

`evidence from old credential/session generation -> cannot establish current auth READY`

### G15

`permission evidence for old application fingerprint/mapping revision -> write_scope=UNKNOWN`

---

## 13. M2 SmartStore capability projection

At the end of a healthy M2 CONNECT implementation, the expected capability picture is conceptually:

```text
auth
  READY
  proven by committed token session + current /seller/account identity match

write_scope
  READY | MISSING | UNKNOWN
  always accompanied by evidence_source/evidence_strength

write
  UNVERIFIED
  because product mutation/read-back endpoints are NOT_ADOPTED until M5

contract_freshness
  CURRENT normally
  independent from the three capability layers
```

Therefore this is a valid M2 state:

`auth=READY / write_scope=READY(OPERATOR_ATTESTED) / write=UNVERIFIED`

and so is:

`auth=READY / write_scope=UNKNOWN / write=UNVERIFIED`

The second state means the marketplace connection is proven while declared product-write permission remains unproven.

It MUST NOT be shown as a disconnected account.

---

## 14. UI presentation contract

The SmartStore connection/capability UI SHALL expose the three capability layers separately.

The default compact presentation is:

```text
인증      ● 연결됨
등록 권한 ● 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

These are status values, not explanatory subtitles.

Detailed evidence/policy explanation belongs in the existing `ⓘ` tooltip/help pattern.

### 14.1 Authentication line

| Internal condition | UI status |
| --- | --- |
| `auth=READY` | `연결됨` |
| `auth=NOT_BOUND` | `연결 필요` |
| `auth=AUTH_MISMATCH` | `계정 확인 필요` |
| auth `PAUSED / APPLICATION_REAUTH_REQUIRED` | `재인증 필요` |
| auth `PAUSED / AUTH_RETRY_LIMIT` | `인증 확인 필요` |
| auth unresolved `REVIEW_REQUIRED` | `확인 필요` |
| other non-READY while automatic recovery is in progress | `연결 확인 중` or equivalent non-success status; MUST NOT display `연결됨` |

`AUTH_MISMATCH` review details SHOULD expose expected vs observed strong identity values and weak corroborating fields as review context only.

### 14.2 Registration-permission line

| Internal condition | UI status |
| --- | --- |
| `write_scope=READY`, `evidence_strength=OPERATOR_ATTESTED` | `권한 확인됨 (관리자 화면 확인)` |
| `write_scope=READY`, `evidence_strength=MACHINE_VERIFIED` | `권한 확인됨 (자동 확인)` |
| `write_scope=MISSING` | `권한 부족` |
| `write_scope=UNKNOWN` | `권한 미확인` |

A generic `권한 확인됨` that hides evidence strength is forbidden when the distinction matters to audit/trust.

### 14.3 Actual-registration line

| Internal condition | UI status |
| --- | --- |
| `write=UNVERIFIED` | `미확인` |
| `write=READY` | `검증됨` |
| `write=BLOCKED` | `차단됨` |
| affected workflow `REVIEW_REQUIRED` | `확인 필요` where that action state is more important than the underlying write label |

During M2, product registration MUST display `미확인`, never `검증됨`.

### 14.4 Contract freshness UI

`contract_freshness` is not a fourth capability badge that turns the account disconnected.

When STALE or REVIEW_REQUIRED, surface a separate warning such as:

- `API 계약 재검토 필요`

The warning MUST NOT overwrite a still-valid `인증: 연결됨` status unless the owning runtime auth invariant itself is no longer provable.

### 14.5 PAUSED reason presentation

Human-readable UI labels may be localized, but the durable reason code remains stable.

Recommended labels:

| reason_code | Korean UI label |
| --- | --- |
| `AUTH_RETRY_LIMIT` | `인증 재시도 한도 초과` |
| `APPLICATION_REAUTH_REQUIRED` | `네이버 애플리케이션 재인증 필요` |
| `SCOPE_INSUFFICIENT` | `필수 API 권한 부족` |
| `ACCOUNT_RESTRICTED` | `계정 제한 확인 필요` |

UI labels MUST NOT become persisted business truth in place of reason codes.

---

## 15. M2 CONNECT vs future registration readiness

M2 CONNECT and M5 REGISTER are deliberately different milestones.

### M2 connection truth

A SmartStore account may truthfully show:

`인증: 연결됨`

once the auth proof contract is satisfied.

This does not require product-write proof.

### Registration permission truth

M2 may separately record provider-declared product permission evidence:

`write_scope=READY | MISSING | UNKNOWN`

with provenance.

This still does not require or authorize a product mutation probe.

### Actual registration truth

M2 leaves:

`write=UNVERIFIED`

until M5 adopts the required mutation/read-back endpoints and performs the bounded real proof.

This separation prevents the M2 CONNECT implementation from probing a product API merely to make the UI look greener.

---

## 16. State convergence examples

These examples illustrate the invariants without becoming an exhaustive combination table.

### Case 1 — healthy M2 connection, operator-attested product permission

```text
auth=READY
write_scope=READY / evidence_strength=OPERATOR_ATTESTED
write=UNVERIFIED
contract_freshness=CURRENT
workflow human-action overlay = none
```

UI:

```text
인증      ● 연결됨
등록 권한 ● 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

### Case 2 — healthy connection, no trustworthy permission evidence

```text
auth=READY
write_scope=UNKNOWN
write=UNVERIFIED
```

UI keeps the account connected and shows `등록 권한: 권한 미확인`.

### Case 3 — product permission positively missing

```text
auth=READY
write_scope=MISSING
write=BLOCKED
workflow_state=PAUSED (scope=product registration)
reason_code=SCOPE_INSUFFICIENT
```

Authentication remains connected.

### Case 4 — identity mismatch

```text
auth=AUTH_MISMATCH
write!=READY
workflow_state=REVIEW_REQUIRED
reason_code=null
```

No auto-rebind.

### Case 5 — 180-day application re-authentication required

```text
auth=NOT_READY
workflow_state=PAUSED (scope=authentication)
reason_code=APPLICATION_REAUTH_REQUIRED
```

After provider-UI re-authentication, ICBM must mint/commit a fresh session and repeat account identity proof before READY.

### Case 6 — contract review date passed, runtime auth still proven

```text
contract_freshness=STALE
auth=READY
write_scope=last independently valid value subject to its own evidence freshness
write=existing runtime value
```

Existing safe runtime behavior may continue, but no new endpoint/capability expansion is allowed until contract review.

### Case 7 — generic GW.AUTHN with ambiguous cause

```text
error_class=UNKNOWN or contextual class supported by evidence
write_scope remains previous evidence-backed state unless its own evidence becomes stale/contradicted
bounded diagnostics run
unresolved -> workflow_state=REVIEW_REQUIRED
reason_code=null
```

Do not guess token expiry or missing scope.

---

## 17. Implementation enforcement targets for Claude Code

This document is architecture/contract only. Runtime implementation belongs to Claude Code.

M2 implementation SHALL make the mapping enforceable rather than leaving it as UI convention.

Repository/runtime tests should cover at least:

1. `auth!=READY` cannot produce `write=READY`;
2. `AUTH_MISMATCH` produces `REVIEW_REQUIRED`, never silent rebinding;
3. `write_scope=MISSING` produces affected `write=BLOCKED` and `PAUSED/SCOPE_INSUFFICIENT`;
4. `write_scope=UNKNOWN` never becomes `SCOPE_INSUFFICIENT` without positive evidence;
5. operator-attested scope READY surfaces `OPERATOR_ATTESTED` in API/UI read model;
6. `NOT_ADOPTED` endpoint resolution fails before network I/O;
7. M2 product write cannot become READY;
8. `contract_freshness=STALE` does not mechanically demote a valid runtime auth proof;
9. STALE blocks endpoint/capability expansion decisions;
10. `PAUSED` requires one of the four frozen reason codes;
11. `REVIEW_REQUIRED` does not require or fabricate a PAUSED reason code;
12. generic `GW.AUTHN` cannot directly map to `SCOPE_INSUFFICIENT`;
13. `APPLICATION_REAUTH_REQUIRED` recovery requires fresh session + account proof;
14. UI/API status projection keeps authentication, permission, and actual write as separate fields;
15. persisted READY state loses to current evidence after restart.

The tests MUST validate server/domain-owned state projection rather than duplicating business decisions in frontend JavaScript.

---

## 18. Runtime verification and `verified_at`

Documentation completion does not verify this convergence contract.

`verified_at` remains `null` until M2 implementation and acceptance evidence demonstrate the relevant state transitions against the current contract.

Required runtime/evidence slots from `SOURCES.md` are:

- `SMARTSTORE-R0-TOKEN`;
- `SMARTSTORE-R0-SELLER-ACCOUNT`;
- `SMARTSTORE-R0-FIRST-TOKEN-CRASH`;
- `SMARTSTORE-R0-TOKEN-REISSUE-WINDOW`;
- `SMARTSTORE-R0-PERMISSION`.

In addition, local/runtime acceptance MUST demonstrate at least:

- auth READY happy path;
- restart convergence;
- AUTH_MISMATCH fail-closed behavior using controlled evidence/harness where real mismatch testing is unsafe;
- bounded auth recovery and `AUTH_RETRY_LIMIT`;
- permission `READY/MISSING/UNKNOWN` projection with provenance;
- M2 `write=UNVERIFIED` enforcement;
- `NOT_ADOPTED` no-network invariant;
- contract-freshness expansion gate;
- UI/API three-line capability projection;
- no secret/bearer leakage in evidence.

One evidence slot completing does not automatically verify this document or any other contract.

Each owning contract's own acceptance requirements remain authoritative for its `verified_at`.

---

## 19. Final M2 capability contract

For SmartStore M2:

`auth READY = current committed session + current matching account identity proof`

`write_scope = provider API-group evidence + provenance/strength`

`write READY = real operation proof, not permission declaration`

`M2 product write = UNVERIFIED`

`endpoint NOT_ADOPTED = no network I/O`

`contract_freshness STALE != runtime capability automatically invalid`

`contract_freshness STALE = no new expansion decision`

`PAUSED = known scoped human-remediable blocker + frozen reason_code`

`REVIEW_REQUIRED = safe next action cannot be determined automatically`

`error_class != workflow_state != reason_code`

`AUTH_MISMATCH -> REVIEW_REQUIRED`

`positive missing scope -> PAUSED / SCOPE_INSUFFICIENT`

`180-day application re-auth required -> PAUSED / APPLICATION_REAUTH_REQUIRED`

`bounded auth recovery exhausted -> PAUSED / AUTH_RETRY_LIMIT`

`positive provider account restriction -> PAUSED / ACCOUNT_RESTRICTED`

The UI must expose the same separation:

```text
인증      <auth truth>
등록 권한 <write_scope truth + evidence strength>
실제 등록 <write truth>
```

No layer may be made greener by borrowing proof from another layer.
