# ICBM M2 SmartStore CONNECT — Claude Code Implementation Instructions

- Status: **IMPLEMENTATION INSTRUCTION / DESIGN FROZEN**
- Milestone: M2 SmartStore CONNECT
- Base main SHA: `b001e557463fe03e48663679b533d6ab941438c9`
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

## 1. Canonical contract bundle

Implementation MUST follow the frozen contract set directly instead of re-summarizing or weakening it:

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

Implementation is split into four behavioral PRs:

```text
PR-B  capability state machine + workflow overlay
PR-C  A0 permission attestation input + storage + invalidation
PR-A  SmartStore adapter + endpoint registry
PR-D  SmartStore CONNECT three-layer UI projection
```

Recommended order is frozen as:

```text
PR-B → PR-C → PR-A → PR-D
```

Reason:

- PR-B defines the domain truth model used by C and A.
- PR-C can then implement A0 entirely with zero provider network traffic.
- PR-A is the first unit that introduces real SmartStore transport capability and therefore comes later.
- PR-D projects already-defined backend/domain truth and does not invent state.

A dependency-discovery finding may justify changing execution order, but MUST NOT collapse these behavioral scopes into one large PR.

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

Creating a parallel marketplace infrastructure stack is prohibited by default.

The following new parallel classes/components are specifically disallowed unless a reviewed justification proves the existing M1 layer cannot be safely generalized:

```text
MarketplaceGateway
SmartStoreHttpClient
SmartStoreSecretStore
SmartStoreSingleFlightAuth
SmartStoreLifecycleLock
```

If reuse is blocked only because an existing abstraction is supplier-named or supplier-typed:

1. inspect all existing call sites;
2. minimally generalize the existing shared abstraction;
3. preserve M1 behavior and regression coverage;
4. do not create a second equivalent layer;
5. keep the refactor subordinate to the current PR objective.

Every implementation PR MUST report:

```text
New infrastructure introduced: none
```

as the default expected result.

Any deviation requires an explicit justification section.

---

# 4. PR-B — capability state machine + workflow overlay

## 4.1 Objective

Implement the SmartStore capability truth model first, without provider credentials or real SmartStore calls.

This PR is the owner of the domain state model used by all later PRs.

## 4.2 Required independent axes

The implementation MUST preserve these axes as independent fields/concepts:

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

Do not merge multiple axes into a convenience enum, single status string, or frontend-only composite state.

Examples of forbidden collapses:

```text
CONNECTED_WITH_PERMISSION
READY_TO_REGISTER
AUTH_OR_SCOPE_ERROR
FAILED_REVIEW_REQUIRED
```

if such a value destroys the independently queryable underlying axes.

## 4.3 Enforcement checklist

`docs/platforms/smartstore/CAPABILITY_MAPPING.md §17` is the implementation enforcement checklist.

Do NOT write a weaker replacement checklist in code comments or tests.

All 15 enforcement targets in §17 must have direct test coverage.

Representative invariants include:

```text
auth != READY
→ write != READY

AUTH_MISMATCH
→ workflow_state=REVIEW_REQUIRED
→ workflow_scope=AUTHENTICATION
→ no auto-rebind

write_scope=MISSING
→ write=BLOCKED
→ workflow_state=PAUSED
→ workflow_scope=PRODUCT_REGISTRATION
→ reason_code=SCOPE_INSUFFICIENT

write_scope=UNKNOWN
!= MISSING

write_scope=READY
!= write=READY

M2 product write
= UNVERIFIED
```

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

Adapters MUST NOT invent additional durable reason strings.

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

Tests should be fixture / unit / repository driven.

No SmartStore credential is required.
No SmartStore provider call is allowed.

The architecture audit for this PR will specifically check that the nine axes above remain independent in persistence, domain transitions, APIs, and tests.

---

# 5. Shared contract: endpoint mapping revision provider

A0 evidence invalidation depends on the current endpoint-to-permission mapping revision.

This MUST NOT be implemented by runtime parsing of Markdown documentation, `SOURCES.md`, upstream version strings, or document hashes.

Documentation provenance and runtime truth remain separate.

## 5.1 Interface boundary

PR-B introduces or owns only the minimal domain-facing contract required to ask for the current mapping revision, for example conceptually:

```text
EndpointMappingRevisionProvider.current_revision()
```

The exact name may follow existing project conventions.

PR-C consumes this interface.

PR-A later supplies the authoritative implementation from the SmartStore endpoint registry.

PR-C MUST NOT temporarily hardcode a revision string that PR-A must later search-and-replace.

## 5.2 Authoritative revision

PR-A owns the actual SmartStore endpoint mapping revision together with the endpoint registry, conceptually:

```text
SMARTSTORE_ENDPOINT_MAPPING_REVISION = "m2-connect-r1"
```

The value is not derived from documentation at runtime.

## 5.3 Mandatory bump invariant

Any code change that modifies the adopted endpoint registry's permission-relevant mapping MUST also change the mapping revision in the same PR.

CI MUST enforce this mechanically.

Recommended pattern:

- compute a deterministic canonical fingerprint/hash of the permission-relevant registry content;
- bind/store the expected fingerprint beside the human-readable mapping revision;
- fail tests if registry mapping content changes while the revision/fingerprint contract is unchanged.

The exact implementation may vary, but the invariant must be executable:

```text
permission-relevant endpoint mapping changed
AND mapping revision not changed
→ CI FAIL
```

This prevents `endpoint_mapping_revision` invalidation from becoming dead code.

---

# 6. PR-C — A0 permission attestation input + storage + invalidation

## 6.1 Objective

Implement `SMARTSTORE-A0-PERMISSION` using the state model from PR-B and the revision-provider interface defined above.

This PR remains network-independent.

## 6.2 Absolute network invariant

```text
SmartStore network calls = 0
```

The following operations must all assert zero SmartStore transport calls:

```text
attestation input
attestation save
attestation read-back
attestation validation
attestation invalidation
attestation display/input rendering
```

Use a transport/gateway spy or equivalent test seam.

## 6.3 Evidence meaning

A0 is:

```text
OPERATOR_ATTESTED_EVIDENCE
```

It proves only that ICBM received and handled operator-attested evidence at the recorded strength.

It does NOT establish:

```text
provider contract truth
provider runtime truth
R0 evidence
MACHINE_VERIFIED permission
write=READY
```

## 6.4 Evidence envelope

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

Positive operator evidence uses:

```text
evidence_strength = OPERATOR_ATTESTED
```

## 6.5 Invalidation

Any of the following invalidates the existing positive attestation for current use:

```text
application_fingerprint mismatch
required_groups change
endpoint_mapping_revision change
freshness expiry
malformed/incomplete evidence
```

The fail-closed result is:

```text
write_scope = UNKNOWN
```

Do not fabricate `MISSING` without positive evidence.

## 6.6 `verified_at` boundary

A0 PASS does not populate `PERMISSIONS_SCOPES.md verified_at`.

The structural limitation remains:

```text
verified_at = null
reason = NO_MACHINE_READABLE_PERMISSION_SOURCE
```

until the contract's separately defined verification path exists.

## 6.7 A0 input UI belongs to PR-C

PR-C owns the operator input interaction needed to create/update an attestation.

Conceptual minimum:

```text
필수 API 그룹:   <read-only/current required groups>
관찰된 API 그룹: [ ] 그룹 A  [ ] 그룹 B ...
확인 일시:       자동 기록
[ 권한 확인 저장 ]
```

The exact UI must follow existing ICBM form components and visual conventions.

Rules:

- `observed_at` is recorded automatically on save;
- application fingerprint is derived from current configured application identity, not manually typed;
- endpoint mapping revision is obtained through the revision-provider interface, not manually typed;
- required groups come from the current domain/registry mapping, not arbitrary operator text;
- observed groups are the operator attestation input;
- malformed/incomplete submissions cannot become READY.

This input UI is PR-C scope.

The three-layer status badges/projection are NOT PR-C scope; they belong to PR-D.

---

# 7. PR-A — SmartStore adapter + endpoint registry

## 7.1 Objective

Implement the SmartStore M2 adapter with exactly the two adopted endpoints and bind the real endpoint mapping revision provider.

Adopted endpoint IDs are exactly:

```text
SMARTSTORE_AUTH_TOKEN
SMARTSTORE_SELLER_ACCOUNT
```

Everything else remains `NOT_ADOPTED` for M2.

## 7.2 Endpoint matrix authority

`docs/platforms/smartstore/ENDPOINT_MATRIX.md §12` contains the 10 frozen implementation invariants.

Use those 10 items directly as the implementation/test checklist.

Do not rewrite them into a weaker local summary.

Especially preserve:

- one source of truth for base URL / method / path;
- endpoint-ID registry routing;
- raw/general HTTP bypass forbidden;
- NOT_ADOPTED rejection before transport I/O;
- redirect auto-follow forbidden;
- explicit endpoint-specific success predicates;
- current committed bearer only for protected calls;
- token candidate is not current session until durable commit;
- seller-account proof binds current generations;
- product/category/image endpoint calls remain zero.

Conceptually all provider I/O flows through one registry-gated caller:

```text
caller.call(endpoint_id, request_data)
```

SmartStore business code MUST NOT construct a URL and invoke a raw client directly.

## 7.3 Auth request

M2 SELF request:

```text
grant_type = client_credentials
type       = SELF
account_id = omitted
```

Token success requires:

```text
HTTP 200
JSON object
access_token non-empty
expires_in positive integer
token_type == Bearer (case-insensitive)
```

Token response is only a candidate until durable session commit.

## 7.4 AUTH_READY path

```text
token response
→ provisional candidate
→ durable session commit
→ GET SMARTSTORE_SELLER_ACCOUNT
→ accountUid present
→ expected identity compare/bind
→ AUTH_READY candidate
```

`AUTH_MISMATCH` never auto-rebinds.

## 7.5 Mapping revision implementation

PR-A supplies the authoritative implementation of the revision-provider contract introduced earlier.

The endpoint registry and mapping revision/fingerprint enforcement live together.

Any permission-relevant registry mapping change without a revision bump must fail CI.

## 7.6 Infrastructure reuse

PR-A must first map SmartStore needs onto existing M1 gateway/auth/session/lifecycle primitives.

Do not introduce the prohibited parallel classes from §3 simply because the provider is a marketplace instead of a supplier.

---

# 8. PR-D — SmartStore CONNECT three-layer UI projection

## 8.1 Objective

Display three distinct truth layers without recomputing state in frontend code.

Healthy example:

```text
인증      ● 연결됨
등록 권한 ◐ 권한 확인됨 (관리자 화면 확인)
실제 등록 ○ 미확인
```

Evidence semantics:

```text
● = machine/runtime-proven strong evidence
◐ = operator-attested limited-strength positive evidence
○ = unverified / unknown
```

## 8.2 Required separation

UI must preserve:

```text
auth READY
!= write_scope READY
!= write READY
```

M2 product write remains:

```text
UNVERIFIED
```

Therefore the UI must not imply actual SmartStore product-registration readiness or success.

## 8.3 Frontend is projection only

Frontend MUST NOT:

- parse provider error strings to create durable workflow state;
- perform localized string matching to determine domain truth;
- convert `UNKNOWN` into `MISSING`;
- convert operator-attested A0 evidence into machine-verified evidence;
- treat `write_scope=READY` as `write=READY`;
- recreate capability transition logic already implemented in PR-B.

## 8.4 UI responsibility boundary

PR-C owns A0 attestation input/save interaction.

PR-D owns the read-only capability/evidence projection, including the three-layer badges/status presentation.

Do not duplicate the A0 input form in PR-D.

Follow ICBM UI rules:

- restrained charcoal / white / metallic theme;
- no explanatory paragraph directly under section headings;
- policy/help text goes in `ⓘ` tooltip;
- actual status/value/time evidence may be shown directly.

---

# 9. Testing strategy

## 9.1 Before real-provider acceptance

Implementation development uses:

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

Real provider calls belong only to the reviewed M2 acceptance campaign after implementation PRs are complete.

## 9.2 Regression floor

Every PR must preserve:

- required CI jobs;
- M0 acceptance;
- M1 KM통상 CONNECT behavior;
- single-data-directory ownership;
- lifecycle lock semantics;
- durable session semantics;
- secret-safe logs/audit evidence;
- no unrelated Collector / product / pricing / order behavior changes.

A SmartStore improvement that breaks an accepted M0/M1 behavior is a failure.

---

# 10. Required PR report from Claude Code

Every implementation PR body must include:

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

And explicitly:

```text
New infrastructure introduced: none
```

unless there is a separately justified exception.

If any new gateway/auth/session/lock/secret-store layer is introduced, the PR must explain why the existing M1 asset could not be safely reused/generalized and identify all alternatives evaluated.

---

# 11. Prohibited work during M2 implementation

Do not:

- implement SmartStore product CREATE / UPDATE / DELETE;
- probe product/category/image APIs;
- call any NOT_ADOPTED endpoint;
- use real-account exploratory requests during development;
- probe product APIs to infer permissions;
- bypass the endpoint registry with a raw/general HTTP client;
- derive durable state from provider error text alone;
- merge AUTH readiness and write readiness;
- merge A0 and R0 evidence;
- display A0 as machine verified;
- resurrect `SMARTSTORE-R0-PERMISSION`;
- create parallel `MarketplaceGateway`, `SmartStoreHttpClient`, `SmartStoreSecretStore`, `SmartStoreSingleFlightAuth`, or `SmartStoreLifecycleLock` stacks without reviewed justification;
- parse Markdown documentation at runtime to derive endpoint mapping revision;
- allow permission-relevant registry mapping changes without a mapping-revision bump;
- restore the previously rejected pricing rule `max(target_margin_price, minimum_sale_price)` during unrelated refactoring;
- modify unrelated Collector/product registration/pricing/order behavior.

---

# 12. Implementation complete != M2 ACCEPTED

Merging PR-B, PR-C, PR-A, and PR-D completes the planned runtime implementation work only.

It does NOT automatically mean:

```text
M2 = ACCEPTED
```

After implementation, run the separately controlled acceptance campaign in `docs/acceptance/M2.md`.

Required milestone R0 slots:

```text
SMARTSTORE-R0-TOKEN
SMARTSTORE-R0-SELLER-ACCOUNT
SMARTSTORE-R0-FIRST-TOKEN-CRASH
```

A0 slot:

```text
SMARTSTORE-A0-PERMISSION
```

Allowed non-gating long-horizon states:

```text
SMARTSTORE-R0-TOKEN-REISSUE-WINDOW
= DEFERRED_LONG_HORIZON

SMARTSTORE-R0-APP-REAUTH
= BLOCKED_BY_TIME
```

Main campaign hard caps:

```text
POST SMARTSTORE_AUTH_TOKEN      <= 8
GET  SMARTSTORE_SELLER_ACCOUNT  <= 6
all other SmartStore endpoints  = 0
```

FIRST-TOKEN-CRASH boundary attempts:

```text
maximum 2
```

Until M2 acceptance is actually completed, SmartStore product write remains:

```text
UNVERIFIED
```

---

## 13. First implementation target

Start with **PR-B** only.

PR-B scope is the capability state machine and workflow overlay defined above.

Do not begin PR-C, PR-A, or PR-D changes in the same branch.

The first PR will be audited specifically for:

1. preservation of all independent capability/workflow/error/outcome axes;
2. compliance with `CAPABILITY_MAPPING.md §17` 15/15 enforcement targets;
3. no SmartStore network calls;
4. no duplicate M1 infrastructure;
5. no provider-specific free-text durable state;
6. clean dependency seams for the later A0 revision-provider consumer.
