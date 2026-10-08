# ADR-0027 — The SmartStore product-name AI stage and the CLIProxyAPI provider: profile, approved identity, data-transfer approval and the first live call

Status: **ACCEPTED** 2026-10-09. This is the contract for the second ROADMAP §12 registration/AI stage, "SmartStore single-product authoring AI", limited to its first item, `recommended_name`. It also covers the first real provider behind the ADR-0012 port. It lands before its code, and each implementation slice cites it as canon.

Decision owners:
- **Product direction:** the owner, in Issue #219 comment `6067968983`. The owner made four decisions:
  - start this stage now, extending the exception of `6057252039`;
  - connect the provider through the local CLIProxyAPI sidecar;
  - approve the binary on the operator's desktop, the transfer of product data through the operator's existing CLIProxyAPI logins, and subscription billing;
  - have Track A run the live test itself.
- **Model:** the owner, in Issue #219 comment `6068160917`, which supersedes `6068149898`. The default model is **GPT 5.6 sol** (`gpt-5.6-sol`), through the operator's Codex (ChatGPT) login. Changing it is the owner's decision.
- **Already decided by canon:**
  - ADR-0012: the provider-neutral port, the optional local sidecar, the approved executable identity, routing approval, provenance, billing, readiness and failure isolation;
  - ADR-0026: the foundation;
  - Canonical v3.1 §7.6: `recommended_name`, with AI_INITIAL deferred;
  - ARCHITECTURE §4.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

What it authorizes:
- this contract;
- the ROADMAP §12 amendment;
- the slices of §9;
- the live calls of §10, made through the approved profile only.

What it does not authorize:
- **ICBM never downloads, installs, updates, starts or stops the sidecar** in this stage. The sidecar is unmanaged: the operator, or Track A in the live test, starts it (ADR-0012 §2).
- **No other stage.** Tags, SearchSignal, category, options, notices, Coupang, 11st and bulk AI keep their order and their gate.
- **No AI_INITIAL automatic application.** A recommendation is shown and applied only by the operator, as `AI_SUGGESTION` (ADR-0026 §7).
- **No non-loopback AI endpoint, and no API key typed into ICBM** other than the sidecar's own client key.

Sources:
- ADR-0012 (§1–§13), ADR-0026, ADR-0007 §3 and §7;
- `documents/architecture/frozen/CANONICAL-V3.1.md` §7.1, §7.6 and §18;
- `documents/roadmap/ROADMAP.md` §12;
- Issues #8, #30 and #127;
- rules 05, 07 and 14.

Recorded by: Claude Code (Track A). The number was confirmed free on main and on every open branch.

Date: 2026-10-09

---

## 1. Scope

**The provider.** A `CLIPROXYAPI` provider profile and its adapter. The adapter is an OpenAI-compatible `POST /v1/chat/completions` on a loopback endpoint, behind the ADR-0012 checks:
- the approved executable identity of the process actually serving the endpoint;
- the approved routing identity;
- the credential in the OS secret store;
- the owner's data-transfer approval;
- a daily call cap.

**The task.** The first runnable task definition: the v29 bundle task `TASK_PRODUCT_RECOMMEND_BUNDLE_V1` with the single result key `product_name`. Its other result keys (tags, category, options) belong to later stages and are not recorded.

**The seed upgrade.** The bundle's v29 output schema gains the structured envelope (ADR-0026 §5) for `product_name`. This goes through a versioned seed upgrade that never overwrites an operator's edit (§6).

**The screens:**
- Settings › AI / Prompt › AI 공급자;
- 통합DB `✨ AI 추천`, live: it requests the run and shows the recommendation;
- the editor's 기본정보 step: it shows the recommendation, and applies it to the name as `AI_SUGGESTION`.

## 2. The provider profile

One profile store, `ai_provider_profiles`, with append-only revisions and a current pointer. Each revision holds:

| Field | Meaning |
| --- | --- |
| `provider_type` | `CLIPROXYAPI`; only an adopted adapter's type is accepted |
| `endpoint` | `http://127.0.0.1:<port>`, loopback only |
| `requested_model` | the model the profile asks for: `gpt-5.6-sol`, the owner's choice (`6068160917`). A profile revision that changes it needs the owner's decision, recorded with the revision |
| `credential_ref` | the OS secret-store name of the sidecar client key (ADR-0007 §3); the key never enters the database, a log, an audit record or a fingerprint |
| `approved_executable` | path, version and SHA-256 of the approved binary (ADR-0012 §3) |
| `approved_routing` | the routing identity: a SHA-256 of the sidecar's routing-relevant configuration with every secret removed, plus `-local-model` (§5) |
| `billing_mode` | `SUBSCRIPTION` for OAuth logins, as the owner stated; otherwise `UNKNOWN` |
| `data_transfer_approved` | the owner's approval that product facts may be sent through this profile (rule 07 §7.2) |
| `daily_call_cap` | the most calls one day may make; reaching it refuses further calls with `AI_DAILY_CAP_REACHED` |

**Who may change what.**
- Approving an executable identity or a routing identity, and approving data transfer, are **protected, audited actions** (ADR-0012 §3 and §5).
- Any revision that changes the endpoint, model, executable or routing makes earlier results stale through the fingerprint's requested identity (ADR-0012 §6, §10).

**The requested identity:**
- `requested_provider = cliproxyapi`;
- `requested_model`;
- `routing_config_version` = the approved routing identity;
- `proxy_version` = the approved binary version and SHA-256.

**Amendment (2026-10-09, AIS-1): the credential source.** ICBM never stores the sidecar's
client key: not in the database, not in the OS secret store, not in a log, audit record or
fingerprint.
- The profile's `credential_source` is `SIDECAR_CONFIG`. At call time, and only for the call, the key
  is read from the `api-keys` list of the approved sidecar's own configuration file. That file is
  found from the serving process's `-config` argument, or next to the approved executable.
- So the key exists in exactly one place, the operator's sidecar configuration.
- No one types it into ICBM, Track A included.
- `credential_ref` is replaced by this source, and AIS-01 reads: the only AI endpoint is loopback, and
  ICBM never persists the sidecar client key.

**Amendment (2026-10-09, AIS-1): the version, as applicable (ADR-0012 §2).** The CLIProxyAPI
Windows image carries no version resource, and ICBM never runs the binary to ask it (AIS-05).
- The approved executable identity is its path and SHA-256.
- `proxy_version` carries that SHA-256, so any change of the file is a new identity and a stale
  result.

## 3. Identity is verified on every call, fail-closed

Before each call the adapter reads the process that is **actually serving** the endpoint's port, and compares that process's executable path, version and SHA-256 with the approved identity (ADR-0012 §2).
- An open port or an answering endpoint is never proof.
- A mismatch puts the profile in `VERSION_MISMATCH` (`AI_EXECUTABLE_MISMATCH`) and refuses the call
  before anything is sent, as `POLICY_BLOCKED` (ADR-0012 §8).
- An identity that cannot be read is `UNAVAILABLE` and refused the same way: nothing serves the port
  (`AI_EXECUTABLE_NOT_SERVING`), or the serving sidecar's configuration cannot be read
  (`AI_ROUTING_UNREADABLE`).
- A routing identity that differs is `ROUTING_CONFIG_MISMATCH` (`AI_ROUTING_MISMATCH`), never
  collapsed into the binary mismatch (ADR-0012 §4).
- The probe is an injectable port. The Windows implementation reads the TCP table and the process image. On any other platform the identity is unverifiable, so no call is made there.

## 4. The call

The adapter sends the composed request as one user message, with `temperature = 0`. It asks for a JSON object only, and parses the first JSON object of the answer.
- **Provenance** (ADR-0012 §6):
  - `actual_model` is the model the response names;
  - tokens are the usage the response reports;
  - `billing_mode` is the profile's;
  - `vendor_cost` stays `null`, because unknown cost is never zero.
- **Errors** map to ErrorClass:
  - connection refused or a timeout → `TRANSIENT`;
  - HTTP 429 → `RATE_LIMITED`;
  - 401 or 403 → `AUTH`;
  - any other HTTP error → `UNKNOWN`;
  - a non-JSON answer → `VALIDATION`.
- **Egress.** The call is loopback only, so the egress guard needs no grant. The adapter is the one AI module allowed to open an HTTP client, and a repository rule pins it as a raw-client owner.

## 5. The sidecar's own updates

The operator's CLIProxyAPI fetches model catalogs and its management panel from GitHub by itself. That traffic is outside ICBM's egress guard (ADR-0012 §13). ADR-0012 §3 keeps automatic updates disabled by default, so:
- the approved routing identity records that the sidecar runs with `-local-model`, which uses its embedded model lists and skips the remote catalog;
- it also records `disable-auto-update-panel: true`;
- the Settings card shows whether the observed configuration matches.

ICBM never edits the sidecar's files. The operator, or Track A in the live test with the owner's approval of `6067968983`, sets them.

## 6. The product-name task and the seed upgrade

**The task definition.**
- Task: `TASK_PRODUCT_RECOMMEND_BUNDLE_V1`.
- Result: `product_name` with `required=("recommended",)`.
- Fact fields: `original_name`, `brand`, `manufacturer`, `origin`, `options`, `detail_description`.
- Schema version: `name-stage-1`.

The target-free subject is used, because the v29 bundle composes with the common market policy until a marketplace is chosen.

**The seed upgrade.** The v29 output schema of `product_name` lacks the envelope. A seed upgrade `v29-ai1` adds `"evidence":[]` and `"requires_review":false` to that object of the bundle's `output` field:
- On a fresh store it is seeded already upgraded, as revision 1 with `seed_version = v29-ai1`.
- On an existing store, it is applied only where the field still holds exactly the previous seed text. It is appended as a `RESET` revision by `system:seed`, and it is audited.
- An operator's edit is never overwritten. A result whose output then lacks the envelope is `FAILED` with `AI_OUTPUT_SCHEMA_INVALID`, and Settings shows why.
- The editor's 기본값 now means the newest seed. Reset restores it, and `modified_fields` compares against it.

## 7. Screens

**Settings › AI / Prompt › AI 공급자.**
- The profile form: endpoint, model, billing mode, daily cap. It has no key field (§2 amendment).
- The observed executable (path, SHA-256) beside the approved one, with an approve button. The same for the routing identity.
- The data-transfer approval.
- The state, in ADR-0012's vocabulary:
  - the runtime state: `NOT_CONFIGURED` until every approval exists, then `AVAILABLE`,
    `VERSION_MISMATCH`, `ROUTING_CONFIG_MISMATCH` or `UNAVAILABLE` (ADR-0012 §8);
  - the `ai` capability (ADR-0012 §9): `READY` only when the runtime is `AVAILABLE` and the cap is
    not reached; `DEGRADED` with the reason code otherwise.
- A reached daily cap is the profile's policy, not a sidecar state. The runtime stays `AVAILABLE`,
  the capability is `DEGRADED` with `AI_DAILY_CAP_REACHED`, and a call is `POLICY_BLOCKED`.

**통합DB detail, `✨ AI 추천`.** It is live when the `ai` capability is READY, and inert with its reason otherwise. It requests the product-name task, then shows:
- the recommendation;
- its confidence and requires-review state;
- whether it is stale;
- the model that answered.

**Editor › 기본정보.** It shows the product's current recommendation beside the name field. `AI 추천 적용` applies it as `AI_SUGGESTION` (ADR-0026 §7, naming the result revision). The operator's own edit or confirmation makes the value `OPERATOR_CONFIRMED` again.

## 8. Readiness

The `ai` capability reads the profile and the probe: no profile is `NOT_CONFIGURED`, and the other states are those of §7. It never fails core readiness (ADR-0012 §9).

## 9. Implementation order

Each slice is its own PR under the Track A loop.

1. **AIS-1 — Provider.**
   - The profile store and the probe port (with its Windows implementation).
   - The CLIProxyAPI adapter and the readiness states.
   - The Settings AI 공급자 card.
   - The approvals as audited actions; the daily cap.
   - Repository rules: the adapter is the only AI raw client, and no vendor SDK.
2. **AIS-2 — The product-name task.**
   - The seed upgrade `v29-ai1` and the task definition.
   - 통합DB `✨ AI 추천`, live.
   - The editor's recommendation and apply.
   - The live test of §10.

## 10. Acceptance and the live test

**Offline, with a fake sidecar process and a fake probe:**
- identity and routing mismatches refuse before sending;
- the cap refuses;
- provenance never fabricates a value;
- the seed upgrade never overwrites an edit;
- the result flows through to the apply.

**Live, by Track A with the owner's approval of `6067968983`:**
- The operator's approved binary serves on loopback with `-local-model`.
- One product of the local test data directory is run through the product-name task.
- The evidence report records:
  - the profile;
  - the observed and approved identity;
  - the request's revisions;
  - the recorded result;
  - the provenance;
  - no egress from ICBM beyond loopback.
- The report never records the key or the prompt text.

## Invariants

```text
AIS-01  the only AI endpoint is loopback; ICBM never persists the sidecar client key (read from the sidecar's config per call)
AIS-02  every call verifies the serving process against the approved executable identity, fail-closed
AIS-03  approving an executable, a routing identity or data transfer is a protected, audited action
AIS-04  no call is made without the owner's data-transfer approval on the profile, or past the daily cap
AIS-05  ICBM never downloads, installs, updates, starts or stops the sidecar in this stage
AIS-06  provenance is copied from the response; unknown cost is null, never 0
AIS-07  a seed upgrade never overwrites an operator's edit
AIS-08  a recommendation is shown and applied only by the operator, as AI_SUGGESTION; no AI_INITIAL
AIS-09  the requested model is the owner's choice (gpt-5.6-sol); changing it is the owner's decision
```
