# Agent Host Audit and Merge Protocol

Status: **V2 — canonical.** PR #147 merged this file into main as `a0643e4642a759c93acc4204b3ff138ad4580e9a`,
so it has been the Agent Host's canonical protocol since that commit, under the bootstrap transition
of §0.1. The bootstrap ended on main `0919c2ae77f7d2f0162fbacdbd0d8274f34ea6f8` (PR #150 merged,
POST_MERGE_VERIFY Issue #89 `5882586622`, owner acceptance Issue #151 `5882336231`); every slice
since uses §1–§8. Before the merge, the #146 trial instruction (`5870526033`) cited the pre-refresh
text at `9cc4939a` as its protocol source.
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
- `CLAUDE.md`, `documents/roadmap/ROADMAP.md` or `documents/architecture/ARCHITECTURE.md`;
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

### 0.1 Bootstrap transition

This protocol's packet-dependent steps need the packet generator that PR-A delivers (§9). They are
therefore bounded as follows.

- **#147 is contract-only.** Merging it records the contract. It does not make the generated Audit
  Packet or automated authority discovery available.
- **Until the bootstrap ends, packet-dependent automation is not claimed as available.** No Host
  step may claim any of the following:
  - a generated packet, an `audit_packet_digest` or packet completeness;
  - automated discovery;
  - a packet-based MERGE_GUARD.

  Packet-dependent work waits for the end of the bootstrap. This includes the #146 trial of §10.
- **Bootstrap audit identity, for #147 and PR-A only:**

  ```text
  (exact HEAD, audited base SHA, bootstrap_manifest_digest)
  ```

  The bootstrap manifest is an **explicit, content-bound evidence manifest** that lists every source
  the audit relies on:
  - git sources by commit or blob SHA;
  - mutable GitHub sources by locator and canonical body digest (§4);
  - the classification record of §3.

  It is published in a durable GitHub review record for that HEAD. `bootstrap_manifest_digest` is
  the SHA-256 of its canonical listing.
- **What else applies unchanged during the bootstrap:**
  - GPT and Claude audit the same identity;
  - each `evidence_seen` covers the bootstrap manifest by content-bound identity;
  - the marker grammar and the classification authority of §3 apply, and designated streams are
    re-scanned in full, by the operator where no generator exists;
  - PASS-only caching;
  - DUAL PASS before READY;
  - FULL CI after READY.
- **MERGE_GUARD during the bootstrap** is §7 with the bootstrap manifest in place of the packet:
  - Immediately before the merge, every listed source is re-read and the designated streams are
    re-scanned. The manifest must be unchanged, and every marked source must be classified.
    Otherwise the rules of §7.1 apply (HOLD or re-audit).
  - Conditions 1 and 5–8 are unchanged, and the merge uses `expected_head_sha`.
  - POST_MERGE_VERIFY applies unchanged.
- **The bootstrap ends automatically** when PR-A is merged, its POST_MERGE_VERIFY passes and its
  bootstrap acceptance is recorded in a durable user or architect source. From that main commit on,
  every slice uses the packet flow of §1–§8. **No slice other than #147 and PR-A may use this
  exception**, including PR-B and PR-C.

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

**Recognition grammar.** Recognition is deterministic.

A source **carries a marker** only when **the first non-empty line of its body, with surrounding
whitespace and a trailing CR removed, equals exactly one of the three tokens above**. The match is
case-sensitive, and nothing else may be on that line.

Marker text anywhere else is **not** a marker. It is ordinary text:
- on a later line;
- inside prose;
- in a quote (`> [ARCHITECT-INSTRUCTION]`);
- in inline code;
- in a code block, where the first line is the fence;
- after other text on the first line (`[ARCHITECT-INSTRUCTION] see below`);
- in another case (`[architect-instruction]`).

An audit or status comment that merely mentions a token stays unmarked. For example, the existing
#146 instruction `5870526033`, whose first line is exactly `[ARCHITECT-INSTRUCTION]`, is marked.

**Classification authority.** The Host never classifies a source on its own judgement. It cannot
clear its own HOLD.

A classification says whether a marked source is `required`, excluded with a reason, or
evidence-only. It is supplied only by a **durable, content-bound classification record under user
or architect authority**, which is one of:
- a marked `[ARCHITECT-INSTRUCTION]` or `[OWNER-AMENDMENT]` source in a designated stream that
  states the mapping;
- a canonical slice specification (a git blob) that the user or architect explicitly authorized to
  carry it.

A marked record is authority by its own marker and needs no further classification of itself.

**Authority write guard.** A marker only means authority if no automation can write one. So:

1. **No automated actor creates or edits a marker-first body.** An automated actor is the Agent
   Host, Claude Code, GPT or any other one. A marker-first body is a GitHub body whose first
   non-empty line, under the grammar above, is a recognized marker token. The ban covers comments,
   reviews, review comments, and issue or PR bodies, and applies to creation and to any edit
   alike.
2. **Marked authority sources are posted and edited only by the user or the architect**, as an
   explicit human authority action. Automation may prepare a **draft** of such a body. It never
   publishes or edits the marked body itself.
3. **Authorship is not machine-provable here.** The user, the Host, Claude Code and GPT can all
   write under the same GitHub account. The protocol therefore **never claims that GitHub account
   identity proves authorship**.

   The boundary is an operating discipline together with a **Host-enforced write guard**: every
   Host GitHub write is checked before it is sent, and a marker-first body is refused before the
   write. An automated actor that bypasses the guard breaks the discipline. The marked body it
   produced is not a valid authority source.
4. This does not invalidate authority records that the user or architect posted directly, such as
   the #147 bootstrap classification record `5876525801`. Their content-bound identity stands.

The mapping names each source by its content-bound identity (locator and body digest). The Host only
applies that exact declared mapping. It never invents, infers or reinterprets one. A marked source
with no matching authorized classification stays **HOLD**, and so does a source whose body digest no
longer matches its mapping. The classification record is itself a content-bound, required source of
the manifest and of `evidence_seen`.

**Discovery is a full re-scan and is edit-aware.** The manifest names the **designated authoritative
streams** of the slice, for example:
- the comments of a named issue;
- a PR's conversation comments, reviews and review comments;
- a named issue or PR body.

Every packet generation, including the pre-merge regeneration of §7.1, reads **every source in
every designated stream in full** (all pages) at its **current** body. It then checks each source
that currently carries a recognized marker against the authorized classification (above), whenever
that source was created or edited.

The scan never skips a source because of its creation order, its locator or an earlier scan. A
source created before an earlier scan and edited in place to add a marker is therefore found like a
new one.

The **source watermark** is recorded as scan provenance only (what was scanned, and when). It is
never a boundary below which sources are skipped.

A source that currently carries a recognized marker and has no matching authorized classification
prevents a clean packet from being issued until an authorized classification record covers it. Such a source is **HOLD**, whether it is
new or an old source edited in place, for example a formerly unmarked comment that now carries
`[ARCHITECT-INSTRUCTION]`. The generator does not guess whether it should be ignored.

A stream that cannot be read completely is **HOLD**, never a partial scan. Examples:
- a failed page;
- a permission refusal;
- a truncated listing.

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
- authoritative-source manifest (designated streams and classifications) and source watermark
  (scan provenance only, outside the canonical packet bytes, §3, §4.2).

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
- every source in every designated stream is re-scanned in full at its current body (§3). Every
  source that currently carries a recognized marker, including an older source edited in place to
  add one, is covered by an authorized classification (§3) before the packet is accepted. A stream that cannot be read completely is
  HOLD;
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

For a ready PR that touches only Markdown under `documents/evidence/`, the workflow runs its lighter docs
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
8. The PR HEAD contains the current base: `merge_base(PR_HEAD, current_base_sha) ==
   current_base_sha`. Equivalently, the HEAD is `behind_by == 0` against the current base. This is
   checked **before** the merge, in addition to condition 7. A divergent or behind HEAD is never left
   for POST_MERGE_VERIFY to discover.

Merge must use `expected_head_sha` so a moved PR HEAD cannot be merged accidentally.

**Residual: the final-check window.** A small TOCTOU window remains between the final authority
re-scan and base check and GitHub's merge mutation. `expected_head_sha` binds the head atomically,
but not the base or a GitHub authority source edited in that window.

Mitigations in this protocol:
- conditions 7 and 8 and §7.1 are re-evaluated **immediately before** the merge call;
- POST_MERGE_VERIFY's tree equality detects a base that moved in the window (HOLD).

**Cut-off:** an authority source edited after that final re-scan is outside this merge's audited
input and binds the next audit, not this merge.

A stronger atomic mitigation is assigned to PR-C (§9).

### 7.1 Pre-merge packet regeneration and authority re-scan

Immediately before MERGE_GUARD is evaluated, the Host regenerates the Audit Packet for the same HEAD
and base. It **fully re-scans every designated authoritative stream** at current bodies, with no
watermark skipping (§3), and re-reads every content-bound source (§4).

- **Unchanged:** the regenerated canonical bytes, `audit_packet_digest` and manifest equal the
  audited packet. Only then may MERGE_GUARD proceed.
- **Unclassified marker:** a source that currently carries a recognized marker and has no
  matching authorized classification is **HOLD** (§3). It may be new, or an older, previously unmarked
  source edited in place after the DUAL PASS. An incompletely read stream is HOLD too.
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
- source manifest (designated streams, classifications) and watermark as scan provenance only;
- full, edit-aware re-scan of every designated stream on every generation (§3);
- hard completeness detection;
- deterministic canonical serialization and reproducible digest;
- audit identity `(HEAD, packet_digest)`;
- evidence_seen recording and coverage checks by content-bound identity.

Do not deploy packet unification without completeness and reproducibility in the same slice.

Required discovery tests (PR-A):
- an existing **unmarked old comment**, created before the previous scan and not in the manifest,
  is **edited in place to add a recognized marker** → the next generation, and the pre-merge
  regeneration of §7.1 after a DUAL PASS, report it as an **unclassified marker → HOLD**; no clean
  packet is issued;
- a new marked source that is not classified → HOLD;
- a stream page that cannot be read, or a truncated listing → HOLD, never a partial scan;
- an edited body of a classified source → its body digest and the packet digest change (§4, §7.1).
- marker grammar (§3), positive: a body whose first non-empty line is exactly one token (with
  surrounding whitespace or a trailing CR) is marked, for example the form of `5870526033`;
- marker grammar, negative: an audit or status comment that merely mentions the tokens in prose
  stays **unmarked**, as does a token on a later line, in a quote, in inline code or a code block,
  followed by other text on the first line, or in another case;
- classification authority (§3): a marked source with no matching classification from an authorized
  record → HOLD; the Host cannot clear it by itself; a classified source whose body digest changed
  no longer matches → HOLD;
- authority write guard (§3): an automated create or edit of any GitHub body (comment, review,
  review comment, issue or PR body) whose first non-empty line is a recognized marker is
  **refused before the GitHub write**. This covers a new body and an edit that turns an unmarked
  body into a marked one. No write request is sent. A draft of the same text written to a local file
  is not refused.

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
- base containment before merge (§7 condition 8);
- expected_head_sha merge;
- post-merge tree verification;
- the stronger atomic mitigation of the final-check window (§7 residual): binding the base and the
  audited authority snapshot to the merge mutation, or re-verifying them atomically with it.

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
