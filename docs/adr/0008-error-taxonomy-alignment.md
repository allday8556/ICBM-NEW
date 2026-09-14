# ADR-0008 — Error taxonomy alignment: runtime v1 `ErrorClass` and Canonical v3.1 §11.3

Status: **ACCEPTED** 2026-09-14 — Architect Stage-0 audit on PR #35, review `5197614709`. Stage 1 is not implemented yet; until it merges, the runtime keeps its 7-value `ErrorClass` (see Staging).
Decision owner: Architect (ChatGPT). Sources:
- Issue #25 body;
- architect comment `5657613795` (the first decision);
- architect comment `5657655857`: v3.1 is the target; a dedicated alignment ADR/PR is required before PR-A;
- architect comment `5657668828`: not a subset; an explicit mapping table; a compatibility-preserving migration; a staged `NOT_FOUND` policy.

Recorded by: Claude Code. The number was confirmed free in `docs/adr/` immediately before writing.
Date: 2026-09-14
Related:
- ADR-0004: only `TRANSIENT` and `RATE_LIMITED` are retried automatically;
- ADR-0005: job states; non-retryable classes go to `DEAD`;
- `docs/ARCHITECTURE.md` §8;
- `docs/platforms/smartstore/ERRORS.md`;
- `docs/platforms/smartstore/CAPABILITY_MAPPING.md` (the `error_class` axis).

Supersedes: in `5657613795`, "resolution (a)" and "do not create global `CONFLICT`, `DUPLICATE`, `REVIEW_REQUIRED`, or `FATAL` classes in this M2 work", as refined by `5657655857` and `5657668828`. ADR-0004 and ADR-0005 are not edited; see Consequences.

---

## Context

### Two value sets that only partially overlap

The runtime implements the v1 set of `docs/ARCHITECTURE.md` §8 in `app/core/errors.py::ErrorClass`:

```text
TRANSIENT, RATE_LIMITED, AUTH, VALIDATION, POLICY_BLOCKED, NOT_FOUND, UNKNOWN
```

Canonical v3.1 §11.3 reads, verbatim:

```text
TRANSIENT | RATE_LIMIT | AUTH | VALIDATION | POLICY
CONFLICT | DUPLICATE | REVIEW_REQUIRED | FATAL | UNKNOWN
```

v3.1 §11.3 also says: 각 class마다 `retry 가능 여부 / 횟수 / backoff / batch pause 여부 / 사용자 개입 필요`를 정책으로 갖는다.

Neither set is a subset of the other:
- `RATE_LIMITED`/`RATE_LIMIT` and `POLICY_BLOCKED`/`POLICY` are spelling pairs;
- `NOT_FOUND` exists only in v1;
- `CONFLICT`, `DUPLICATE`, `REVIEW_REQUIRED` and `FATAL` exist only in v3.1.

`ERRORS.md` used the v3.1 names while stating that it used "existing ICBM classes only".

### Where the v1 values live today

This inventory was taken at `main` `6573461`:

| Where | What | Constraint |
| --- | --- | --- |
| `jobs.last_error_class` (0001) | String(20), current state | no CHECK |
| `job_attempts.error_class` (0001) | String(20). "Append-style history" per ADR-0005: rows are never deleted but are updated when an attempt finishes. | no CHECK, no trigger |
| `supplier_connections.last_error_class` (0002) | String(20), current state | no CHECK |
| `marketplace_capabilities.error_class` (0003) | String(20), current state | **the only CHECK**: generated from the enum in the model, a frozen literal in migration 0003. No test compares the two. |
| `audit_events` JSON (`JOB_DEAD_LETTERED`, `SUPPLIER_CONNECTION_FAILED`, `MARKETPLACE_CAPABILITY_CHANGED`) | the `error_class` field | **append-only at the database level (triggers). Stored values can never be rewritten.** |
| JSON logs; committed M0 evidence (`docs/acceptance/M0/…`) | `error_class`, `"class"` | historical record |
| API error envelope | `"class"` and `"retryable"` | not in OpenAPI |
| `MarketplaceCapabilityView.error_class` | typed `ErrorClass` | **the only OpenAPI-visible field** |
| `app/connect/marketplace/service.py` | `ErrorClass(row.error_class)` | **a strict reader**: an unknown stored value raises |
| UI | shows `class · code` as text; never switches on the class | — |

`NOT_FOUND` is raised only for ICBM-local lookups:
- `SUPPLIER_UNKNOWN`;
- `MARKETPLACE_CAPABILITY_UNKNOWN` (two sites);
- `JOB_TYPE_UNKNOWN`, the only path that persists it (to `job_attempts`, `jobs` and the dead-letter audit event);
- `JOB_NOT_FOUND`;
- `DIAGNOSTICS_DISABLED`;
- framework 404s.

The supplier transport never emits it.

### Provenance gap

The frozen v3.1 document is not in this repository. The only copy available to the implementer is an untracked working-tree file. Its header reads `FREEZE CANDIDATE (v3.1) — FREEZE APPROVED 판정 대기`.

This ADR quotes §11.3 verbatim and relies on Issue #25 (`5657655857`) for its frozen status. See the confirmation points.

## Decision

### Mapping and compatibility table

The aligned taxonomy is the union of both sets, with the two spelling pairs collapsed: **11 classes**.

| Aligned class | v1 runtime | Canonical v3.1 §11.3 | Relation | In the runtime enum |
| --- | --- | --- | --- | --- |
| `TRANSIENT` | `TRANSIENT` | `TRANSIENT` | identical | yes |
| `RATE_LIMITED` | `RATE_LIMITED` | `RATE_LIMIT` | one class, two spellings; the persisted spelling is canonical (A) | yes |
| `AUTH` | `AUTH` | `AUTH` | identical | yes |
| `VALIDATION` | `VALIDATION` | `VALIDATION` | identical | yes |
| `POLICY_BLOCKED` | `POLICY_BLOCKED` | `POLICY` | one class, two spellings; the persisted spelling is canonical (A) | yes |
| `NOT_FOUND` | `NOT_FOUND` | — | v1 only; kept and scoped (C) | yes |
| `CONFLICT` | — | `CONFLICT` | v3.1 only; added (B) | implementation stage |
| `DUPLICATE` | — | `DUPLICATE` | v3.1 only; added (B) | implementation stage |
| `REVIEW_REQUIRED` | — | `REVIEW_REQUIRED` | v3.1 only; added (B) | implementation stage |
| `FATAL` | — | `FATAL` | v3.1 only; added (B) | implementation stage |
| `UNKNOWN` | `UNKNOWN` | `UNKNOWN` | identical | yes |

### A. Spelling: the persisted tokens are canonical

`RATE_LIMITED` and `POLICY_BLOCKED` are the only spellings ICBM persists, emits, types or writes in its contracts. `RATE_LIMIT` and `POLICY` are the same classes under their v3.1 names. They appear only when v3.1 is quoted.

This amends the **spelling** of v3.1 §11.3 by ADR, as `5657655857` point 2 requires. It does not change v3.1's **semantics**.

Why:
- `audit_events` is append-only, so its historical `RATE_LIMITED` and `POLICY_BLOCKED` values can never be rewritten. Keeping these spellings is the only way every durable record — rows, audit, logs and committed M0 evidence — uses one spelling.
- No data migration is needed.
- There is no dual-read path.
- The API's `class` values are unchanged.
- ADR-0004 and ADR-0005 stay literally true.

Rejected alternative (A2): adopt `RATE_LIMIT` and `POLICY` and keep the old spellings as readable legacy aliases. That would require:
- a CHECK accepting both spellings forever;
- normalisation in every reader, including audit and log consumers;
- a changed API `class` value;
- `AUTO_RETRYABLE` covering both spellings;
- a permanently split audit history.

### B. `CONFLICT`, `DUPLICATE`, `REVIEW_REQUIRED` and `FATAL` are added

The v3.1 semantics are adopted as global definitions. `ERRORS.md` §21 already applies them to SmartStore.

- **`CONFLICT`** — the operation contradicts the current state of its target, remote or local. Re-read or reconcile before any further mutation.
- **`DUPLICATE`** — positive evidence that the operation would create, or has created, a resource whose identity already exists. It takes precedence over `CONFLICT`. It is never inferred from an earlier uncertain write or its retry history. Reconcile to the existing resource; never create another copy.
- **`REVIEW_REQUIRED`** (cause) — reserved for a provider, domain or endpoint condition whose own semantics establish human judgment as the cause, when no narrower class applies. It is never assigned because a person is needed or a retry budget ran out. It is independent of `workflow_state=REVIEW_REQUIRED` and of `contract_freshness=REVIEW_REQUIRED`.
- **`FATAL`** — a proven deterministic integration defect: the same request cannot succeed without a code, configuration or contract correction. Examples:
  - a route or method that contradicts the adopted endpoint contract (`ERRORS.md` §9.3, §10.7).

  It stops the affected capability. It never burns a retry budget.

None of the four is retried automatically, and none is ever inferred from a count or an exhausted budget. As everywhere, a class never selects a workflow state by itself (`CAPABILITY_MAPPING.md` §9.3).

**Per-class policy.** v3.1 §11.3 requires one policy per class. The retry and backoff columns restate ADR-0004 and ADR-0005. The batch/queue column is the target for the batch contract (v3.1 §11.2, §11.4), which M2 does not implement.

| Class | Automatic retry | Backoff | Batch / queue effect (target) | Human action | HTTP status |
| --- | --- | --- | --- | --- | --- |
| `TRANSIENT` | yes, bounded by `max_attempts` | exponential (ADR-0005) | counts toward the failure budget | only once the workflow escalates | 503 |
| `RATE_LIMITED` | yes, bounded | scheduled; the provider quota horizon when known | slow the limited scope (v3.1 §11.4) | no | 429 |
| `AUTH` | no | — | pause the affected account queue (v3.1 §11.4) | re-authenticate under the owning auth contract | 401 |
| `VALIDATION` | no | — | counts toward the failure budget | correct the input | 422 |
| `POLICY_BLOCKED` | no | — | counts toward the failure budget | the policy condition must change | 403 |
| `NOT_FOUND` | no | — | none (ICBM-local only) | correct the reference | 404 |
| `CONFLICT` | no | — | counts toward the failure budget | reconcile first | 409 |
| `DUPLICATE` | no | — | counts toward the failure budget | reconcile to the existing resource | 409 |
| `REVIEW_REQUIRED` | no | — | counts toward the failure budget | yes | 409 |
| `FATAL` | no | — | stop the affected capability | correct code, configuration or contract | 500 |
| `UNKNOWN` | no; a write outcome is reconciled, never replayed | — | counts toward the failure budget | diagnose or reconcile | 500 |

### C. `NOT_FOUND`: kept, scoped and staged

- It stays a member permanently. Historical `job_attempts`, `jobs` and audit rows hold it (`JOB_TYPE_UNKNOWN` dead letters), and the capability reader is strict.
- **Scope:** it is raised only for an ICBM-local addressed resource that does not exist — an API path, key, identifier, registered job type or disabled local feature. This matches every current raise site. `JOB_TYPE_UNKNOWN` stays `NOT_FOUND`.
- **It never classifies a provider or wire response.** A supplier or marketplace "not found" is classified by its evidence:
  - SmartStore `GW.NOT_FOUND` → `FATAL` or `UNKNOWN` (`ERRORS.md` §9.3);
  - an API-server `NOT_FOUND` → usually `VALIDATION`, sometimes `CONFLICT`, or left uncertain (§10.4).
  No integration emits it today.
- Narrowing its emitters later (for example, reclassifying `JOB_TYPE_UNKNOWN` as `FATAL`) needs a superseding ADR, and historical rows stay readable.

### D. Compatibility and migration rules

1. **Additive only.** No member is ever removed or renamed while persisted rows or audit JSON can hold it. The strict reader and the append-only audit table make any narrowing a crash or a lie.
2. **No data migration.** Decision A means no stored value changes.
3. **One CHECK.** Only `marketplace_capabilities.error_class` lists classes. The implementation stage widens it in a new migration and never edits 0003.
4. **No new CHECKs** on the columns that lack one. A new CHECK would re-validate history and gains nothing now.
5. **API.** The envelope's `class` can carry the new values, and `retryable` keeps its meaning. The OpenAPI enum of `MarketplaceCapabilityView.error_class` widens additively. The UI already shows the class as text.
6. **Fit.** Every value fits String(20); the longest is `REVIEW_REQUIRED`, 15 characters.
7. **Committed M0 evidence** stays valid as written.

### Staging

**Stage 0 — this PR (documentation only):**
- this ADR;
- `ARCHITECTURE.md` §8 (the aligned list);
- `ERRORS.md` (aligned spellings; the §1 note on staging and `NOT_FOUND`);
- one `CAPABILITY_MAPPING.md` row.

The runtime is unchanged.

**Stage 1 — one implementation PR, after this ADR is accepted and before PR-A.** It is separate from this PR.

- `ErrorClass` gains `CONFLICT`, `DUPLICATE`, `REVIEW_REQUIRED` and `FATAL`, with `HTTP_STATUS` entries. `AUTO_RETRYABLE` is unchanged.
- Migration 0005 widens `ck_marketplace_capabilities_error_class_valid` to exactly the enum.
- Tests:
  - HTTP statuses pinned;
  - the four new classes never retry (ADR-0005 `DEAD` on first failure);
  - the migrated CHECK accepts every member and rejects a non-member.
- Repository consistency rules:
  - `ErrorClass` members == the `ARCHITECTURE.md` §8 list == this ADR's aligned column;
  - the `ERRORS.md` §1 list == `ErrorClass` minus `NOT_FOUND`;
  - no v3.1-only spelling (`` `RATE_LIMIT` ``, `` `POLICY` `` as a class token) in ICBM contracts outside a v3.1 quotation;
  - no `NotFoundError` raised under `integrations/`.

**Until Stage 1 merges:** no code emits the four new classes, and no adapter maps their conditions onto an older class (`5657655857` point 4). PR-A starts only after Stage 1.

## Consequences

- **ADR-0004 and ADR-0005 are unchanged.** The automatic-retry set is still `TRANSIENT` and `RATE_LIMITED`. ADR-0005's list of classes that go to `DEAD` on the first failure is extended by this ADR with the four new classes, without editing ADR-0005.
- **v3.1 §11.3's semantics are adopted;** its spelling is amended here.
- **`ERRORS.md` stays the SmartStore mapping,** and its §21 per-class convergence is consistent with the definitions above.
- **The batch/queue effects are a target** for the batch contract, not M2 behaviour.

## Rejected alternatives

- **"The v1 set is a subset of v3.1."** False (`5657668828`).
- **A2: adopt the v3.1 spellings, with legacy aliases.** Rejected under A.
- **Collapse the four v3.1-only classes into older ones** (resolution (a) of `5657613795`). Superseded by `5657655857`: that would silently lose v3.1 semantics.
- **Drop `NOT_FOUND` to match v3.1 exactly.** Historical rows would become unreadable, the capability reader would crash, and ICBM-local lookups would lose their 404 meaning.

## For architect confirmation

1. **Decision A:** the persisted spellings `RATE_LIMITED` and `POLICY_BLOCKED` are canonical, and the v3.1 names are aliases.
2. **HTTP statuses** for the four new classes: `CONFLICT` 409, `DUPLICATE` 409, `REVIEW_REQUIRED` 409, `FATAL` 500.
3. **`NOT_FOUND` scope** as in C, including `JOB_TYPE_UNKNOWN` staying `NOT_FOUND`.
4. **v3.1 provenance.** The frozen document is not in the repository, and the only available copy is marked `FREEZE CANDIDATE`. Please confirm that §11.3 as quoted is the frozen text, and whether the frozen v3.1 should be committed under `docs/`.
5. **The batch/queue column** is a target for the future batch contract, not M2 behaviour.
