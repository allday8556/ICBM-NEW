# ADR-0012 — AI runtime provider contract: provider-neutral profiles, an optional local sidecar, capability readiness and failure isolation

Status: **PROPOSED**. This is the contract PR for Issue #8. It becomes binding only when this line reads ACCEPTED, and that needs three things: the GPT audit, the independent Claude AI cross-audit and the user's explicit merge authorization. Nothing in this ADR authorizes an AI call, a sidecar download, start or stop, or a provider credential.
Decision owner: Architect (ChatGPT). Sources: the Issue #8 body, which is the contract agreed during architecture review, and the architect kickoff `5725353427` on Issue #8, which authorized this contract-only PR, fixed the number and corrected the supplier name.
Recorded by: Claude Code. The number was confirmed free in `docs/adr/` and in every open PR immediately before writing.
Date: 2026-09-18
Related:
- ADR-0004 and ADR-0005: only `TRANSIENT` and `RATE_LIMITED` retry automatically;
- ADR-0006: one owner per data directory;
- ADR-0007 §3 (secret boundary), §6 (capability readiness separated from core readiness), §7 (egress allow-boundary);
- ADR-0008: the `ErrorClass` taxonomy;
- ADR-0010 §13: COLLECT works with every AI capability unavailable.

---

## Context

M3 is accepted with **AI provider calls = 0** (`docs/acceptance/M3.md`), so source truth is proven independently of AI. That was Issue #8's precondition. M4 is CURRENT as a status pointer only. Enrichment will attach to the canonical `Product` there, and nothing in M4 is authorized by this ADR.

The canonical documents already place AI as a supporting capability, never a parallel system:
- `docs/ARCHITECTURE.md` §1 lists it among the supporting capabilities;
- §3 forbids AI writing ungrounded source facts;
- CLAUDE.md §5.4 says AI never fabricates source facts and ambiguous evidence becomes `REVIEW_REQUIRED`.

Issue #8 records the runtime contract agreed in architecture review. ICBM may use a local routing sidecar such as CLIProxyAPI but must never depend on one. Around that sit profiles, executable identity, routing identity, provenance, billing, readiness, and STALE handling after a routing change.

This ADR builds on mechanisms that already exist and does not replace any of them:
- **Capability readiness** (ADR-0007 §6; `app/system/readiness.py` `ReadinessReport`). A capability such as `supplier:kmretail` never fails core readiness: with a healthy core the report is `overall = READY` (HTTP 200) and lists every non-READY capability in `degraded_capabilities`. Only a core failure answers 503.
- **The process-local egress guard** (`app/core/egress.py`). It observes the ICBM interpreter's own socket audit events (PEP 578), blocks every non-loopback destination unless a scoped grant covers it (ADR-0007 §7), and never sees traffic from another process.
- **The OS secret store** (ADR-0007 §3). No plaintext secret at rest; no secret in the canonical DB, logs, audit or GitHub.
- **`ErrorClass`** (ADR-0008), and the rule that only `TRANSIENT` and `RATE_LIMITED` retry automatically.
- **Protected actions are audit logged** (`docs/ARCHITECTURE.md` §13).

Issue #8's older sequence names the supplier "K홀세일". The accepted supplier is KM통상 (`supplier_key` `kmretail`). That wording is stale only (kickoff `5725353427`).

## Decision

### 1. One provider-neutral port, configured by profile

**The port.** AI is reached through one port, `AIProvider`, and adapters implement it. The adapters Issue #8 names are illustrative, and none of them is mandatory:
- a local routing sidecar adapter, e.g. for CLIProxyAPI;
- official-API adapters, e.g. OpenAI, Anthropic, Gemini.

**The core principle.** ICBM may support a local routing sidecar, but it must not depend on one.
- No product or domain contract may assume that a particular provider or proxy exists, or that it is the only one.
- ICBM runs fully with **no** AI provider configured at all: core readiness READY, COLLECT, `ProductFactsRevision` and the canonical DB.

**Recommendation, not mandate.** Sidecar or OAuth-subscription routing may be used for development and experimentation. Production and business-critical AI should prefer official provider API credentials and contracts, unless the operator explicitly accepts another risk profile.

**`AIProviderProfile` is the unit** of configuration, approval, readiness and STALE scope. There is no global AI switch. At contract level a profile carries (names illustrative, schema not fixed here):

```text
AIProviderProfile
  profile_id                        # e.g. cliproxy-dev, openai-prod, anthropic-prod
  provider_type
  endpoint
  credential_ref                    # a reference to an OS secret-store record, never the secret

  approved executable identity      # sidecar profiles only: path, version, SHA-256 (§3)
  expected_proxy_version
  expected_binary_sha256

  observed_routing_config_version
  approved_routing_config_version

  status                            # the runtime state of §8
```

- Changing one profile never changes another's state or results. A change to `cliproxy-dev` does not invalidate `anthropic-prod` results.
- **No hidden prompt.** An adapter adds no hidden or provider-specific prompt text. What reaches a provider is the versioned prompt identified by `prompt_version` (§6) and the task input, nothing else. Where the operator-owned prompt text lives is Choice for review C.

### 2. An optional local sidecar: lifecycle, ownership and security

Preferred local flow:

```text
the operator starts ICBM
→ ICBM checks the approved local sidecar endpoint for a configured, approved profile
→ already running?
   ├─ yes: use it only if its identity is verified (Choice for review A), and do not claim process ownership
   └─ no:  if configured and approved, start the approved binary as a managed child
           → wait for verified readiness
           → the profile's capability becomes AVAILABLE
```

**Ownership is explicit.**
- ICBM owns a sidecar only when it started that process itself, as a managed child, and recorded that it did.
- An open port or an answering endpoint is **not** proof of ownership. "Port 8317 is open" proves nothing about who started the process.
- On shutdown ICBM may stop a sidecar it owns. It must **not** terminate a sidecar that was already running before it started.
- Only the process that owns the data directory (ADR-0006) manages a sidecar on its behalf.

**No automatic acquisition.** ICBM never downloads, installs or updates a sidecar binary on its own. It starts one only when that binary's approved identity (§3) and a configured, approved profile both exist.

**Local security defaults:**

```text
host                 loopback only: 127.0.0.1, ::1 or localhost
                     (0.0.0.0 is not loopback, as `is_loopback_host` already rules)
remote management    disabled unless explicitly required and approved
local API credential ICBM-dedicated, stored in the OS secret store
                     (never in the canonical DB, logs, audit or GitHub)
```

**Implementation-time evidence, not decided here.** These must be measured before implementation and must not be invented:
- the executable invocation and its configuration flags;
- readiness behaviour (**no generic `/health` contract is assumed**);
- the model-list and request endpoints;
- Windows packaging;
- release and update behaviour;
- how the effective routing semantics can be observed (§4).

### 3. The executable identity is the protected approval target

The approval target is the **binary identity**, not each launch. An operator approves one identity once:

```text
path
version
SHA-256
```

- **Within an approved identity**, automatic start and stop may happen without a prompt at each launch. Every launch is audited with the path, version and SHA-256 that ran.
- **Re-approval is required** whenever the identity changes:
  - the executable path changed;
  - the binary SHA-256 changed;
  - the version is not approved;
  - an automatic update replaced the file.
- **Automatic core updates are disabled by default.** The path is: pinned tested version → new release detected → compatibility verification → explicit approval and update.
- **Approving an identity is a protected, audited action.** A mismatch puts the profile in `VERSION_MISMATCH` (§8).

### 4. Routing identity is separate from binary identity

These two failures stay distinct and are never collapsed into one mismatch state:

```text
VERSION_MISMATCH
→ the binary/version identity differs from the approved expectation
→ typical operator action: install or restore the approved binary

ROUTING_CONFIG_MISMATCH
→ the binary is approved, but the effective routing semantics differ from the approved routing identity
→ typical operator action: review, restore or approve the routing configuration
```

**What `routing_config_version` is.** It represents the **effective, non-secret routing semantics that ICBM observes**, never a manually typed version string. Out-of-band configuration changes must be detectable.

A raw configuration-file SHA-256 may serve as an initial implementation, but only if it faithfully captures every effective routing input. The long-term contract is a provider-neutral effective routing identity.

**What it covers.** Where observable, it includes every non-secret semantic that can change model selection or output behaviour:

```text
provider/model pinning
model aliases
routing strategy
credential priority / weights
excluded models
fallback / retry routing policy
other non-secret model-selection semantics
```

**No secret credential material is ever part of this fingerprint.**

**Development sidecar profile defaults** (deterministic routing):

```text
provider/model pinning        REQUIRED
model alias                   DISABLED unless explicitly approved
automatic provider fallback   DISABLED unless explicitly approved
automatic core update         DISABLED
proxy version                 PINNED
binary SHA-256                VERIFIED
loopback only                 REQUIRED
```

If later measurements justify aliases or fallback, the contract is extended explicitly. They are never enabled silently.

### 5. A routing change is approved, never trusted

```text
out-of-band routing change detected
→ profile status = ROUTING_CONFIG_MISMATCH
→ new enrichment for that profile is blocked
→ a safe change and impact preview is shown
→ the operator explicitly approves the current routing identity
→ the new approved_routing_config_version becomes active
```

- **Approving a routing identity is a protected, audited action**, and it spends no AI budget.
- It records at least:
  - `previous_routing_config_version`, `observed_routing_config_version`, `approved_routing_config_version`;
  - `approved_at`, `approved_by`;
  - `proxy_version`, `proxy_binary_sha256`.
- The preview shows non-secret semantic differences where possible, and never a secret credential value.
- The UI offers explicit actions equivalent to `[변경 내용 확인] [현재 라우팅 설정 승인]`. The layout is not fixed here.

### 6. The pre-call fingerprint and the post-call provenance are different things

**Pre-call cache fingerprint.** It must be computable **before** the call:

```text
enrich_input_fingerprint = hash(
    relevant_facts
  + policy_version
  + prompt_version
  + requested_provider
  + requested_model
  + routing_config_version
  + proxy_version
  + enrichment_schema_version
)
```

`actual_model` is **not** part of it, because it is unknown until the call has run.

**Post-call execution provenance.** It is recorded separately:

```text
AIExecutionProvenance
  requested_provider
  requested_model

  actual_provider
  actual_model

  proxy_name
  proxy_version
  proxy_binary_sha256
  routing_config_version

  alias_applied
  fallback_applied

  billing_mode
  tokens_in
  tokens_out
  vendor_cost
  estimated_cost
  allocated_cost

  latency_ms
```

When the actual provider or model cannot be determined reliably, it is recorded as `UNKNOWN` or null. **Provenance is never inferred or fabricated.** Where these records are stored belongs to the enrichment implementation (M4 onward), not to this ADR.

### 7. Unknown cost is not zero cost

```text
billing_mode = METERED_API | SUBSCRIPTION | UNKNOWN
```

- **Metered official API:** `vendor_cost` is the measured provider cost.
- **Subscription or OAuth runtime:** `billing_mode = SUBSCRIPTION` and `vendor_cost = null`.
- **Not measured:** `vendor_cost` stays null. It is never reported as 0.
- **Other measures:** tokens, calls and latency are measured where available. What is not measured is null, never 0.
- **`allocated_cost`:** an optional internal allocation. It is never presented as vendor-billed cost.

### 8. Runtime state and task `ErrorClass` are two different things

The runtime state is a profile's persistent, current state. `ErrorClass` (ADR-0008) describes one task's failure event. The two enums stay separate. Runtime states:

```text
AVAILABLE
DEGRADED
QUOTA_LIMITED
AUTH_EXPIRED
VERSION_MISMATCH
ROUTING_CONFIG_MISMATCH
UNAVAILABLE
```

How a task failure caused by each state is classified:

| runtime state | task `ErrorClass` | retry |
| --- | --- | --- |
| `AUTH_EXPIRED` | `AUTH` | no blind retry; operator login or intervention |
| `QUOTA_LIMITED` | `RATE_LIMITED` | bounded backoff and retry where policy allows |
| `VERSION_MISMATCH` | `POLICY_BLOCKED` | none until the approved runtime is restored |
| `ROUTING_CONFIG_MISMATCH` | `POLICY_BLOCKED` | none until the routing is reviewed and approved |
| `UNAVAILABLE`, `DEGRADED` | the actual cause of the event (`TRANSIENT`, `AUTH`, …) | as that class allows; no fixed class is assumed (Choice for review D) |

Only `TRANSIENT` and `RATE_LIMITED` are ever retried automatically (ADR-0004, ADR-0005). This ADR does not change that.

### 9. AI is a capability, never core readiness

This is the existing pattern of ADR-0007 §6, applied per profile:
- A core failure answers **503**.
- With a healthy core, a profile that is not `AVAILABLE` answers **200**, with `overall = READY` and the profile's capability, e.g. `ai:<profile_id>`, listed in `degraded_capabilities`.
- **A capability failure never fails core readiness**, unless that capability is required for core process safety. No AI profile is.
- A sidecar failure never stops COLLECT, `ProductFactsRevision` or the canonical DB (§13).

Capability readiness is a general ICBM pattern, not an AI-specific mechanism.

Readiness payload, for illustration:

```text
GET /api/ready

overall = READY
core:           schema, data_dir_owner, secret_store, execution_mode, egress_guard: READY
capabilities:   ai:cliproxy-dev → not ready (runtime state VERSION_MISMATCH)
degraded_capabilities: [ai:cliproxy-dev]
```

A provider-neutral AI runtime status endpoint may be added, e.g. `GET /api/v1/ai/runtime/status`. It exposes safe diagnostics only: expected and actual version, binary SHA identity, routing identity, and managed or unmanaged process status. It exposes no secret. How the runtime-state vocabulary is carried in the readiness payload is Choice for review B.

### 10. Routing approval marks results STALE; it never enqueues reprocessing

There are two separate decisions:
1. **Routing approval** is a data-integrity and trust declaration. It spends no AI budget.
2. **Bulk re-enrichment** is a cost and time decision. It needs its own preflight and an explicit action.

```text
routing mismatch
→ Impact Preview
→ [approve routing]
→ affected prior results become STALE
→ no automatic mass enqueue
→ separate Re-enrichment Preflight
→ choose scope / priority
→ [execute reprocessing]
→ bounded queue
```

> **Approving a new routing identity marks affected prior enrichment results STALE but must not automatically enqueue mass reprocessing. ICBM must first present an impact/preflight summary, and bulk re-enrichment is a separate explicit action executed with bounded scheduling.** (Issue #8 §11)

### 11. The conservative, profile-scoped STALE policy (v1)

```text
routing_config_version changes for profile P
→ every prior enrichment result produced under P and the old routing identity becomes STALE
```

- **It is profile-scoped, not global.** If `cliproxy-dev` changes, `cliproxy-dev` results become STALE; if `anthropic-prod` is unchanged, its results stay valid.
- **No task-specific impact is inferred** from a proxy's internals.
- **Impact Preview keeps two scopes apart:**
  - STALE scope, which is a data-integrity state;
  - execution scope, which is what the operator chooses to recompute now.
- **A possible execution priority** (illustrative):
  - P1: registration-ready or about-to-register products;
  - P2: products on sale that need a fresh AI result;
  - P3: products being actively edited;
  - P4: remaining DB products.
- **User or manual locked values stay protected** and are never overwritten.

> **A change to an approved `routing_config_version` invalidates all prior enrichment results produced under the affected AIProviderProfile and prior routing identity. ICBM does not initially attempt task-level routing-impact inference.** (Issue #8 §13)

### 12. Task-scoped routing invalidation is deferred

Invalidating per task (category, title, tag, option routing identities) is **not** part of v1. It is not implemented by parsing any proxy's configuration semantics, because that would couple the provider-neutral contract to one proxy.

These measurements come first:

```text
routing_change_detected
stale_products_count
stale_results_count
reprocessed_task_count
skipped_stale_count
locked_result_count
reprocess_latency
reprocess_tokens
reprocess_cost
```

Only production data that justifies the added complexity can motivate a provider-neutral task-routing identity extension, and it would come by a later, explicit contract change.

> **STALE scope and execution scope are separate. Routing approval may mark results STALE, but bulk re-enrichment requires a separate preflight and explicit execution decision. Task-scoped routing invalidation is deferred until production measurements justify the added provider coupling.** (Issue #8 §13)

### 13. Egress: a sidecar is never treated as covered by ICBM's guard

```text
ICBM → local sidecar           loopback
local sidecar → AI provider    external Internet egress, from another process
```

**What the guard can see.** The egress guard observes only the ICBM interpreter. It allows loopback without a grant, so it records the ICBM → sidecar call as local, and it cannot see the sidecar's own downstream traffic at all.

**What follows:**
- **Zero guard counts are never evidence** that no AI-provider traffic happened while a sidecar was in use. A sidecar is never described as governed by the guard, and it must not become an invisible bypass around egress policy.
- **The AI runtime's own policy and audit must account explicitly** for:
  - the approved local proxy use;
  - the downstream provider access it implies;
  - timeout and retry;
  - observability.

  A profile states its downstream provider(s) as non-secret metadata, and status and diagnostics show that sidecar egress lies outside ICBM's guard.
- **An adapter that calls a provider directly from ICBM** does so only inside an explicit, scoped egress grant for that profile's declared hosts, as the supplier transport does (ADR-0007 §7). The guard is never disabled.

### 14. AI failure never blocks the source-truth path

If the AI runtime is unavailable or policy-blocked:

```text
COLLECT                  ✅
ProductFactsRevision     ✅
canonical DB             ✅
AI enrichment            ⏸ / DEGRADED
```

- **AI never writes, rewrites or blocks source facts.** Its output is enrichment, kept apart from source facts and evidence (CLAUDE.md §5.4, `docs/ARCHITECTURE.md` §3).
- **An AI task is never part of a collection run.** A failed or paused AI task never changes a run's outcome or a revision.
- **ADR-0010 §13 stays unchanged**, including its repository rule that the source-truth path imports no AI, OCR or vision library and no `ai` package.

### 15. What this ADR does not decide

This ADR is a contract only. It fixes none of these, and each belongs to a later, separately authorized implementation:
- schema, migrations, table or column names;
- endpoint paths beyond the illustrative ones above;
- UI layout;
- the concrete provider list;
- sidecar invocation;
- the enrichment tasks themselves;
- the M4 `Product`.

The PR that records this ADR makes no AI call, handles no provider credential, downloads, starts or stops no sidecar, and changes no application code, schema or UI.

## Choices for review

- **A. Using an unmanaged sidecar.**
  - The gap: Issue #8 says a sidecar that was already running is used "but do not claim process ownership". It does not say whether its identity is checked.
  - This ADR's choice: a pre-existing sidecar is used only when the identity checks a managed launch needs (§3: approved path, version and SHA-256 of the executable actually serving the endpoint) and the routing check (§4) both succeed. Otherwise the profile is `VERSION_MISMATCH` or `UNAVAILABLE`, whichever cause applies, and is not used.
  - The alternative: trust any loopback answer. That would make §3 decorative.
- **B. The runtime state in readiness.**
  - The gap: the existing `CapabilityReport.status` vocabulary (`READY`, `NOT_CONFIGURED`, `DISCONNECTED`, `CONNECTING`, `AUTH_EXPIRED`, `DEGRADED`, `PAUSED`) was built for supplier connections. It has no `VERSION_MISMATCH`, `ROUTING_CONFIG_MISMATCH` or `QUOTA_LIMITED`.
  - This ADR's choice: leave the representation to implementation, under one invariant: every profile that is not `AVAILABLE` appears in `degraded_capabilities`, and its runtime state is visible without a secret. That can mean extending the capability status vocabulary or carrying the runtime state beside the status.
  - Related open point: whether an ICBM with no profile configured lists an `ai:` capability at all.
- **C. Where the prompt text comes from.**
  - The user's recorded instruction: the prompts for each AI item are already saved in the Settings (설정) tab's AI registry, and AI must use them, not newly authored prompts.
  - This ADR's choice: bind only what is contract-level. `prompt_version` identifies the exact operator-owned prompt text sent, and adapters add no hidden or provider-specific prompt (§1).
  - Still open: the storage location of that saved text has to be confirmed with the user when the implementation starts. The legacy repository is not a source.
- **D. `DEGRADED`.**
  - The gap: Issue #8 lists `DEGRADED` as a runtime state but gives it no mapping.
  - This ADR's choice: classify it like `UNAVAILABLE`, by the actual cause of each event, with no fixed `ErrorClass`.

## Consequences

- **Implementation needs its own authorization and evidence.** Each implementation PR after acceptance must bring the evidence §2 names, measured rather than assumed: invocation, readiness behaviour, endpoints, Windows packaging, release behaviour, routing observability.
- **Enrichment attaches to this port.** M4 enrichment can depend only on `AIProvider` and `AIProviderProfile` semantics, never on one adapter.
- **Possible future repository rules.** They could pin the port boundary, e.g.:
  - domain and service code imports no provider SDK outside the adapters;
  - no secret-bearing field enters a routing fingerprint or provenance record;
  - the ADR-0010 §13 source-truth isolation rule, which already exists, is unchanged.
- **Nothing changes in the application yet.** Readiness, the egress guard, the secret store, `ErrorClass` and the job states stay exactly as they are until an implementation PR is authorized.

## References

- Issue #8 (body; architect kickoff `5725353427`)
- `docs/acceptance/M3.md` (AI provider calls = 0; bounded by §2)
- ADR-0004, ADR-0005, ADR-0006, ADR-0007 (§3, §6, §7), ADR-0008, ADR-0010 (§13)
- `app/system/readiness.py` (`ReadinessReport`: `overall`, `capabilities`, `degraded_capabilities`), `app/connect/contracts.py` (`CapabilityReport`), `app/connect/state.py` (`CapabilityStatus`), `app/core/egress.py` (the process-local guard), `app/core/net.py` (`is_loopback_host`), `app/core/errors.py` (`ErrorClass`)
