# SmartStore CAPABILITY_MAPPING §17 — Implementation Ownership Registry

- Status: **NORMATIVE IMPLEMENTATION OWNERSHIP METADATA**
- Owning contract: `CAPABILITY_MAPPING.md §17`
- Scope: M2 SmartStore CONNECT implementation PR ownership only
- This file does **not** redefine capability semantics. `CAPABILITY_MAPPING.md §17` remains the master target list.
- The target-ID set in this file MUST exactly match the target-ID set in `CAPABILITY_MAPPING.md §17`. CI enforcement is not implemented yet, so reviewers MUST currently perform this exact-set comparison manually.

## 1. Purpose

This registry exists because implementation ownership and the semantic target list evolve at different layers.

Rules:

1. Every target present in `CAPABILITY_MAPPING.md §17` MUST have an ownership row here.
2. A target MUST NOT be added to §17 without assigning an owning PR in the same contract-change PR.
3. This registry MUST NOT contain a target that no longer exists in §17.
4. Numeric cardinality such as `15/15` or `18/18` MUST NOT be copied into implementation instructions. The live §17 list is authoritative.
5. Each owning PR MUST provide a named test traceable 1:1 to the target at the layer it owns.
6. Split-layer targets require evidence from every owning layer. One PR MUST NOT claim another PR's portion by fixture or mock substitution.
7. PR-B remains provider/network-call zero even where a later owning PR proves the provider/transport layer.

## 2. Current ownership

| §17 ID | Enforcement target | Owner | Required layer-specific evidence |
| --- | --- | --- | --- |
| 1 | `auth!=READY` cannot produce `write=READY` | **PR-B** | domain transition / invariant test |
| 2 | `AUTH_MISMATCH -> REVIEW_REQUIRED/AUTHENTICATION`, never auto-rebind | **PR-B** | domain workflow-overlay test |
| 3 | `write_scope=MISSING -> write=BLOCKED + PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT` | **PR-B** | domain transition test |
| 4 | `write_scope=UNKNOWN` never becomes `SCOPE_INSUFFICIENT` without positive evidence | **PR-B** | evidence-to-state fixture test |
| 5 | `workflow_scope` accepts only frozen enum and is never free text | **PR-B** | enum/schema/domain validation test |
| 6 | operator-attested permission uses limited-strength `◐` semantic marker | **PR-B + PR-D** | PR-B: `OPERATOR_ATTESTED` strength remains distinct in domain/API; PR-D: renders limited-strength marker without promotion |
| 7 | machine-verified permission uses distinct strong `●` semantic marker when supported | **PR-B + PR-D** | PR-B: `MACHINE_VERIFIED` remains distinct semantic strength; PR-D: renders distinct strong marker only when supported |
| 8 | `NOT_ADOPTED` endpoint fails before network I/O | **PR-A** | real registry-gated caller / transport-spy test; PR-B MUST NOT fake this coverage |
| 9 | M2 product write cannot become READY | **PR-B** | domain capability invariant test |
| 10 | `STALE` preserves existing runtime proof and blocks expansion without extra safety judgment | **PR-B** | freshness/domain transition test |
| 11 | `REVIEW_REQUIRED` freshness stops operations depending on disputed invariant | **PR-B** | freshness/domain gate test |
| 12 | `PAUSED` requires one frozen reason and one frozen scope | **PR-B** | workflow value/schema invariant test |
| 13 | generic `GW.AUTHN` cannot map directly to `SCOPE_INSUFFICIENT` or `APPLICATION_REAUTH_REQUIRED` | **PR-B** | fixture-driven error-to-workflow test; no provider call |
| 14 | before accepted `SMARTSTORE-R0-APP-REAUTH`, suspected app re-auth maps to REVIEW_REQUIRED, not PAUSED | **PR-B** | feature/evidence-gate state test |
| 15 | after accepted app-reauth detection contract, recovery requires provider re-auth + fresh session + fresh identity proof | **PR-B** | state-machine contract test with fixtures only; real provider recovery evidence belongs later acceptance, not PR-B development |
| 16 | UI/API keeps auth, permission, write, evidence strength, workflow state/scope, and freshness separate | **PR-B + PR-D** | PR-B: independent domain/persistence/read-API fields; PR-D: UI projection does not collapse them |
| 17 | persisted READY loses to current evidence after restart | **PR-B** | repository/restart convergence test |
| 18 | `SMARTSTORE-A0-PERMISSION` handling performs zero SmartStore calls and never promotes A0 to R0/MACHINE_VERIFIED | **PR-C** | A0 save/read/invalidate/render transport-spy tests; PR-B MUST NOT fake this coverage |
| 19 | new capability bootstrap is `UNRECORDED`; the four-state freshness behavior matrix, closed/non-reentrant transition graph, same-value reviewed re-recording provenance, and prohibition on PR-A fabricating `CURRENT` are enforced | **PR-B** | named domain/persistence/service/API tests for `UNRECORDED`, allowed/forbidden transitions, `freshness_recorded_at`, actor audit, same-value re-recording, and local operator recording entry point with zero provider calls |
| 20 | same-scope workflow convergence promotes `REVIEW_REQUIRED` to a positively proven frozen `PAUSED` reason while ambiguity never erases an existing proven `PAUSED` reason | **PR-B** | named domain transition tests covering promotion and preservation in both directions |
| 21 | operator-attested evidence persists `attested_status` (consistent with required/observed groups) and the freshness-policy bound in effect at recording; current `freshness_status` is never persisted | **PR-C** | migration/model/domain tests: DB and domain consistency of `attested_status`, the stored bound, and no current-freshness column |
| 22 | canonical 30-day bound on the injected clock: `FRESH` just before and exactly at the bound, `EXPIRED` just after; `READY` and `MISSING` both converge to `UNKNOWN`; expired `MISSING` removes the evidence-dependent `SCOPE_INSUFFICIENT` pause without promoting `write` | **PR-C** | named deterministic boundary tests for `READY` and for `MISSING`; named integration test for the pause release; config tests for default 30 and override 1..30 only |
| 23 | time-driven expiry converges through the existing read path; the first read after the bound produces exactly one durable change and one audit event, repeated reads none; the attestation record is never mutated | **PR-C** | named integration test counting state writes and `MARKETPLACE_CAPABILITY_CHANGED` events across repeated reads; no scheduler |

## 3. PR-B implementation boundary

PR-B starts first and implements only its owned domain/state/API portions.

PR-B MUST report the §17 traceability table in its PR body with:

```text
§17 target
owner
named test
status
notes / deferred owner when split or later-owned
```

For target 8 and target 18, PR-B records the canonical later owner and performs no substitute implementation.

For targets 6, 7 and 16, PR-B implements only the domain/API semantic portion. PR-D later proves the visual/projection portion.

For target 19, PR-B owns a local operator-authorized freshness-recording entry point, persistence/audit provenance, and transition enforcement. It MUST make zero SmartStore/provider calls. PR-A consumes recorded freshness but MUST NOT author, infer, seed, or fabricate `CURRENT`. PR-D may later surface the operator UI without becoming the source of truth.

PR-B provider/network calls remain exactly zero.

## 3.1 PR-C boundary for targets 18 and 21–23

PR-C owns operator-attested permission evidence at the domain, persistence, service, API and A0 input-UI layers, with zero SmartStore/provider calls.

For targets 22 and 23, PR-C may adjust the permission convergence path it consumes from PR-B (the permission transition and the service convergence) so that expired `MISSING` evidence releases the evidence-dependent `SCOPE_INSUFFICIENT` overlay (`CAPABILITY_MAPPING.md` S6). This transfers no PR-B target. PR-B's named tests for targets 3, 4, 12 and 20 remain required; if one of them encodes behaviour that S6 changes, PR-C updates it explicitly and cites S6 in its PR body.

PR-C MUST NOT add a periodic scheduler for expiry (target 23, S7). PR-D later owns the read-only projection of the expired state.

## 3.2 PR-D and PR-E boundary (Issue #41, decision 5667551746)

PR-D proves the UI halves of targets 6, 7 and 16 with real-browser tests that reuse the existing in-process harness (the installed browser; every request answered in-process). The tests are named `test_s17_06_*`, `test_s17_07_*` and `test_s17_16_*`. Each asserts zero SmartStore/provider calls and zero egress while rendering. PR-D is read-only projection, following `CAPABILITY_MAPPING.md` §14, including §14.8–§14.11.

PR-E (M2 operator actions) owns no §17 target. It adds the mutating operator entry points under the instructions' PR-E section. It MUST NOT be used to claim any PR-D projection coverage.

## 4. Repository-rule follow-up

The stale `15/15` incident that triggered issue #23 is evidence that ownership/cardinality consistency should become machine-checked repository policy.

Recommended future `test_repository_rules.py` invariant:

```text
parse CAPABILITY_MAPPING.md §17 target IDs
parse this ownership registry target IDs
assert exact set equality
assert every target has >= 1 owner
assert no duplicate target ID
```

If feasible, also validate that split targets name all required layer owners.

This repository-rule enhancement is **not** part of PR-B runtime scope unless separately reviewed. The immediate architectural rule is already normative: an unowned §17 target is invalid contract metadata and blocks implementation.
