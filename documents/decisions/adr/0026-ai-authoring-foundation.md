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

This section follows three sources:
- Issue #30 and its refinement `5661813529`;
- the AI / Prompt tab of the approved v29 prototype, as it already stands in Settings: the "AI Prompt Registry" in `ui/web/js/pages/settings/settings-schema.js`.

The stores follow that registry exactly. They add no role, policy or task the registry does not name.

The composition order is the one the registry's help states: 공통 규칙(Global) → 역할(Role) → 플랫폼 정책(Platform Policy) → 작업(Task) → 실행 데이터.

**The registry's entries are the store's keys.**

| Layer | Keys (v29 registry) |
| --- | --- |
| GLOBAL | 공통 규칙 |
| ROLE | 수집 검증 AI, 상품/MD AI, 커머스 운영 AI, CS 응대 AI, 판매상태 검증 AI |
| PLATFORM POLICY | 스마트스토어 정책, 쿠팡 정책, 11번가 정책 |
| TASK | 추출 보정 (수집관리), 통합 상품 추천 (통합DB), 카테고리 재추천 (등록관리), 주문 운영 보조 (주문관리), 문의 답변 (문의관리), 품절 판정 (품절확인) |

Canon requires independent tasks (Canonical §7.1, Issue #30 refinement). So the registry task `통합 상품 추천` holds one prompt per independent task: 상품명, 태그, 카테고리 and 옵션. These are Issue #30's `PROMPT_NAME`, `PROMPT_TAGS`, `PROMPT_CATEGORY` and `PROMPT_OPTIONS`, under the role 상품/MD AI.

Each registry task names the role it composes with. The foundation stores and edits every registry entry. A task runs only when its own stage lands (§1, ROADMAP §12).

**PromptTemplate store.**
- It holds the GLOBAL, ROLE and TASK layers above.
- Each key has append-only revisions and a current pointer. The current pointer moves only with `expected_current_revision`.
- Each revision writes one audit event, `AI_PROMPT_TEMPLATE_REVISED`. The event carries the key, the prior and new revision, the actor and the time, never the text.
- Each revision is content-addressed by a SHA-256 digest.
- **Seeds.** Seeds enter the store as explicit revision 1 rows of a seed version, from the v29 prompts. Issue #30's refinement keeps the v29 prompts as seeds.
  - A registry entry with no v29 prompt text is seeded empty and reads `미작성`. No text is invented for it.
  - Runtime never reads a prompt from code.
- This is the same pattern as the registration target policy: an append-only revision, a current table and an audited save.

**PlatformPolicy store.**
- It is separate from PromptTemplate, with its own keys, revisions, lifecycle and audit event `AI_PLATFORM_POLICY_REVISED` (Issue #30: no shared storage or version lifecycle).
- Its keys are the registry's three policies. Each holds its marketplace's AI-facing guidance, as the registry describes it:
  - 스마트스토어: 추천/제외 태그 ID · SEO · 공식 데이터 우선
  - 쿠팡: 카테고리 추천 → AI 재검증 · 필수 옵션 확인
  - 11번가: 실제 카테고리 ID 범위 · 등록 정책
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

### 3.1 Amendment (2026-10-08, AIF-1): the registry is the v29 prototype's own

The owner asked that the foundation be built as already planned. That plan is the layered prompt
system of the approved v29 prototype (`design/prototypes/icbm_redesign_test_v29_final.html`):
- `ICBM_GLOBAL_RULES_V2`
- `ROLE_REGISTRY`
- `PLATFORM_POLICY_REGISTRY`
- `TASK_REGISTRY`
- the `promptModal` editor and its `composePromptPreview`

The Settings card listed in §3 is one view of that system. This amendment supersedes three parts of
§3: the registry table, the split of `통합 상품 추천` into four prompts, and the seed and audit
wording.

**Entries.** The store keys are the prototype's ids (`app/capabilities/ai/registry.py`):

| Kind | Count | Notes |
| --- | --- | --- |
| Global rule set | 1 | `ICBM_GLOBAL_RULES_V2` |
| Roles | 6 | including `ROLE_SHOPPING_INSIGHT_V1` |
| Platform policies | 4 | including `POLICY_COMMON_MARKET_V1`, the policy of a task with no marketplace yet |
| Tasks | 15 | |

- Each task names the role it composes with, as the prototype's buttons pair them. Two tasks that
  no prototype button calls have no role.
- Settings shows the entries v29 shows there: the global rules, 5 roles, 3 platform policies and
  6 tasks.
- The AI 쇼핑 인사이트 header button opens its role and task. Each marketplace tab's `Policy 편집`
  button opens its policy.

**Tasks.** A task revision holds the prototype's four fields:
- `mode`, the task's name, which is never edited;
- `prompt`;
- `variables`, the input contract;
- `output`, the OUTPUT_SCHEMA.

**The bundle task.** `TASK_PRODUCT_RECOMMEND_BUNDLE_V1` stays one prompt. Its output schema already
returns `product_name`, `tags`, `category` and `options` as separate objects. AIF-3 records each
object as its own task state, so the four tasks share one request and keep independent states
(Canonical §7.1).

**Editing.** The editor has the prototype's seven tabs:
- 공통 규칙
- 역할
- 플랫폼 Policy
- 작업
- 출력 형식
- 입력 변수
- 조립 미리보기

Each tab saves or resets only its own field, as a new revision against the revision it was read
from. A reset appends a revision whose field equals the seed's. History is never deleted.

**Seeds.** Seeds are the prototype's texts, verbatim (`app/capabilities/ai/seed_v29.json`), as
revision 1 with `origin = SEED` and `seed_version = v29`.
- Migration 0053 only creates the stores, because every canonical table starts empty (M0
  acceptance).
- The application writes the seed when it starts on a database that lacks it, in one unit of work
  with one `AI_PROMPT_REGISTRY_SEEDED` audit record. A restart writes nothing.
- Runtime reads the stores, never the seed file.
- Every operator save (`OPERATOR`) and reset (`RESET`) is audited without its text.

**Composition.** The composition is the prototype's preview layout. The global rules come first,
then these sections in order:
- `ROLE_PROFILE`
- `PLATFORM_POLICY`
- `TASK_MODE`, followed by the task prompt
- `INPUT_VARIABLES`
- `OUTPUT_SCHEMA`
- `RUNTIME_DATA`

AIF-1 composes the saved layers with the runtime-data placeholder. AIF-2 fills that placeholder and
adds the platform limits a policy references.

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
- The tab keeps its v29 layout: the "AI 기본 설정" card, and the full-width "AI Prompt Registry" with its roles, platform policies and tasks.
- Each registry entry opens its current text and revision from the two stores, and saves with its expected revision. Each save shows the new revision.
- The "AI 기본 설정" toggles stay inert placeholders until their stage (`6054956408`). They are AI 상품명 추천, AI 태그 추천, AI 카테고리 검증 and 결과 미리보기 후 적용.
- There is no provider, model or key field until the owner decides them.

**AI status.** The `ai` capability (`NOT_CONFIGURED`) is shown where readiness is shown.

**Editor and register AI buttons.** They stay inert 준비 중 placeholders until their task's stage, because no task of a later stage is built here.

## 9. Implementation order

Each slice is provider-zero and its own PR, under the Track A loop.

1. **AIF-1 — Stores.**
   - PromptTemplate and PlatformPolicy: migration, store, the v29 registry's keys and seeds, audited revisioned save, read and save routes, the Settings registry screen;
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
