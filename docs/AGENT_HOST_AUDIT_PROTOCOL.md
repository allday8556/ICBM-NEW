# Agent Host Audit and Merge Protocol

Status: **ACCEPTED**
Date: 2026-09-28
Scope: ICBM-NEW Agent Host audit, CI, merge, and post-merge verification workflow.

This document records the operating protocol agreed after PR #146 exposed three distinct failure
classes: stale audit input/spec conflict, CI environment/timing failure, and a real code blocker.
The protocol preserves fail-closed behavior while reducing duplicate rules and duplicate work.

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

Its inputs are immutable source identities:

- exact HEAD SHA;
- audited base SHA;
- slice specification path and blob SHA;
- owner amendments and their immutable source identities;
- architect instructions and GitHub comment IDs;
- evidence packets and GitHub comment IDs;
- scope allow-list source identity;
- binding prior decisions required by the slice;
- authoritative-source manifest and source watermark.

The packet contains a manifest of every required source and the exact identities used to build it.

### 4.1 Completeness

Hard completeness checks:

- every source declared `required` by the manifest is present in the packet;
- every recognized authoritative marker discovered since the source watermark is classified before
  the packet is accepted;
- source identities resolve to the exact referenced immutable source;
- packet HEAD and base match the candidate being audited.

Failure of a hard completeness check is **HOLD**, not a code BLOCKER.

### 4.2 Reproducibility

Packet generation must be deterministic.

```text
same immutable source set + same HEAD + same base
→ byte-identical canonical packet
→ same audit_packet_digest
```

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
- evidence_seen: the immutable source identities the auditor consumed;
- blocker/hold reason when not PASS.

Before DUAL PASS is accepted:

```text
GPT.evidence_seen    ⊇ packet.manifest.required
Claude.evidence_seen ⊇ packet.manifest.required
```

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
2. GPT packet digest == Claude packet digest == current packet digest.
3. Packet completeness/reproducibility checks PASS.
4. GPT and Claude `evidence_seen` each cover `packet.manifest.required`.
5. FULL CI is GREEN for the exact HEAD.
6. No HOLD is active.
7. `audited_base_sha == current_base_sha`.

Merge must use `expected_head_sha` so a moved PR HEAD cannot be merged accidentally.

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

## 9. Rollout order

The protocol is implemented in three slices:

### PR-A — Audit Packet foundation

Must land as one safe unit:

- generated immutable packet;
- authoritative-source markers;
- source manifest and watermark;
- hard completeness detection;
- deterministic canonical serialization and reproducible digest;
- audit identity `(HEAD, packet_digest)`;
- evidence_seen recording and coverage checks.

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
- MERGE_GUARD;
- expected_head_sha merge;
- post-merge tree verification.

## 10. PR #146 trial

PR #146 is the first trial candidate for this protocol.

The trial must not change PR #146 merely to install this process. Apply the process around its
current candidate HEAD.

For a new #146 HEAD:

1. build a complete deterministic packet from the marked authoritative sources;
2. run GPT audit;
3. on GPT PASS, run a separate Claude audit;
4. require same HEAD + same packet digest + required evidence coverage;
5. mark READY only after DUAL PASS;
6. run FULL CI once for that new HEAD;
7. pass MERGE_GUARD;
8. merge with expected_head_sha;
9. verify merged main tree equals the audited HEAD tree.

Historical PR #146 events remain useful regression fixtures:
- stale/misaligned spec input must be caught by packet completeness/authority handling;
- same-HEAD CI retry must not be mistaken for a new code identity;
- the zero-dimensional combination option defect is a genuine BLOCKER and must remain fail-closed.
