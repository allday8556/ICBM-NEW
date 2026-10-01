# Agent Host Audit and Merge Protocol

Status: **V3 — canonical.** V2 became canonical when PR #147 merged this file into main as
`a0643e4642a759c93acc4204b3ff138ad4580e9a`; its bootstrap ended on main
`0919c2ae77f7d2f0162fbacdbd0d8274f34ea6f8` (PR #150). V3 is the operating-authority correction of
`documents/decisions/adr/0022-agent-operating-authority.md`: no human classification gates a packet,
a stop is either a user decision or a technical hold the Host recovers from by itself, and
auto-merge and auto-next are the default mode. Every mechanical check of V2 is kept (§0.2).
Date: 2026-09-28 (first text); refreshed on clean main `b1b5175774159989bcf2ea2ef0caed45a018641b`;
V3 on 2026-09-30; risk-scope clarification on 2026-10-01 (still V3, not V4).
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

A DUAL PASS, a GREEN FULL CI and a passed MERGE_GUARD authorize a merge only. What a slice may
build is decided by the canonical documents and by the operating authority of ADR-0022 (§0.2), not
by this protocol. The protocol never authorizes:
- a side-effecting provider mutation, LIVE write or canary;
- the residual-risk acceptance;
- any action `CLAUDE.md` §7.2 reserves for the user, such as a force-push, a branch deletion or a
  destructive operation.

### 0.1 Bootstrap transition (historical)

The bootstrap ended with PR #150. This section is kept as the record of how #147 and PR-A were
audited; nothing in it applies to a later slice. Where it speaks of a classification record, read
the V2 text of §3 that PR #147 merged: V3 has none.

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
  the packet flow of §1–§8 is the strong path. Under the 2026-10-01 risk calibration it applies to
  HIGH_RISK slices; BASIC and PROVIDER_ZERO use §1.1. **No slice other than #147 and PR-A may use
  this historical bootstrap exception**, including PR-B and PR-C.

### 0.1.1 V3 transition (the PR that delivers V3 only)

The V2 generator cannot audit the change that removes its own gate: it puts a source into a packet
only through a human classification record, and it does not carry the PR body. The owner's
instruction that ADR-0022 records forbids asking for such a record. So the one PR that delivers V3
is audited with the generator of **its own exact HEAD**:

- the Host scripts of that HEAD are copied, unchanged, to a staging Host directory outside the
  repository, and each copy's SHA-256 equals the hash the README pins for that HEAD;
- everything mechanical applies unchanged, in the V3 form of §1–§8: the exact HEAD, the generated
  packet and its digest, one identity for GPT and Claude, `evidence_seen` coverage, the PASS-only
  cache, DUAL PASS before READY, FULL CI after READY, the packet regeneration before the merge,
  MERGE_GUARD, the merge with `expected_head_sha`, POST_MERGE_VERIFY;
- the scripts that produce the packet are themselves in the audited diff, so both auditors read
  the generator they are given a packet by.

No other slice uses this. From that merge on, the runtime Host runs the merged scripts, and a
Host script that differs from the merged main is not used.

### 0.2 Operating authority (ADR-0022)

**Who decides what.** The single operational assignment is `documents/rules/14-operating-authority.md`
§14.1: product decisions are the user's, implementation decisions are the agent's, and
safety/correctness verification follows the risk tier of §14.2. This protocol does not restate a
second role contract.

The user authorized continuous execution of the work the canonical documents define. The user is
not a per-step approver. The default mode is:

```text
auto_merge = true
auto_next  = true
```

**Never a question for the user.** None of these stops a loop for a person, and none asks the user
to post anything on GitHub:
- implementation, refactoring and internal design;
- schema, migration and endpoint design for an already-approved feature;
- tests, lint, types, imports, paths, repository rules;
- migration numbering, a merge of the current main, a mechanical conflict;
- a BLOCKER and its repair, however often it takes;
- packet generation, source discovery, evidence bookkeeping, digests;
- CI, a stale HEAD, a re-audit of the same HEAD, FULL CI, MERGE_GUARD;
- the merge, POST_MERGE_VERIFY and the start of the next canonical slice.

**The user's decisions.** The active list lives only in rule §14.4: an undecided or out-of-scope
product decision, a protected execution action owned by rule §7.2, or the user's own hold. §5.1
encodes those concepts as Host categories; it does not add another approval rule.

A routine read-only provider call, read-back, health check or already-approved lookup with no
external side effect, sensitive-data export or material new cost is not a protected action and does
not stop for a separate approval.

An implementation choice is never in that list. §5.1 names the same list as the Host's closed
categories, one category per entry, and adds none.

**No self-authorization.** Removing the per-step approval does not let an agent widen the product.
An agent that finds the work needs a product feature or a product policy outside the canonical
documents does not build it: it stops with `HUMAN_DECISION_REQUIRED`. The auditors return the same
verdict when a diff does that.

**What V3 keeps from V2, unchanged for HIGH_RISK.** None of these is relaxed when rule §14.2 routes
a change to the strong path:
- the exact-HEAD audit and the generated Audit Packet;
- the packet digest and the two-part audit identity (HEAD, packet digest);
- the GPT audit, the independent Claude audit and the same-identity DUAL PASS;
- `evidence_seen` coverage and the PASS-only cache;
- FULL CI after READY;
- the pre-merge packet regeneration and the current-base check;
- MERGE_GUARD, the merge with `expected_head_sha`, POST_MERGE_VERIFY;
- every core invariant and protected action owned by rules §6 and §7.

## 1. Flow

### 1.1 Risk routing

Before selecting a validation path, apply rule §14.2 to the whole diff and its intended operation.

- **BASIC:** focused/default tests plus applicable lint/type checks and CI. No Audit Packet or
  dual audit is required.
- **PROVIDER_ZERO:** functional tests plus CI, and review when the changed boundary, contract or
  complexity needs it. It may cover read-only/provider-zero integration paths, but it executes no
  protected side-effecting LIVE action and does not rerun final LIVE evidence.
- **HIGH_RISK:** the exact-HEAD strong flow below. A side-effecting LIVE write, `UNKNOWN`/replay or
  external identity handling, credential/secret handling, an irreversible destructive data path or
  actual weakening of a core safety gate is here. A routine read-only provider operation is not.

A mixed change uses its highest credible risk. Uncertainty alone is not a tier; if an external side
effect, credential exposure, identity collision or irreversible damage cannot be ruled out, that
credible path is HIGH_RISK. The Host scripts in this directory implement the strong path; invoking
them therefore selects HIGH_RISK. Lower tiers use the normal PR/CI route. This is a scope rule for
V3, not a new Host generation.

**Transitional V3 auto-next baseline.** A BASIC or PROVIDER_ZERO PR merges through the normal path
after its own tier is green; a full-main audit is not a merge prerequisite and does not retroactively
make that PR HIGH_RISK or give it strong-path proof. The present V3 lookahead still requires
`full-audit-baseline.json` to name the current main with `status=DUAL_PASS`. Consequently, after a
lower-tier merge moves main:

```text
run-lookahead-main-v1.ps1
→ NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED
→ TECHNICAL_HOLD (never HUMAN_DECISION_REQUIRED)
→ supervising agent runs run-full-audit-v1.ps1 without `-ForceFull`
→ existing DELTA/FULL self-escalation policy
→ GPT + Claude DUAL PASS
→ baseline main=current main, status=DUAL_PASS
→ resume Host → lookahead → next canonical work
```

The Host does not invoke this standalone audit from the hold. The supervising agent observes the
technical hold and performs the recovery without a routine user question. With no supervising agent,
a fully unattended Host waits at `MAIN_NOT_DUAL_PASS_AUDITED`. BLOCKED, INSUFFICIENT, another
technical hold or main movement never starts next work; the existing recovery, backoff and
circuit-breaker rules apply, so the recovery does not loop without bound. A HIGH_RISK PR merged by
the Host already goes through its existing post-merge full-main audit; its DUAL PASS writes the
current-main baseline, and auto-next must not run a duplicate standalone audit.

### 1.2 HIGH_RISK strong flow

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
→ pre-merge packet regeneration and full re-scan (§7.1)
→ MERGE_GUARD
→ merge with expected_head_sha
→ POST_MERGE_VERIFY
→ fresh main: the next canonical slice of this Host's track (auto_next)
```

A BLOCKER from either auditor returns to the top on a new HEAD: repair, new packet, both audits
again. A slice ends on its DUAL PASS and merge, or on a real `HUMAN_DECISION_REQUIRED`. The number
of BLOCKERs and repairs never ends it and never hands it to the user. The one other way a run ends
is the cost circuit breaker of §5.1: a technical hold that keeps returning on the same state ends
the run as `TECHNICAL_HOLD_EXHAUSTED`, which is a report and asks for no decision.

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

## 3. Packet sources and markers

**A slice declares its evidence by citing it.** No one classifies a source, and nothing is
inferred.

- The **declaration** is the PR body, together with the Host's slice specification or remediation
  authorization when one exists. The PR body is itself a required packet source: both auditors
  read what the slice says it does, does not do and relies on, and an edited body is a new audit
  identity.
- The **scanned streams** are:
  - the PR's own four streams: conversation comments, body, reviews, review comments;
  - the comments of every issue the declaration names as `Issue #<n>`;
  - any stream an optional Host manifest designates. The manifest only designates streams.
- A **citation** is a source id of 9 to 12 digits in a code span, and its form names the kind of
  the source, because GitHub numbers the kinds separately:
  - `` `<id>` `` — a conversation comment of the PR or of a named issue;
  - `` `review:<id>` `` — a review of the PR;
  - `` `review-comment:<id>` `` — a review comment of the PR.

  A number outside a code span, such as a CI run id, is not a citation.
- Citations are read **from the declaration only**. An id or a path in a code span inside a file
  of the diff — an ADR that names a historical record, a document that names another — is content
  under audit, not a citation of the slice.
- **Canon is read at the audited base.** The base is the canon that binds before the slice; what
  the slice changes in the canon is in its diff, and is judged, never judged by. A slice can
  therefore not rewrite the canon it is held to.
- **The baseline canon is the Host's, not the declaration's.** Every packet carries, at the audited
  base and whatever the declaration cites: `documents/roadmap/ROADMAP.md`,
  `documents/roadmap/CURRENT-MILESTONE.md`, `documents/rules/07-execution-safety.md` and
  `documents/rules/14-operating-authority.md` — what may be built, in which order, what is never
  done, and who decides. The list is fixed in the Host (`Get-BaselineCanon`); no runtime
  configuration and no declaration shortens it. A baseline document that is not at the base is
  named as absent in the packet header, never skipped silently.
- A **canonical document citation** is `` `canon:<repository path>` `` in a code span. It adds that
  file, as it is at the audited base, to the packet as a required source bound by its git blob
  SHA. The declaration cites the further canonical documents the slice implements (rule §8.3:
  which contract it implements). A declaration only adds canon to the baseline.
- A **packet source** is a comment, review or review comment in a scanned stream that the
  declaration cites **by its kind and its id**. A source of another kind that carries the same id
  never resolves the citation. Every packet source is `required`: both auditors must report it in
  `evidence_seen`.
- **Declared evidence never disappears silently.** A citation that no scanned stream holds — a
  wrong id, a source on an issue the declaration does not name, a source deleted since — or a
  cited canonical document that is not at the audited base cannot be
  read. That is a **TECHNICAL_HOLD** (`CITED_SOURCE_UNRESOLVED`), which the agent clears by
  correcting the declaration. It is never dropped from the packet.
- **The declaration chooses evidence, never authority.** A user's product decision binds through
  the canonical documents, because a decision is binding only once it is reflected in an ADR or a
  canonical document (rule §1.1). The audited base SHA fixes that canon (§4), and both auditors
  judge the diff against it whatever the declaration cites. A comment the declaration does not
  cite is therefore missing evidence at most, never a removed decision: a diff that goes beyond
  the canon is `HUMAN_DECISION_REQUIRED` (§0.2) whatever its declaration cites or omits. The
  user's own stop on a PR is the owner's hold file, which no declaration can affect.
- **An audit is never blind to the canon.** The packet carries the baseline canon, the canonical
  documents the declaration adds, and the ones the diff changes. An auditor that needs a canonical
  document the packet does not carry returns `INSUFFICIENT` and names its path; it never assumes
  what an unseen document says, and it never passes on a diff alone. The agent then cites that
  document and the new packet is audited. A declaration cannot make an audit pass by citing less:
  the scope and authority documents are in the packet without it.

**The scan is full and edit-aware.** Every packet generation, including the pre-merge regeneration
of §7.1, reads every source of every scanned stream in full (all pages) at its current body. It
never skips a source because of its creation order, its locator or an earlier scan. The **source
watermark** is scan provenance only, never a boundary below which sources are skipped.

A stream or a declaration that cannot be read completely is a **TECHNICAL_HOLD** (§5.1), never a
partial scan: a failed page, a permission refusal, a truncated listing.

**Markers are provenance.** Three tokens are recognized:

```text
[ARCHITECT-INSTRUCTION]
[EVIDENCE-PACKET]
[OWNER-AMENDMENT]
```

A source **carries a marker** only when the first non-empty line of its body, with surrounding
whitespace and a trailing CR removed, equals exactly one of the tokens. The match is
case-sensitive, and nothing else may be on that line. Marker text anywhere else is ordinary text:
on a later line, inside prose, in a quote, in inline code, in a code block, after other text on the
first line, or in another case.

- A marker names what kind of record a source is. The packet records it as the source's `kind`.
- A marked source is a packet source when the declaration cites it, exactly like an unmarked one.
- **A marked source never holds a packet.** A marked source the declaration does not cite is
  history. It is listed in the scan provenance and is otherwise ignored.
- **No classification record is read.** A `scope: PR #<N>` record with `required:`,
  `evidence-only:` and `excluded:` sections was V2's human classification. Existing ones, such as
  the #147 bootstrap record `5876525801` and the owner amendments `5907095955` and `5909645067`,
  stay in GitHub as history and are never edited or deleted. None is required, none is parsed, and
  no new one is asked for.

**Authority write guard.** Kept from V2, unchanged: **no automated actor creates or edits a
marker-first body.** An automated actor is the Agent Host, Claude Code, GPT or any other one. The
ban covers comments, reviews, review comments, and issue or PR bodies, and applies to creation and
to any edit alike. Every Host GitHub write is checked before it is sent, and a marker-first body is
refused before the write. A draft of the same text written to a local file is not refused.
Authorship is not machine-provable here, so the protocol never claims that a GitHub account
identity proves authorship.

**Edits still bind.** A packet source enters the packet by its locator and the SHA-256 of its
current body (§4). An edited body, a new citation and a removed citation each change the packet
digest, so each invalidates an audit PASS (§2, §7.1).

## 4. Audit Packet

The Audit Packet is a **generated artifact, never a hand-edited audit record**.

Its inputs are **content-bound source identities**:

- exact HEAD SHA;
- audited base SHA, which fixes the canonical ROADMAP, ADRs, architecture and contracts the slice
  is judged against;
- the PR's changed files and their complete diff;
- slice specification path and blob SHA, and the scope allow-list source, when the Host selected
  the slice;
- the slice declaration itself (§3):
  - the PR body, as a required source, by its locator and the SHA-256 of its body. It is read once
    per generation;
  - the Host's slice specification or remediation authorization, when one exists, by the identity
    listed above;
- the baseline canon and the canonical documents the declaration cites (§3), each by its path and
  git blob SHA at the audited base;
- the durable evidence the declaration cites (§3). Citations are read from the whole declaration:
  the PR body as it was read for the packet, and the slice specification or remediation
  authorization when one exists;
- the scanned-stream list, and the source watermark as scan provenance only, outside the canonical
  packet bytes (§3, §4.2).

The PR body and each cited source are identified as below.

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

- the PR body is readable and is present in the packet, once, as a required source;
- every citation of the declaration — the PR body and, when one exists, the slice specification or
  remediation authorization — resolves to a source a scanned stream holds, and that source is
  present in the packet, once, with its body;
- every source in every scanned stream is re-scanned in full at its current body (§3);
- every changed file of the PR is in the packet, complete;
- packet HEAD and base match the candidate being audited.

Failure of a hard completeness check is a **TECHNICAL_HOLD** (§5.1), not a code BLOCKER: the Host
retries it. No completeness check depends on a human record.

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

### 5.1 Verdicts and holds

The audit output contract is the prompt of `automation/agent-host/run-audit-v1.1.ps1` under policy
`packet-v9`: it offers exactly `VERDICT=<PASS|BLOCKER|INSUFFICIENT|HUMAN_DECISION_REQUIRED>`, and the
Host itself may turn a verdict into `HOLD`. A Host that still runs an earlier policy offers only the
first three; its prompt is that Host's, not this contract.

An exact-head audit returns one of:

- **PASS** — audit satisfied for this audit identity.
- **BLOCKER** — a material defect in changed code on a reachable path, in one of the six kinds of
  ADR-0022 §4.1: real data damage, a real duplicate
  registration or wrong external transmission, a security or credential leak, a core function that
  does not work, a test or CI failure caused by a real code defect, or a bypass of a core safety
  gate. The auditor names the kind at the start of its summary. It is repaired automatically: new
  HEAD, new packet, both audits again. A wording difference between documents, citation format, a
  non-essential packet detail, a hypothetical concern, a defence-in-depth improvement, an
  unrelated pre-existing issue, a suggestion to be more rigorous or a request to prove a decided
  requirement in more detail is not a BLOCKER: the auditor passes and records it as a note.
- **HUMAN_DECISION_REQUIRED** — the diff itself needs one of the user's decisions (§0.2). The
  auditor names the category of the closed list at the start of its summary. A verdict that names
  none has not said what the user should decide: the Host records it as a technical **HOLD** and
  audits again.
- **INSUFFICIENT**, or a verdict the Host turns into **HOLD** because a required source is missing
  from `evidence_seen` — a technical hold: the audit is run again.

A loop pass stops in exactly one of two classes:

```text
HUMAN_DECISION_REQUIRED   the closed list of §0.2. The run ends and waits for the user.
TECHNICAL_HOLD            everything else. The Host recovers or retries by itself.
```

- The class comes from the **category** of the reason, never from how often something failed, and
  never from the words `HUMAN_DECISION_REQUIRED` alone.
- A reason is the user's only when the whole reason is a form the Host composes after it validated
  the category: the category by itself, `NEXT_HOLD_<CATEGORY>` or
  `<WHO>_HUMAN_DECISION_REQUIRED_<CATEGORY>` for every category except `OWNER_HOLD`, or one of the
  two owner-hold reasons (`PR_ON_OWNER_HOLD`, `GUARD_PR_ON_OWNER_HOLD`), which the orchestrator
  writes only after it has read the owner's hold file. `OWNER_HOLD` in any other form is technical,
  and so is a category word inside any other reason: `NO_LIVE_ACTION` is technical.
- There is no third class. A run the circuit breaker ended has the status
  `TECHNICAL_HOLD_EXHAUSTED` and the class `TECHNICAL_HOLD`.
- The human categories are a closed list in the Host, and they are exactly the entries of §0.2:
  `NEW_PRODUCT_FEATURE`, `PRODUCT_DIRECTION_UNDECIDED`, `BEYOND_USER_REQUIREMENT`, `LIVE`,
  `SUPPLIER_ORDER`, `RESIDUAL_RISK_APPROVAL`, `COST`, `EXTERNAL_DATA_TRANSFER`, `DESTRUCTIVE`, and `OWNER_HOLD` for
  the user's own hold file on a PR.
  A reason that names none of them is technical.
- Examples of a TECHNICAL_HOLD: a stale main, a packet that could not be generated, an unreadable
  stream, a CI infrastructure failure, a mergeability problem, a migration collision, source
  bookkeeping, an auditor that returned nothing readable.

**A TECHNICAL_HOLD is retried.** The same state (PR, HEAD, main, reason) is run again with a
doubling wait. After the configured number of consecutive retries of that same state the run ends
`TECHNICAL_HOLD_EXHAUSTED`. That is a cost circuit breaker and a report. It is not a request for a
decision. The count is of consecutive holds in one state: any different state in between starts it
again, so a state that returns later is retried in full.

**A repeated BLOCKER is never handed to the user.** After the configured number of repair cycles
the fixer stops repeating itself: every later attempt is an independent re-analysis, given the
blockers the earlier attempts left and told to take another approach.

**Scope reports are not holds.** A fix or an implementation that needs a file outside the slice's
listed paths, a migration or a removed file is reported in the run output and in the PR body, and
the auditors judge it. An explicit architect ruling's file list is still enforced.

### 5.2 Audit cache

- PASS may be cached only under the exact `(HEAD, audit_packet_digest)`.
- BLOCKER and HOLD are not reusable audit cache entries; a later audit is fresh.
- A packet-digest change invalidates audit PASS without invalidating same-HEAD CI.

The Host never imports an external audit as though the Host performed it. External audit evidence
is a packet source when the declaration cites it, and its provenance is preserved.

## 6. CI

DRAFT runs only the lightweight path required by `.github/workflows/ci.yml`.

FULL CI is requested only after DUAL PASS and READY for the current audit identity.

The PR stays draft until then. Under `.github/workflows/ci.yml`, marking it ready for review is the
FULL CI request for that exact HEAD. A draft run has no `CI gate` result at all.

The workflow selects BASIC, PROVIDER_ZERO or FULL consistently with rule §14.2. BASIC runs focused
default/contract checks; PROVIDER_ZERO runs the functional provider-zero suite; FULL is reserved
for HIGH_RISK and explicit final-LIVE verification. Every selected scope ends in the same `CI gate`
GREEN rule below.

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
BLOCKER. A repeated unresolved environment-shaped failure becomes a TECHNICAL_HOLD (§5.1) rather
than an unlimited rerun loop. Nobody is asked whether CI should be handled.

## 7. MERGE_GUARD

Merge is allowed only when all of the following are true:

1. PR HEAD == GPT HEAD == Claude HEAD == FULL CI HEAD.
2. GPT packet digest == Claude packet digest == the digest of the packet **regenerated at merge
   time** (§7.1). A previously generated packet is never treated as current.
3. Packet completeness/reproducibility checks PASS on the regenerated packet.
4. GPT and Claude `evidence_seen` each cover the regenerated `packet.manifest.required`, by
   content-bound identity (§4).
5. FULL CI is GREEN for the exact HEAD.
6. No hold is active, the owner's own hold file on the PR included.
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
- POST_MERGE_VERIFY's tree equality detects a base that moved in the window (HOLD). A merge found
  on a moved base is never accepted as the audited result: the run holds as a TECHNICAL_HOLD and
  no next slice starts from it.

**The atomic close is GitHub's, and it is a repository setting.** GitHub's merge API binds only the
head (`expected_head_sha`); it has no parameter that binds the base. What binds the base atomically
is the repository ruleset's strict required-status-check policy ("require branches to be up to date
before merging"): GitHub then refuses the merge mutation itself when the base moved. On this
repository that policy is off (`strict_required_status_checks_policy: false`). The Host has no
repository-settings write at all — its GitHub writes are comments, PR bodies, update-branch and the
merge — and this protocol does not make one a condition of a merge. Until the policy is on, the
window is bounded and detected as above; once it is on, GitHub closes it.

**Cut-off:** a cited source edited after that final re-scan is outside this merge's audited
input and binds the next audit, not this merge.

**Base freshness is the Host's work.** A PR HEAD that does not contain the current base is brought
up to date before it is audited, with GitHub's own update-branch (no force), and the new HEAD is
audited from scratch. A conflict GitHub cannot merge is a TECHNICAL_HOLD for the repair path.

A stronger atomic mitigation was assigned to PR-C (§9). Its reachable form is the ruleset policy
above; the Host scripts have no atomic means of their own.

### 7.1 Pre-merge packet regeneration and full re-scan

Immediately before MERGE_GUARD is evaluated, the Host regenerates the Audit Packet for the same HEAD
and base. It **fully re-scans every scanned stream** at current bodies, with no watermark skipping
(§3), and re-reads the declaration.

- **Unchanged:** the regenerated canonical bytes, `audit_packet_digest` and manifest equal the
  audited packet. Only then may MERGE_GUARD proceed.
- **Any difference** invalidates the DUAL PASS. The Host returns to audit under the new audit
  identity. Examples:
  - a cited source's body changed;
  - the declaration cites one more source, or one fewer;
  - a changed manifest, bytes or digest.

  The same-HEAD FULL CI stays valid (§2).
- **A marker is never a difference.** A source edited to carry a marker, a new marked source and an
  edited legacy classification record change nothing unless the declaration cites them.
- **A stream or declaration that cannot be re-read** is a TECHNICAL_HOLD, for example one that was
  deleted or is inaccessible.

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

Tree mismatch is a TECHNICAL_HOLD and requires investigation; it is not silently accepted, and the
next slice does not start. The check reads GitHub's own record of the merge on every pass that finds
the PR merged, so a restart cannot skip it.

**After a verified merge** the Host audits the merged main and, with `auto_next`, reads the
canonical ROADMAP on that fresh main and starts the next canonical slice of its track. One Host runs
one track; parallel tracks run as separate Host directories, each with its own state, worktrees,
PR, packet, audit identity and merge. A track whose next step is one of the user's decisions stops
with `HUMAN_DECISION_REQUIRED`; the other tracks go on. Tracks never reorder the canon: a track whose
next step depends on an incomplete step of another track waits (`EARLIER_STEP_INCOMPLETE`, a
technical hold) instead of skipping it, and a track runs ahead only where the canonical documents
make its work independent of the other track's open steps.

**Final LIVE evidence is evaluated at final LIVE closeout, not after every merge.** A merge means a
previous exact-main visual/restore/retention proof does not cover the new commit, but an ordinary
BASIC or PROVIDER_ZERO merge does not rerun or re-record it. The proof is established once on the
final main immediately before the bounded LIVE action, through its reviewed path. Until then the
state is simply not current for LIVE and grants no permission. `UNKNOWN` never becomes retryable,
identity remains isolated and credentials remain protected throughout.

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

Required discovery tests (V3; `automation/agent-host/tests/`, pinned by the fixture test; every
fixture scenario pins its ending, and the runner fails on a failed or missing pin):
- a packet is complete with no `[OWNER-AMENDMENT]` and no classification record anywhere;
- a legacy `[OWNER-AMENDMENT]` in a scanned stream, classified or not, never produces a hold;
- no `scope: PR #<N>` record is needed for a canonical slice packet;
- audit bookkeeping never produces `HUMAN_DECISION_REQUIRED`;
- a stream page that cannot be read, or a truncated listing → TECHNICAL_HOLD, never a partial scan;
- a citation no scanned stream holds, or a cited source deleted since → TECHNICAL_HOLD, never a
  smaller packet;
- an auditor's `HUMAN_DECISION_REQUIRED` without a category of the closed list → technical, audited
  again;
- an edited body of a cited source, and a changed citation, each change the packet digest (§4, §7.1);
- an uncited source edited in place to add a marker changes nothing;
- marker grammar (§3), positive and negative, as in V2;
- authority write guard (§3): an automated create or edit of any GitHub body whose first non-empty
  line is a recognized marker is **refused before the GitHub write**. A draft of the same text
  written to a local file is not refused.

Required control-loop tests (V3, same place):
- BLOCKER → repair → new HEAD → new packet → re-audit;
- GPT PASS + Claude PASS → DUAL PASS;
- DUAL PASS + FULL CI GREEN + MERGE_GUARD PASS → merge with `expected_head_sha`, and
  POST_MERGE_VERIFY;
- after POST_MERGE_VERIFY the next canonical slice is selected and, with `auto_next`, started;
- a step that needs a new product feature → `HUMAN_DECISION_REQUIRED`;
- a protected execution action under rule §7.2 → `HUMAN_DECISION_REQUIRED`; a routine read-only
  provider call is not one;
- a changed HEAD or packet digest → no earlier PASS is reused;
- a packet that changed just before the merge → no merge, a re-audit.

### PR-B — Audit result/control-flow cleanup

- PASS / BLOCKER / the two hold classes of §5.1;
- PASS-only cache;
- repeated BLOCKER → independent re-analysis, never a hand-off (§5.1);
- separate observable session/execution identity where enforceable.

### PR-C — Canonical CI and merge state

- exact-HEAD CI state;
- skipped/duplicate/retry handling;
- pre-merge packet regeneration and full re-scan (§7.1);
- MERGE_GUARD;
- base containment before merge (§7 condition 8);
- expected_head_sha merge;
- post-merge tree verification;
- the stronger atomic mitigation of the final-check window (§7 residual): binding the base and the
  audited authority snapshot to the merge mutation, or re-verifying them atomically with it.

## 10. PR #146 trial (historical)

PR #146 was closed as superseded by PR #158. This section is the record of the V2 trial plan.

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
