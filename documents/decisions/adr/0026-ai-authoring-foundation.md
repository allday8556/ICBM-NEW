# ADR-0026 — AI authoring foundation: the prompt and platform-policy stores, the provider port's execution, the structured enrichment result and the apply lock (provider-zero)

Status: **ACCEPTED** 2026-10-08. This is the contract for the first stage of the ROADMAP §12 registration/AI lane, the "AI authoring foundation". It lands before any of its code, and each implementation slice cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219.
  - The owner started this stage before the first vertical is accepted, provider-zero only, in parallel with M6.5 (`6057252039`).
  - The owner allowed inert 준비 중 AI placeholders (`6054956408`).
- **Already decided by canon:**
  - Canonical v3.1 §1, §3.1 and §7 (ENRICH);
  - ADR-0012 (the provider-neutral AI port, fingerprint and provenance);
  - ADR-0013 §8–§10 and ADR-0014 §18 (AI is optional and never satisfies required evidence);
  - ARCHITECTURE §4 "Registration authoring and AI boundary";
  - Issue #30 and its refinement `5661813529` (the PromptTemplate store and its layers);
  - Issue #127 (sequencing and UI semantics).
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14. This covers module placement, the data model, routes and the slice order.

What it authorizes:
- this contract;
- the ROADMAP §12 amendment recording `6057252039`;
- the provider-zero slices of §9, each its own PR.

What it does not authorize:
- **No real AI provider call.** No vendor adapter, SDK, sidecar, API key or egress grant exists until the owner decides:
  - the provider and model;
  - the credential;
  - the cost cap;
  - the transfer of product data to an external AI (rule 07 §7.2, ADR-0012 §13, ADR-0017 §9).
- **No AI_INITIAL automatic application.** Its timing stays deferred, as ARCHITECTURE §4 and ROADMAP §12 state.
- **No task of a later stage:** no SmartStore name or tag task, no SearchSignalAdapter, no Coupang or 11st, no bulk AI, no Shopping Insight.
- **No AI call inside COLLECT,** and no change to any source fact.
- **No new final-value owner.** The register Preparation stays the owner of the listing's outbound values (§7).

Sources:
- `documents/architecture/frozen/CANONICAL-V3.1.md` §1, §3.1, §5.4 and §7;
- `documents/architecture/ARCHITECTURE.md` §3 and §4;
- `documents/roadmap/ROADMAP.md` §7 and §12;
- ADR-0004, ADR-0007 §3, §6 and §7, ADR-0010 §13, ADR-0012, ADR-0013 §8–§10, ADR-0014 §4, §18 and §21, ADR-0017 §9;
- Issues #30, #56 and #127;
- rules 04, 05, 07 and 14.

Recorded by: Claude Code (Track A). The number was confirmed free on main and on every open branch.

Date: 2026-10-08

---

## 1. Scope

The foundation is the machinery every later AI task reuses. It has four parts, in the ROADMAP §12 order:

1. **Runtime prompt and platform-policy stores.** These are the PromptTemplate and PlatformPolicy runtime.
2. **The provider port's execution.** This covers the `AIProvider` port of ADR-0012, an enrichment job, and the execution provenance.
3. **The structured enrichment result.** It carries per-task independent status, evidence, confidence, the input fingerprint and staleness derived from it.
4. **The apply lock.** An AI value is applied to the register Preparation with optimistic concurrency, and a value the operator confirmed is never overwritten.

Everything runs and is proven with no AI provider. The production container has none, so the AI capability reads `NOT_CONFIGURED`. Tests inject a deterministic fake provider through the container, never through a store or a route.

## 2. Owners

| Owner | Holds | Never holds |
| --- | --- | --- |
| AI capability (`app/capabilities/ai/`) | the PromptTemplate and PlatformPolicy stores; the `AIProvider` port and its execution; `AIExecutionProvenance`; the `ai` capability readiness | a source fact, a product value, a final value, a provider SDK, a secret |
| PRODUCT DB (`app/stages/products/enrichment*`) | the enrichment request, the `EnrichmentResult` revisions per product, their fingerprints and derived staleness (ARCHITECTURE §3: PRODUCT DB owns enrichment proposals) | a final listing value, a marketplace call |
| REGISTER (`app/stages/register/`, existing) | the Preparation's outbound values and their provenance, including a value applied from an `EnrichmentResult` (§7) | an AI call |
| Settings (`ui/web/js/pages/settings/`) | the screen over the two stores (§8) | a hard-coded prompt or limit |

COLLECT never imports the AI capability or the enrichment module. A repository rule enforces this, and it extends the ADR-0010 §13 source-truth import rule.

## 3. PromptTemplate and PlatformPolicy stores

This section follows Issue #30 and its refinement `5661813529`. Settings help in the v29 prototype describes the composition order as 공통 규칙 → 역할 → 플랫폼 정책 → 작업 → 실행 데이터.

**PromptTemplate store.**
- It holds versioned prompt text in three layers:
  - `GLOBAL`: common rules;
  - `ROLE`, for example `ROLE_PRODUCT_MD_V1`;
  - `TASK`, for example `PROMPT_NAME_V1`, `PROMPT_TAGS_V1`, `PROMPT_CATEGORY_V1`, `PROMPT_OPTIONS_V1`.
- Each template key has append-only revisions plus a current pointer. The current pointer moves only with `expected_current_revision`.
- Each revision writes one audit event, `AI_PROMPT_TEMPLATE_REVISED`, carrying the key, the prior revision, the new revision, the actor and the time, and never the text.
- Each revision is content-addressed by a SHA-256 digest.
- Seeds enter the store as explicit revision 1 rows of a seed version, taken from the approved prototype's prompt registry. Runtime never reads a prompt from code.
- This is the same pattern as the registration target policy: an append-only revision, a current table and an audited save.

**PlatformPolicy store.**
- It is separate from PromptTemplate, with its own keys, revisions, lifecycle and audit event `AI_PLATFORM_POLICY_REVISED` (Issue #30: no shared storage or version lifecycle).
- A policy holds the marketplace's AI-facing guidance text per marketplace key.
- A numeric or rule limit that a canonical owner already holds is **referenced, never copied**. Examples are the listing-name length, the tag count and the prohibited-expression rules of the register preflight. At composition, the policy reads the current value from that owner, and the owner's revision becomes part of the policy's version identity.
- The deterministic prohibited-expression filter stays authoritative over any AI output (Issue #30 refinement).

**Composition.** One task's request is composed in this order:
1. GLOBAL
2. ROLE
3. the marketplace's PlatformPolicy, when the task has a target
4. TASK
5. execution data

- `prompt_version` is the tuple of every composed layer's revision. `policy_version` is the PlatformPolicy revision plus the revisions of the canonical owners it referenced.
- This resolves the ADR-0012 §6 note that prompt_version covers every composed layer.

## 4. The provider port and its execution

**The port.** This is the `AIProvider` port of ADR-0012 §1.
- The port takes one composed task request and returns a typed outcome plus `AIExecutionProvenance` (ADR-0012 §6).
- It exposes its `requested_identity()`:
  - `requested_provider`
  - `requested_model`
  - `routing_config_version`
  - `proxy_version`
- **Production:** the container binds `NoAIProvider`.
  - `requested_identity()` is absent.
  - The `ai` capability reads `NOT_CONFIGURED`. It appears in `degraded_capabilities` and never fails core readiness (ADR-0012 §9).
- The `AIProviderProfile` store of ADR-0012 §1 is deferred. It lands with the first adopted provider, because its endpoint and credential reference are owner decisions.

**The enrichment request.**
- An enrichment request names:
  - one canonical product;
  - its tasks;
  - an optional target `(marketplace_key, marketplace_account_id)` (GLOSSARY spelling).
- It is a job of the existing jobs capability (`enrich.tasks`), never inline. Collection never enqueues it.
- When the `ai` capability is not ready, the request is refused with `AI_PROVIDER_NOT_CONFIGURED`. No job is created and nothing is written.
- **Tasks share a request but not a state.** Compatible tasks may share one provider call, but each task gets its own status. A retry re-runs only the failed tasks (Canonical §7.1). "A normal product means one AI call" is not a contract.

**Errors and cost.**
- Provider errors map to ErrorClass as in ADR-0012 §8. Only TRANSIENT and RATE_LIMITED are retried automatically.
- Provenance records the billing mode as `METERED_API`, `SUBSCRIPTION` or `UNKNOWN`. Unknown cost is never zero cost (ADR-0012 §7).
- Provenance is copied from the port's outcome, never inferred or fabricated (ADR-0012 §6).

## 5. The structured enrichment result

**The result row.** One `EnrichmentResult` revision per task run, keyed by:
- the product;
- the task key;
- the target, which is `NULL` for a target-free task.

Each row carries:
- `status`: `OK` or `FAILED`;
- for `OK`:
  - `value`, as JSON, validated against the task's output schema;
  - `evidence`;
  - `confidence`, between 0 and 1;
  - `requires_review`;
- for `FAILED`: the classified error code;
- the input fingerprint (below), the composed `prompt_version` and `policy_version`, and `enrichment_schema_version`;
- the `AIExecutionProvenance`.

Results are append-only. The current result of a key is its newest revision. A value is never written into a ProductFacts revision or a product field, so AI never writes a source fact (Canonical §1, rule 05 §5.4).

**The fingerprint.**
- It is computed before the call:

  ```text
  hash(relevant_facts + policy_version + prompt_version + requested_provider + requested_model
       + routing_config_version + proxy_version + enrichment_schema_version)
  ```

- This is ADR-0012 §6. That later contract replaces the `model_id` of Canonical §7.2 and excludes `actual_model`.
- `relevant_facts` is the digest of exactly the fact fields the task declares it depends on, with their ProductFacts revision identities (dependency-scoped staleness, Issue #30 refinement).
- No secret is ever part of a fingerprint or provenance.

**Reuse and staleness.**
- A request whose fingerprint equals the current `OK` result's fingerprint makes no call and reuses that result (Canonical §7.2). The response says `REUSED`.
- `STALE` is derived on read, never stored. A result is stale when the fingerprint recomputed from today's inputs differs from its own.
- When an input change makes a result stale, only the tasks that depend on that input re-run (Canonical §7.3). A stale result stays readable with its reason, which names the changed input: facts, prompt, policy, provider or schema.

**Task schemas.**
- A task's output schema and its declared fact dependencies are code, versioned by `enrichment_schema_version`.
- This ADR fixes the envelope only. Each task's schema lands with its own stage, for example `recommended_name` with the SmartStore name stage.
- The foundation's tests use a test-only task definition.

## 6. AI never decides what canon gives to a deterministic owner

These rules are unchanged:
- **Category:** an AI category suggestion is never reviewed metadata (ADR-0015). Auto-apply needs an accepted, persisted CategoryMapping (ADR-0004).
- **Option combinability:** the combinability of options is decided deterministically, never by AI (Canonical §8.1, ADR-0014).
- **Prohibition:** prohibition is never decided by AI alone (Canonical §9.2).
- **Notices:** notice AI never invents a missing legal or source fact (Issue #127 §3).
- **Required fields:** an AI value never satisfies a required field. Only `SOURCE_FACT` and `OPERATOR_CONFIRMED` do, while `AI_SUGGESTION` raises `FIELD_AI_SUGGESTION_UNCONFIRMED` (ADR-0014 §4 and §18, `app/stages/register/policy.py`).

## 7. The apply lock and optimistic concurrency

This section follows Canonical §7.7, ARCHITECTURE §4 and Issue #127 §4.

**Where a value goes.** The listing's outbound values have exactly one owner, the register Preparation (ADR-0014 §27). An AI value reaches the listing only by an **apply** command on that owner.

**The apply command.** It names:
- the Preparation;
- the field;
- the `EnrichmentResult` revision;
- `expected_preparation_revision`.

It appends a new Preparation revision in which the field's value is the result's value with provenance `AI_SUGGESTION`. That value is AI_UNREVIEWED: visible, and never satisfying until the operator confirms it.

**The lock.** The unit is field × marketplace × account (Canonical §7.7, GLOSSARY `marketplace_account_id`). A Preparation already belongs to one marketplace account, so the lock is the field's current provenance in that Preparation:
- A value whose provenance is `OPERATOR_CONFIRMED` or `SOURCE_FACT` is locked. An apply onto it is refused with `AI_APPLY_FIELD_LOCKED`, and the value is never overwritten. The operator may still type a new value themselves.
- An `expected_preparation_revision` that is no longer current is refused with `AI_APPLY_REVISION_CHANGED` and skipped, never overwritten. A batch reports this as skipped-as-changed (Issue #127 §4).
- A `STALE` or `FAILED` result, or a result for another product, task or target, is refused.

The lock and the revision check are both kept, one against a locked value and one against a time-of-check/time-of-use race (Canonical §7.7).

**Not here.** The lock scope does not decide where results are cached; §5 keys the cache by product, task and target (ARCHITECTURE §4, Issue #30). A product-level final name across Preparations is not created here. If a later stage needs one, it is a superseding decision, because it would be a second owner of the listing name.

**Name collision.** A locked value whose source input changed is marked `AI_INPUT_DRIFT`, not `SOURCE_DRIFT`. `SOURCE_DRIFT` already means supplier-source drift in OPERATE (`operate.source_drift`).

## 8. Screens

**Settings › AI / Prompt.**
- The existing "AI Prompt Registry" reads the two stores and edits a template or a policy with its expected revision. Each save shows the new revision.
- The prompt toggles stay inert placeholders (`6054956408`).
- There is no provider, model or key field until the owner decides them.

**AI status.** The `ai` capability (`NOT_CONFIGURED`) is shown where readiness is shown.

**Editor and register AI buttons.** They stay inert 준비 중 placeholders until their task's stage, because no task of a later stage is built here.

## 9. Implementation order

Each slice is provider-zero and its own PR, under the Track A loop.

1. **AIF-1 — Stores.**
   - PromptTemplate and PlatformPolicy: migration, store, seeds, audited revisioned save, read and save routes, Settings screen;
   - the COLLECT import rule.
2. **AIF-2 — Port and execution.**
   - `AIProvider` port, `NoAIProvider`, the `ai` capability readiness;
   - composition of the layers and the referenced canonical limits;
   - the `enrich.tasks` job, `AIExecutionProvenance`, and the ErrorClass mapping;
   - a deterministic fake provider in tests only.
3. **AIF-3 — Results.**
   - the `EnrichmentResult` store;
   - fingerprint, reuse, derived staleness with its reason, dependency-scoped re-run;
   - request and read routes.
4. **AIF-4 — Apply lock.**
   - the apply command on the register Preparation with the lock and the expected revision.
   - It touches the register owner, so a handoff to Track B precedes it.

A real provider, the `AIProviderProfile` store, a vendor adapter, and the egress rule amendments a direct adapter would need are **not** in this order. They wait for the owner decisions listed above.

## 10. Acceptance

Proven offline with the fake provider:
- the stores revise and audit;
- a request composes every layer and records every revision;
- a task's failure leaves the other tasks `OK`;
- the same inputs reuse the result with no call;
- a changed prompt, policy, fact or provider identity makes exactly the dependent results stale;
- an apply writes `AI_SUGGESTION` and never satisfies a required field;
- a locked value or a changed revision is never overwritten.

With no provider:
- every request is refused;
- nothing is written;
- the manual registration path is unchanged.

## Invariants

```text
AIF-01  AI never writes a source fact or a product field; a result is a proposal row of its own
AIF-02  no AI call runs inside COLLECT, and COLLECT imports neither the AI capability nor enrichment
AIF-03  no prompt or limit is hard-coded at runtime: prompts come from PromptTemplate, limits from their canonical owner
AIF-04  PromptTemplate and PlatformPolicy are separate stores; every save is revisioned with an expected revision and audited without its text
AIF-05  each task has its own status; a retry re-runs only failed tasks
AIF-06  the fingerprint is ADR-0012 §6; equal fingerprint means reuse with no call; STALE is derived, never stored
AIF-07  provenance is copied from the provider outcome; unknown cost is never zero; no secret is in a fingerprint or provenance
AIF-08  with no provider, ai is NOT_CONFIGURED, core readiness is unaffected, and every request is refused before any write
AIF-09  an AI value reaches a listing only through the Preparation apply, as AI_SUGGESTION, which never satisfies a required field
AIF-10  an apply never overwrites an OPERATOR_CONFIRMED or SOURCE_FACT value, and never lands on a changed revision
AIF-11  no vendor SDK, adapter, credential or egress grant exists until the owner decides the provider, model, key, cost cap and data transfer
```
