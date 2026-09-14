# ICBM M2 SmartStore CONNECT — Claude Code Implementation Instructions

- Status: **IMPLEMENTATION INSTRUCTION / DESIGN FROZEN**
- Milestone: M2 SmartStore CONNECT
- Runtime implementation owner: Claude Code
- Architecture / contract owner: ChatGPT + independent cross-audit
- Acceptance authority: `docs/acceptance/M2.md` plus the owning SmartStore contracts

M2 implements **CONNECT only**.

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
   ^
   M2
```

M2 MUST NOT implement or probe SmartStore product registration, update, delete, category, image, or other non-adopted APIs.

---

## 1. Canonical contract bundle and read order

Implementation MUST follow the frozen contract set directly instead of re-summarizing or weakening it.

For PR-B, read in this order:

1. `docs/implementation/M2_SMARTSTORE_CONNECT_IMPLEMENTATION_INSTRUCTIONS.md`
2. `docs/platforms/smartstore/CAPABILITY_MAPPING.md` — **direct PR-B implementation contract**
3. `docs/platforms/smartstore/README.md`
4. only as needed for owning semantics: `ERRORS.md`, `AUTH.md`, `PERMISSIONS_SCOPES.md`, `ACCOUNT_IDENTITY.md`, `ENDPOINT_MATRIX.md`, `SOURCES.md`, `docs/acceptance/M2.md`

The full frozen SmartStore bundle remains:

- `docs/platforms/smartstore/ACCOUNT_IDENTITY.md`
- `docs/platforms/smartstore/AUTH.md`
- `docs/platforms/smartstore/PERMISSIONS_SCOPES.md`
- `docs/platforms/smartstore/ERRORS.md`
- `docs/platforms/smartstore/ENDPOINT_MATRIX.md`
- `docs/platforms/smartstore/SOURCES.md`
- `docs/platforms/smartstore/CAPABILITY_MAPPING.md`
- `docs/platforms/smartstore/README.md`
- `docs/acceptance/M2.md`

If implementation convenience conflicts with these contracts, the implementation changes — not the contract semantics.

---

## 2. Scope discipline: four implementation PRs

M2 MUST NOT repeat the large M1 single-PR pattern.

```text
PR-B  capability state machine + workflow overlay
PR-C  A0 permission attestation input + storage + invalidation
PR-A  SmartStore adapter + endpoint registry
PR-D  SmartStore CONNECT three-layer UI projection
```

Frozen implementation order:

```text
PR-B → PR-C → PR-A → PR-D
```

- PR-B defines the domain truth model used by C and A.
- PR-C implements A0 entirely with zero provider traffic.
- PR-A is the first unit that introduces real SmartStore transport capability.
- PR-D projects already-defined backend/domain truth and does not invent state.

A dependency finding may justify changing execution order, but MUST NOT collapse these behavioral scopes into one large PR.

---

## 3. Reuse M1 infrastructure — do not build parallel stacks

M2 MUST reuse or minimally generalize existing M1 assets where equivalent functionality already exists.

Expected reusable assets include:

- `SupplierGateway`
- `SingleFlightAuth`
- `SafeAuditPayload`
- encrypted session / token persistence
- lifecycle / single-owner lock
- durable commit + restart recovery patterns
- centralized transport / gateway boundary
- secret-safe logging and evidence sanitization

The following new parallel classes/components are prohibited by default unless a reviewed justification proves the existing M1 layer cannot be safely generalized:

```text
MarketplaceGateway
SmartStoreHttpClient
SmartStoreSecretStore
SmartStoreSingleFlightAuth
SmartStoreLifecycleLock
```

If reuse is blocked only because an existing abstraction is supplier-named or supplier-typed:

1. inspect all existing call sites;
2. minimally generalize the shared abstraction;
3. preserve M1 behavior and regression coverage;
4. do not create a second equivalent layer;
5. keep the refactor subordinate to the current PR objective.

Every implementation PR reports, by default:

```text
New infrastructure introduced: none
```

Any deviation requires explicit justification.

---

# 4. PR-B — capability state machine + workflow overlay

## 4.1 Objective

Implement SmartStore capability truth first, without provider credentials or real SmartStore calls.

PR-B owns the domain state model used by later PRs.

## 4.2 Independent axes

The implementation MUST preserve these as independently queryable fields/concepts:

```text
auth
write_scope.status
write.status
contract_freshness
workflow_state
workflow_scope
reason_code
error_class
remote_outcome
```

Do not collapse them into convenience states such as `READY_TO_REGISTER`, `CONNECTED_WITH_PERMISSION`, or `AUTH_OR_SCOPE_ERROR`.

A structured workflow overlay object may group `workflow_state + workflow_scope + reason_code` as a value object, but those fields remain independently represented and MUST NOT absorb `error_class`, `write_scope`, `auth`, `write`, `contract_freshness`, or `remote_outcome`.

## 4.3 §17 is the master enforcement checklist

`docs/platforms/smartstore/CAPABILITY_MAPPING.md §17` is the master implementation checklist.

**Do not pin or duplicate the number of §17 targets in this instruction document.**

Rules:

1. every current §17 target must have an explicit owning implementation PR assigned by the canonical §17 ownership metadata;
2. adding a §17 target without assigning an owning PR in the same contract change is forbidden;
3. each owning PR must provide a named test traceable 1:1 to each target at the layer it owns;
4. split-layer targets require tests from every owning layer; one PR MUST NOT fake another PR's coverage;
5. PR-B implements/tests only the PR-B-owned domain/state/API portion and records the remaining targets as deferred to their canonical owning PRs;
6. do not rewrite §17 into a weaker local checklist.

The current ownership decision is:

- PR-B owns domain/state targets and state-transition semantics;
- PR-A owns the real registry-gated `NOT_ADOPTED` pre-I/O enforcement target;
- PR-C owns A0 zero-network handling/non-promotion;
- PR-D owns UI glyph/projection portions of split evidence-strength and field-separation targets.

The exact per-target ownership lives with `CAPABILITY_MAPPING.md §17`; this instruction intentionally does not copy a cardinality.

Repository-rule follow-up: this incident is evidence for adding a future `test_repository_rules.py` consistency check that fails when a §17 target has no owner assignment. That CI extension is not PR-B runtime scope and must not be smuggled into PR-B unless separately reviewed.

## 4.4 Frozen workflow scope

M2 `workflow_scope` accepts exactly:

```text
AUTHENTICATION
PRODUCT_REGISTRATION
```

No provider-specific durable scope string may be invented.

## 4.5 Frozen PAUSED reasons

Exactly:

```text
AUTH_RETRY_LIMIT
APPLICATION_REAUTH_REQUIRED
SCOPE_INSUFFICIENT
ACCOUNT_RESTRICTED
```

`APPLICATION_REAUTH_REQUIRED` automatic detection remains disabled until the corresponding R0 contract is accepted.

The following alone MUST NOT assign it:

```text
401
GW.AUTHN
token failure
application age
```

Unresolved suspected application re-auth converges fail-closed to:

```text
workflow_state = REVIEW_REQUIRED
workflow_scope = AUTHENTICATION
reason_code    = null
```

## 4.6 PR-B test model

PR-B tests are fixture / unit / repository/domain driven.

```text
SmartStore credential required = no
SmartStore provider calls      = 0
```

Provider/network calls from PR-B are a scope violation, not extra confidence.

The PR-B audit must verify:

- nine-axis independence;
- 1:1 named-test traceability for every PR-B-owned §17 target;
- exact zero SmartStore provider calls;
- no PR-C/PR-A/PR-D implementation pulled forward.

---

# 5. Shared contract: endpoint mapping revision provider

A0 invalidation depends on the current endpoint-to-permission mapping revision.

This MUST NOT be implemented by runtime parsing of Markdown documentation, `SOURCES.md`, upstream version strings, or document hashes. Documentation provenance and runtime truth remain separate.

## 5.1 Interface boundary

PR-B introduces/owns only the minimal domain-facing contract required to ask for the current mapping revision, conceptually:

```text
EndpointMappingRevisionProvider.current_revision()
```

The exact name may follow project conventions.

- PR-C consumes the interface.
- PR-A supplies the authoritative implementation from the endpoint registry.
- PR-C MUST NOT hardcode a temporary revision string that PR-A later replaces.

## 5.2 Authoritative revision

PR-A owns the actual SmartStore endpoint mapping revision together with the endpoint registry, conceptually:

```text
SMARTSTORE_ENDPOINT_MAPPING_REVISION = "m2-connect-r1"
```

The value is not derived from documentation at runtime.

## 5.3 Mandatory bump invariant

Any permission-relevant endpoint-registry mapping change MUST change the mapping revision in the same PR.

CI MUST enforce this mechanically, preferably through a deterministic fingerprint/hash of the permission-relevant registry content bound to the human-readable revision.

```text
permission-relevant mapping changed
AND mapping revision not changed
→ CI FAIL
```

---

# 6. PR-C — A0 permission attestation input + storage + invalidation

PR-C implements `SMARTSTORE-A0-PERMISSION` using PR-B state and the revision-provider interface.

Absolute invariant:

```text
SmartStore network calls = 0
```

Attestation input/save/read-back/validation/invalidation/rendering all assert zero SmartStore transport calls.

A0 means:

```text
OPERATOR_ATTESTED_EVIDENCE
```

It does NOT establish provider runtime truth, R0, MACHINE_VERIFIED permission, or `write=READY`.

Persist at least:

```text
status
evidence_source
evidence_strength
observed_at
application_fingerprint
required_groups
observed_groups
endpoint_mapping_revision
freshness_status
```

Invalidation inputs include application fingerprint mismatch, required-group change, mapping-revision change, freshness expiry, and malformed/incomplete evidence. Fail closed to:

```text
write_scope = UNKNOWN
```

Do not fabricate `MISSING` without positive evidence.

A0 PASS does not populate `PERMISSIONS_SCOPES.md verified_at`; `NO_MACHINE_READABLE_PERMISSION_SOURCE` remains applicable until the owning contract's verification path exists.

PR-C owns the A0 **input UI**. PR-D owns the later read-only three-layer status projection.

---

# 7. PR-A — SmartStore adapter + endpoint registry

PR-A implements exactly the M2 adopted endpoints:

```text
SMARTSTORE_AUTH_TOKEN
SMARTSTORE_SELLER_ACCOUNT
```

Everything else remains `NOT_ADOPTED`.

`docs/platforms/smartstore/ENDPOINT_MATRIX.md §12` is the direct adapter/registry implementation checklist. Do not rewrite its invariants into a weaker local summary.

Especially preserve:

- base URL/method/path single source of truth;
- endpoint-ID registry routing;
- raw/general HTTP bypass forbidden;
- `NOT_ADOPTED` rejection before transport I/O;
- redirect auto-follow forbidden;
- explicit endpoint-specific success predicates;
- current committed bearer only for protected calls;
- token candidate not current until durable commit;
- seller-account proof binds current generations;
- product/category/image endpoint calls remain zero.

M2 SELF token request:

```text
grant_type = client_credentials
type       = SELF
account_id = omitted
```

Token success requires HTTP 200, JSON object, non-empty `access_token`, positive integer `expires_in`, and case-insensitive `Bearer` token type.

`AUTH_MISMATCH` never auto-rebinds.

PR-A also supplies the authoritative implementation of `EndpointMappingRevisionProvider` and owns the mapping revision/fingerprint CI invariant.

---

# 8. PR-D — SmartStore CONNECT three-layer UI projection

Healthy example:

```text
인증      ● 연결됨
등록 권한 ◐ 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

Semantics:

```text
● = machine/runtime-proven strong evidence
◐ = operator-attested limited-strength positive evidence
○ = unverified / unknown
```

UI preserves:

```text
auth READY
!= write_scope READY
!= write READY
```

Frontend is projection only. It MUST NOT parse provider error strings into durable state, convert UNKNOWN to MISSING, convert A0 to MACHINE_VERIFIED, or recreate PR-B state transitions.

PR-C owns the A0 input/save form. PR-D owns read-only capability/evidence projection and glyphs.

Follow ICBM UI rules: restrained charcoal/white/metallic theme, policy/help under `ⓘ`, and no explanatory paragraph directly under headings.

---

# 9. Testing and regression floor

Before real-provider acceptance use only:

```text
unit tests
fixtures
fake transport
mock provider responses
repository tests
restart tests
failure injection
network spies
```

Development MUST NOT use exploratory real-account SmartStore calls.

Every implementation PR preserves:

- required CI jobs;
- M0 acceptance;
- M1 KM통상 CONNECT behavior;
- single-data-directory ownership;
- lifecycle lock semantics;
- durable session semantics;
- secret-safe logs/audit evidence;
- no unrelated Collector/product/pricing/order changes.

---

# 10. Required PR report

Every implementation PR body includes:

```text
Objective
Base SHA
Head SHA
Changed files
Behavior implemented
Contracts referenced
Existing M1 assets reused
Any shared abstraction changed
Network endpoints reachable
Network endpoints proven unreachable
Tests added/changed
Regression results
Known gaps
Out-of-scope work
```

And, by default:

```text
New infrastructure introduced: none
```

Any new gateway/auth/session/lock/secret-store layer requires a separate justification explaining why M1 assets could not be reused/generalized.

---

# 11. Prohibited work during M2 implementation

Do not:

- implement SmartStore product CREATE/UPDATE/DELETE;
- probe product/category/image APIs;
- call a `NOT_ADOPTED` endpoint;
- use real-account exploratory requests during development;
- probe product APIs to infer permissions;
- bypass endpoint registry with a raw/general HTTP client;
- derive durable state from provider error text alone;
- merge AUTH readiness and write readiness;
- merge A0 and R0;
- display A0 as machine verified;
- resurrect `SMARTSTORE-R0-PERMISSION`;
- create parallel `MarketplaceGateway`, `SmartStoreHttpClient`, `SmartStoreSecretStore`, `SmartStoreSingleFlightAuth`, or `SmartStoreLifecycleLock` stacks without reviewed justification;
- parse Markdown at runtime to derive endpoint mapping revision;
- allow permission-relevant mapping changes without revision bump;
- restore the rejected pricing rule `max(target_margin_price, minimum_sale_price)` during unrelated refactoring;
- modify unrelated Collector/product registration/pricing/order behavior.

---

# 12. Implementation complete != M2 ACCEPTED

Merging PR-B, PR-C, PR-A, and PR-D completes planned runtime implementation only. It does not make M2 accepted.

After implementation, execute `docs/acceptance/M2.md` as a separately controlled campaign.

Required M2 R0:

```text
SMARTSTORE-R0-TOKEN
SMARTSTORE-R0-SELLER-ACCOUNT
SMARTSTORE-R0-FIRST-TOKEN-CRASH
```

A0:

```text
SMARTSTORE-A0-PERMISSION
```

Allowed non-gating long-horizon states:

```text
SMARTSTORE-R0-TOKEN-REISSUE-WINDOW = DEFERRED_LONG_HORIZON
SMARTSTORE-R0-APP-REAUTH           = BLOCKED_BY_TIME
```

Main campaign hard caps:

```text
POST SMARTSTORE_AUTH_TOKEN      <= 8
GET  SMARTSTORE_SELLER_ACCOUNT  <= 6
all other SmartStore endpoints  = 0
```

FIRST-TOKEN-CRASH boundary attempts: maximum 2.

Until M2 acceptance is actually completed, SmartStore product write remains `UNVERIFIED`.

---

# 13. First implementation target

Start with **PR-B only**.

Do not begin PR-C, PR-A, or PR-D changes in the same branch.

PR-B will be audited specifically for:

1. preservation of all independent capability/workflow/error/outcome axes;
2. compliance with every **PR-B-owned** `CAPABILITY_MAPPING.md §17` target, with named 1:1 test traceability;
3. exact zero SmartStore provider/network calls;
4. no duplicate M1 infrastructure;
5. no provider-specific free-text durable state;
6. clean dependency seams for later A0 and endpoint-registry work;
7. explicit deferral, not fake coverage, for §17 targets owned by PR-A/PR-C/PR-D.
