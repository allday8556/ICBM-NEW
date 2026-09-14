# SmartStore CAPABILITY_MAPPING §17 — Implementation Ownership Registry

- Status: **NORMATIVE IMPLEMENTATION OWNERSHIP METADATA**
- Owning contract: `CAPABILITY_MAPPING.md §17`
- Scope: M2 SmartStore CONNECT implementation PR ownership only
- This file does **not** redefine capability semantics. `CAPABILITY_MAPPING.md §17` remains the master target list.

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

PR-B provider/network calls remain exactly zero.

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
