# Agent Host Audit and Merge Protocol

Status: **V2 — canonical candidate (PR #147).** It is runtime-authoritative for the Agent Host from the
main commit that contains this file. Until then, the #146 trial instruction (`5870526033`) cites
the pre-refresh text at `9cc4939a` as its protocol source.
Date: 2026-09-28 (first text); refreshed on clean main `b1b5175774159989bcf2ea2ef0caed45a018641b`
Scope: ICBM-NEW Agent Host audit, CI, merge, and post-merge verification workflow.

This document records the operating protocol agreed after PR #146 exposed three distinct failure
classes: stale audit input/spec conflict, CI environment/timing failure, and a real code blocker.
The protocol preserves fail-closed behavior while reducing duplicate rules and duplicate work.

## 0. V2 boundary

This protocol is the Agent Host's **control plane**: audit input, audit results, CI state, merge
and post-merge verification. Two hard boundaries hold.

**Pre-V2 Host control-plane rules are authoritative only where this document re-adopts them.**
A rule, convention or behavior of an earlier Host version is not runtime-authoritative merely
because it exists. This covers:
- orchestrator, audit, repair or lookahead scripts before V2 (`v1.x`);
- their state files and registries;
- earlier process comments.

Explicitly re-adopted:
- the audit-before-FULL-CI order of Issue #143 `5868192403` (§1, §6);
- the CI scope policy of `.github/workflows/ci.yml` (Issue #143, PR #144): draft runs are
  lightweight and a ready PR runs the full suite (§6).

Any other pre-V2 Host behavior must conform to this document, or it is not used. Examples:
- work selection;
- remediation or auto-next PR creation;
- the open-PR duplicate guard;
- cached state.

**Product, domain and safety contracts are not changed by this protocol.** It does not amend,
supersede or relax:
- any ADR;
- `CLAUDE.md`, `ROADMAP.md` or `docs/ARCHITECTURE.md`;
- any marketplace safety contract, among them:
  - ADR-0014, including the never-resend and positive-only reconcile rules of §28;
  - ADR-0018, including G3-30 and G3-31;
  - ADR-0020;
  - the execution-safety rules of `CLAUDE.md` §7.

Those stay binding until their own ADR or canonical process supersedes them.

A DUAL PASS, a GREEN FULL CI and a passed MERGE_GUARD authorize a merge only. Whether a slice may
adopt an endpoint in code or change a schema is decided by the canonical contracts (ADR-0020), not
by this protocol. The protocol never authorizes:
- a provider call, LIVE or a canary;
- the residual-risk acceptance;
- any action `CLAUDE.md` §7.2 reserves for the user, such as a force-push, a branch deletion or a
  destructive operation.

## 1. Flow

```text
DRAFT implementation
→ candidate HEAD fixed
→ Audit Packet generated and completeness-checked
→ GPT exact-head audit
→ independent Claude cross-audit
→ same-identity DUAL PASS
→ READY
→ FULL CI
→ GREEN
→ pre-merge packet regeneration and authority re-scan (§7.1)
→ MERGE_GUARD
→ merge with expected_head_sha
→ POST_MERGE_VERIFY
```

No implementation change is made while an audit result is merely a specification/evidence/authority
conflict. Resolve the audit input first.

## 2. Identity

Two identities are used because audit validity and CI validity do not have the same inputs.

```text
Audit identity = (exact HEAD, audit_packet_digest)
CI identity    = exact HEAD
```

Rules:

- HEAD changes → prior audit results and prior CI results are invalid for the new HEAD.
- HEAD unchanged but packet digest changes → prior audit results are invalid; CI for that HEAD
  remains valid.
- HEAD and packet digest unchanged → an eligible cached PASS may be reused.
- READY is meaningful only for the current HEAD and current accepted audit identity.

## 3. Authoritative-source markers

Authority is declared when a durable GitHub source is written; it is not inferred later from the
GitHub account that posted it.

The recognized markers are:

```text
[ARCHITECT-INSTRUCTION]
[EVIDENCE-PACKET]
[OWNER-AMENDMENT]
```

Only marked sources participate in automatic authoritative-source discovery. Unmarked ordinary PR
discussion, implementation summaries, audit results, and status comments do not become authority
merely because the repository owner posted them.

A marked authoritative source discovered after the previous source watermark and not classified in
the manifest prevents a clean packet from being issued until it is classified. The generator does
not guess whether it should be ignored.

## 4. Audit Packet

The Audit Packet is a **generated artifact, never a hand-edited audit record**.

Its inputs are **content-bound source identities**:

- exact HEAD SHA;
- audited base SHA;
- slice specification path and blob SHA;
- owner amendments;
- architect instructions;
- evidence packets;
- scope allow-list source;
- binding prior decisions required by the slice;
- authoritative-source manifest and source watermark.

The last five inputs are each identified as below.

**Content-bound identity.** A git object (a commit SHA, or a path with its blob SHA) already names
its content. A GitHub comment ID, review ID or issue/PR number does not. It is a stable locator
whose body can be edited in place, so it never identifies content alone.

Every mutable GitHub source therefore enters the manifest as:
- its kind and locator (for example the comment ID);
- its **canonical body digest**: SHA-256 of the body exactly as the GitHub API returns it, UTF-8,
  with line endings normalized to LF;
- its `updated_at`, recorded as provenance only, never as identity. It sits outside the canonical
  packet bytes, so it never affects `audit_packet_digest` (§4.2).

The packet embeds the body digest, so an edited body changes the packet bytes and the
`audit_packet_digest`.

The packet contains a manifest of every required source and the exact content-bound identities
used to build it.

### 4.1 Completeness

Hard completeness checks:

- every source declared `required` by the manifest is present in the packet;
- every recognized authoritative marker discovered since the source watermark is classified before
  the packet is accepted;
- every source is re-read at generation time. A git source resolves to its exact object. A
  mutable GitHub source must be readable, and its current canonical body digest is what the packet
  records. A source that cannot be read is HOLD;
- packet HEAD and base match the candidate being audited.

Failure of a hard completeness check is **HOLD**, not a code BLOCKER.

### 4.2 Reproducibility

Packet generation must be deterministic.

```text
same content-bound source set + same HEAD + same base
→ byte-identical canonical packet
→ same audit_packet_digest
```

Conversely, any change to a source body changes that source's body digest, and with it the packet
bytes and `audit_packet_digest`, even when HEAD, base and every locator are unchanged.

Timestamps, random IDs, map iteration order, platform-specific line endings, or other
non-deterministic values must not affect the digest.

The reproducibility contract is test-pinned.

## 5. Audit

GPT and Claude audit the same `(HEAD, audit_packet_digest)`.

Claude audit execution should use a different execution/session identity from the implementation
run when the Host can enforce that distinction. Semantic independence beyond the identities the
Host can observe remains an operating discipline, not a claim of machine-enforced proof.

Every audit result records:

- exact HEAD;
- audit_packet_digest;
- verdict;
- evidence_seen: the content-bound source identities the auditor consumed (for a mutable GitHub
  source, its locator **and** body digest, never the ID alone);
- blocker/hold reason when not PASS.

Before DUAL PASS is accepted:

```text
GPT.evidence_seen    ⊇ packet.manifest.required
Claude.evidence_seen ⊇ packet.manifest.required
```

Coverage compares content-bound identities. An entry whose body digest differs from the manifest's
does not cover that source.

### 5.1 Verdicts

Only three control-flow verdicts exist:

- **PASS** — audit satisfied for this audit identity.
- **BLOCKER** — code or contract defect that can be fixed by changing the implementation.
- **HOLD** — human decision, missing/ambiguous authority or evidence, permission refusal, or another
  condition that automation must not resolve by editing code.

Only a CODE/CONTRACT BLOCKER consumes the automatic-fix counter.

A repeated materially identical BLOCKER after an attempted fix escalates to HOLD; the Host must not
loop on the same misunderstanding.

### 5.2 Audit cache

- PASS may be cached only under the exact `(HEAD, audit_packet_digest)`.
- BLOCKER and HOLD are not reusable audit cache entries; a later audit is fresh.
- A packet-digest change invalidates audit PASS without invalidating same-HEAD CI.

The Host never imports an external audit as though the Host performed it. External audit evidence
may be an authoritative input when explicitly classified, but authorship/provenance is preserved.

## 6. CI

DRAFT runs only the lightweight path required by `.github/workflows/ci.yml`.

FULL CI is requested only after DUAL PASS and READY for the current audit identity.

The PR stays draft until then. Under `.github/workflows/ci.yml`, marking it ready for review is the
FULL CI request for that exact HEAD. A draft run has no `CI gate` result at all.

For a ready PR that touches only Markdown under `docs/evidence/`, the workflow runs its lighter docs
scope, and that run's `CI gate` is judged by the same GREEN rule below. This protocol does not widen
or narrow the workflow's scope classification.

A FULL CI result is GREEN only when:

```text
failure count == 0
AND at least one CI gate == success
```

`skipped` is neutral. A lightweight DRAFT run with a skipped CI gate is never FULL-CI GREEN.

Same-HEAD duplicate FULL CI runs are cancelled/excluded when an already-valid same-HEAD FULL CI
GREEN exists. Cancelled duplicate run IDs are recorded.

### 6.1 Failure and retry

A CI failure starts as **FAILURE_UNCLASSIFIED**.

One same-HEAD failed-job retry may be used to gather evidence only when there is concrete
environmental/timing evidence, such as:

- runner/network/install layer failure;
- timeout/resource-starvation evidence that is not itself a demonstrated application assertion or
  deterministic logic failure;
- prior same-HEAD success combined with an environment-shaped current failure.

Prior success alone is not sufficient evidence of INFRA_FAILURE.

Retry permission is not an INFRA verdict. If the failure is reproducible in code/tests, it is a
BLOCKER. A repeated unresolved environment-shaped failure becomes HOLD rather than an unlimited
rerun loop.

## 7. MERGE_GUARD

Merge is allowed only when all of the following are true:

1. PR HEAD == GPT HEAD == Claude HEAD == FULL CI HEAD.
2. GPT packet digest == Claude packet digest == the digest of the packet **regenerated at merge
   time** (§7.1). A previously generated packet is never treated as current.
3. Packet completeness/reproducibility checks PASS on the regenerated packet.
4. GPT and Claude `evidence_seen` each cover the regenerated `packet.manifest.required`, by
   content-bound identity (§4).
5. FULL CI is GREEN for the exact HEAD.
6. No HOLD is active.
7. `audited_base_sha == current_base_sha`.

Merge must use `expected_head_sha` so a moved PR HEAD cannot be merged accidentally.

### 7.1 Pre-merge packet regeneration and authority re-scan

Immediately before MERGE_GUARD is evaluated, the Host regenerates the Audit Packet for the same HEAD
and base. It re-scans the authoritative sources from the source watermark and re-reads every
content-bound source (§4).

- **Unchanged:** the regenerated canonical bytes, `audit_packet_digest` and manifest equal the
  audited packet. Only then may MERGE_GUARD proceed.
- **Unclassified marker:** a marked source discovered after the watermark and not classified in
  the manifest is **HOLD** (§3).
- **Any other difference** invalidates the DUAL PASS. The Host returns to audit under the new
  audit identity. Examples:
  - a changed body digest;
  - a new classified source;
  - a changed manifest, bytes or digest.

  The same-HEAD FULL CI stays valid (§2).
- **A source that cannot be re-read** is HOLD, for example one that was deleted or is
  inaccessible.

MERGE_GUARD does not proceed on the packet generated before the audit.

Scope violations are expected to be caught during audit and packet completeness rather than
duplicated as a separate merge-time interpretation rule.

## 8. POST_MERGE_VERIFY

There is no automatic second audit merely because the merge completed.

The Host verifies the merge result:

- the PR is reported merged;
- main contains the expected merged result;
- **merged main tree == audited HEAD tree**.

With the audited-base equality guard, the tree equality is the cheap final proof that the result
placed on main is the tree that was audited, regardless of merge/squash commit metadata.

Tree mismatch is HOLD and requires investigation; it is not silently accepted.

**A merge moves main, so it changes the accepted code SHA.** Every exact-main proof bound to the
previous main is stale for the new one, among them the Gate 3 visual acceptance (ADR-0018 G3-31).
POST_MERGE_VERIFY never reports such a proof as current for the merged main. It is re-established
only through its own reviewed path.

## 9. Rollout order

The protocol is implemented in three slices:

### PR-A — Audit Packet foundation

Must land as one safe unit:

- generated packet over content-bound source identities (git objects; mutable GitHub sources by
  locator + canonical body digest);
- authoritative-source markers;
- source manifest and watermark;
- hard completeness detection;
- deterministic canonical serialization and reproducible digest;
- audit identity `(HEAD, packet_digest)`;
- evidence_seen recording and coverage checks by content-bound identity.

Do not deploy packet unification without completeness and reproducibility in the same slice.

### PR-B — Audit result/control-flow cleanup

- PASS / BLOCKER / HOLD only;
- PASS-only cache;
- CODE/CONTRACT BLOCKER-only fix counter;
- repeated same BLOCKER → HOLD;
- separate observable session/execution identity where enforceable.

### PR-C — Canonical CI and merge state

- exact-HEAD CI state;
- skipped/duplicate/retry handling;
- pre-merge packet regeneration and authority re-scan (§7.1);
- MERGE_GUARD;
- expected_head_sha merge;
- post-merge tree verification.

## 10. PR #146 trial

PR #146 is the first trial candidate for this protocol.

The trial must not change PR #146 merely to install this process. Apply the process around its
current candidate HEAD.

The main cleanup changed #146's inputs. Its head `e75daab6` is based on `65d64035`, and main is now
`b1b5175774159989bcf2ea2ef0caed45a018641b`. Its base is therefore stale (§7), and its audit packet
must be rebuilt. The next #146 HEAD is a refresh on current main that meets its recorded refresh
requirements:
- the CREATE contract correction `5862400626`, now on main;
- the ARCHITECTURE CREATE-state wording and the adoption boundary transferred from #142
  (`5874873776`);
- an ICBM-owned registration `stockQuantity` ≥ 1, or a named fail-closed gap;
- ADR-0014 §28.2/M5-31 and §28.3/M5-33 replaced in place.

Those requirements are authority inputs of its packet. This protocol does not decide them.

For a new #146 HEAD:

1. build a complete deterministic packet from the marked authoritative sources;
2. run GPT audit;
3. on GPT PASS, run a separate Claude audit;
4. require same HEAD + same packet digest + required evidence coverage;
5. mark READY only after DUAL PASS;
6. run FULL CI once for that new HEAD;
7. regenerate the packet and re-scan authority immediately before merge (§7.1), then pass
   MERGE_GUARD;
8. merge with expected_head_sha;
9. verify merged main tree equals the audited HEAD tree.

Historical PR #146 events remain useful regression fixtures:
- stale/misaligned spec input must be caught by packet completeness/authority handling;
- same-HEAD CI retry must not be mistaken for a new code identity;
- the zero-dimensional combination option defect is a genuine BLOCKER and must remain fail-closed.
