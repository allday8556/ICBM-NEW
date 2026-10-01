# ADR-0022 — Agent operating authority: the user decides the product, the agent runs the loop

Status: **ACCEPTED** — decided by the repository owner (the user) on 2026-09-30, on canonical main
`c57149fdc73f565f921130a6ef09edfa8b2d3f3e`. It is a governance and control-plane contract: it changes
the Agent Host's rules and scripts and no product code, schema, endpoint or domain contract.
- **Its authority becomes effective when this exact PR is audited (GPT exact-head), independently
  cross-audited (Claude) and merged.**
- It amends `documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md` (V2 → V3) and adds
  `documents/rules/14-operating-authority.md`.
- It amends ADR-0020 **by the note of §7 only**. ADR-0020's text, and every safety gate of ADR-0014
  and ADR-0018, are neither rewritten nor relaxed.

Decision owner: the user (repository owner — `CLAUDE.md` §1). The user gave this decision in the
working session of 2026-09-30 and confirmed it there when asked. Recorded by Claude Code; this
document is the durable record, and it is the last authority record of this kind the process needs.

Scope calibration, 2026-10-01: the same decision owner limited BLOCKER to the five material defect
classes recorded by PR #165 comment `5921331154` and selected the risk-based validation tiers now
canonical in rule §14.2. PR #174's calibration added the separately sourced safety-gate-bypass class
in §4.1. A later same-day proportionality pass narrowed HIGH_RISK and human approval to credible
side-effecting or irreversible risk; a routine read-only provider operation is not one by name
alone. These are small control-plane clarifications of this ADR, not a new architecture project or
Agent Host V4.

---

## Context

Agent Host V2 made every audit depend on a human record. A source that carried an authority marker
had to be classified by a marker-first record that only a person may post, with an exact
`scope: PR #<N>` line and typed entries. Without it no packet was issued.

PR #160 showed the cost. Its code and tests were finished and the packet still held, four times
over, on four marked comments of Issue #126 that nobody had classified for that PR. Clearing it
needed the user to post a record by hand. The same happened on earlier slices. The user became a
clerk of the process: asked to post amendments, to confirm repairs, to approve merges, to approve
the next slice.

None of those questions was a product decision. They were bookkeeping, and the mechanical checks
already covered what they guarded: the exact-HEAD audit, two independent audits of one identity,
the packet digest, FULL CI, the merge guard and the post-merge verification.

## Decision

### 1. Standing operating authority

When the user starts a track of work, that start is the authority for **all of the work the
canonical documents already define for it**, until it is done. The canonical documents are
`documents/roadmap/ROADMAP.md`, the ADRs, `documents/architecture/ARCHITECTURE.md`, the contracts and
the acceptance records, read from the fresh main.

The user is not a per-step approver. A canonical sentence that asks for a separate per-step
authorization, a kickoff or an architect sign-off is satisfied by this standing authority only when
the step it gates is of the agent's kind (§3).

The standing authority never satisfies a sentence that gates one of the user's decisions (§2).
That sentence keeps its full force: the step waits for the user, as §2 and §7 require.

### 2. What the user decides

Only these stop a loop for the user:

- **A.** a product feature the canonical requirements do not contain;
- **B.** a user-visible behaviour, UX or policy that the existing requirements do not decide and
  that has several real product directions;
- **C.** a change that goes beyond the user's existing requirements;
- **D.** a protected execution action in the active list owned by rule §7.2;
- **E.** a hold the user placed on a PR themselves (the owner's hold file). It is the user's own
  stop, so only the user lifts it.

Rule §14.4 is the active operational list; this ADR records why it exists. The Host calls a matching
stop `HUMAN_DECISION_REQUIRED`. A routine read-only provider call, read-back, health
check or already-approved lookup that has no external side effect, sensitive-data export or material
new cost is not D and needs no separate user approval. A technical choice is never one of these
categories, and a stop that names none of them is not the user's, whatever it calls itself.

### 3. What the agent decides and does

The agent is responsible for, and never asks the user about:

- design detail, implementation, refactoring and internal architecture;
- schema, migration and endpoint design for an already-approved feature;
- tests, lint, types, imports, paths and repository rules;
- migration numbering, merging the current main, mechanical and non-semantic conflicts;
- repairing an audit BLOCKER, as often as it takes;
- packet generation, source discovery, evidence bookkeeping, digests;
- CI runs and re-runs, a stale HEAD, a re-audit;
- FULL CI, MERGE_GUARD, the merge, POST_MERGE_VERIFY;
- starting the next canonical slice.

Those choices are verified, not approved. The verification strength is selected by the canonical
risk tiers in rule §14.2; only HIGH_RISK requires the exact-HEAD independent dual audit.

The current operational assignment is stated once in rule §14.1. This decision record does not
copy a second role table.

### 4. Holds

A stop is one of two classes, decided by its category and never by a count.

- **`HUMAN_DECISION_REQUIRED`** — exactly the list of §2. The run waits.
- **`TECHNICAL_HOLD`** — everything else: a stale main, a packet that could not be built, an
  unreadable source, a CI infrastructure failure, mergeability, a migration collision, source
  bookkeeping. The Host recovers or retries by itself.

A repeated BLOCKER is not handed to the user because it repeated. The repair re-analyses the
problem independently and takes another approach, and the audits run again.

A technical hold that keeps returning on the same state ends the run as
`TECHNICAL_HOLD_EXHAUSTED`. That is a circuit breaker on cost, which is itself one of §2 D, and a
report. It asks for no decision.

### 4.1 What a BLOCKER is

The first five classes were decided by the user on 2026-10-01 in PR #165 comment `5921331154`.
The sixth was added separately by the PR #174 rules calibration; it is not attributed to that
comment. An audit BLOCKER is only a material defect introduced or exposed by the changed code on a
reachable path for the scoped operation:

1. a real possibility of data damage;
2. a real possibility of a duplicate registration or a wrong external transmission;
3. a security hole or a credential leak;
4. a core function that does not actually work;
5. a test or CI failure caused by a real code defect;
6. an actual bypass or disabling of a core safety gate required by that operation.

None of these is a BLOCKER: a hypothetical concern without a credible reachable path; a
defence-in-depth improvement; an unrelated pre-existing issue the change does not worsen; a
difference in document wording; the same meaning phrased differently across rule files; citation
format; a non-essential difference in how the packet is built; a README, ADR or ROADMAP wording
mismatch; a suggestion that something could be made more rigorous; a request to prove an
already-decided product requirement in more detail. An auditor passes those and records them as
notes. Notes are never repaired as blockers and never hold a merge.

The six classes are exhaustive. A finding that names none of them is a NOTE or, when evidence is
missing, a technical hold; it is not upgraded to BLOCKER because it repeated.

### 5. No human classification

The marker-and-classification gate is removed from the packet.

- A slice declares its evidence by citing it in its declaration: its PR body, and the Host's
  slice specification or remediation authorization when one exists. The packet's sources are the
  comments, reviews and review comments the declaration cites, read at their current body and bound
  by their SHA-256.
- A citation names the kind of its source as well as its id, and resolves only to a source of
  that kind.
- Canon is read at the audited base: the canon that binds before the slice. What the slice
  changes in it is in the diff.
- Every packet carries the Host's baseline canon whatever the declaration cites: the roadmap, the
  current milestone, the execution-safety rule and the operating-authority rule. The declaration
  only adds the further canonical documents the slice implements; it cannot leave the baseline
  out. An auditor that needs canon the packet does not carry returns `INSUFFICIENT`; it never
  passes on a diff alone, so citing less never helps a slice pass.
- A citation that cannot be read is a technical hold, never a smaller packet: declared evidence
  does not disappear silently.
- A marker is provenance. It never makes a source a packet input and never holds a packet.
- The declaration chooses **evidence, never authority**. A user's product decision binds through
  the canonical documents — the ROADMAP, the ADRs, the architecture, the contracts and the
  acceptance records — because a decision is binding only once it is reflected there (rule §1.1).
  Every audit judges the diff against that canon at the audited base, whatever the declaration
  cites. So a declaration that does not cite a comment removes no decision of the user's: the
  decision is in the canon, where the auditors read it, or it is not binding yet. A diff that
  goes beyond the canon is `HUMAN_DECISION_REQUIRED` (§2) whatever its declaration cites or
  omits, and the user's own stop on a PR is the hold file, which no declaration can affect.
- No `scope: PR #<N>` record with `required:` / `evidence-only:` / `excluded:` sections is read,
  required or requested.
- Existing `[OWNER-AMENDMENT]` and `[ARCHITECT-INSTRUCTION]` records are kept as history. They are
  never deleted or edited. Among them: `5907095955`, `5909645067` and the PR #147 bootstrap record
  `5876525801`. (Naming them here does not cite them: a citation is read from a slice's
  declaration, never from a document in its diff.)
- No new `[OWNER-AMENDMENT]` is created, and the user is never asked to post one.

### 6. What is not relaxed

Removing the per-step approval is not self-authorization, and it lowers no mechanical check.

- An agent that finds the work needs a product feature or policy outside the canonical documents
  does not build it. It stops with `HUMAN_DECISION_REQUIRED`.
- Kept exactly for HIGH_RISK: the exact-HEAD audit; the Audit Packet and its digest; the two-part
  audit identity; the GPT audit and the independent Claude audit; the same-identity DUAL PASS;
  `evidence_seen` coverage; the PASS-only cache; FULL CI; the pre-merge packet regeneration; the
  fresh-main check; MERGE_GUARD; the merge with `expected_head_sha`; POST_MERGE_VERIFY.
- BASIC and PROVIDER_ZERO use rule §14.2. They do not claim that their lighter validation is a
  HIGH_RISK audit. A mixed change uses its highest credible risk; uncertainty alone is not a tier.
- Transitional V3 still requires a current-main `DUAL_PASS` baseline before auto-next. After a
  lower-tier merge, `MAIN_NOT_DUAL_PASS_AUDITED` is a technical hold: a supervising agent, not the
  Host itself, runs the existing standalone full-main auditor with its default DELTA/FULL
  self-escalation and without `-ForceFull`, then resumes the Host only after DUAL PASS. This is not a
  merge prerequisite or HIGH_RISK proof for the lower-tier PR. Without a supervising agent the Host
  waits; BLOCKED, INSUFFICIENT, another technical hold or main movement starts no next work and uses
  existing bounded recovery. A Host-managed HIGH_RISK merge already writes the current-main baseline
  in its post-merge audit, so it needs no duplicate standalone audit before auto-next.
- Kept exactly: the authority write guard. No automated actor writes a marker-first body.
- Kept exactly: the gates on side-effecting LIVE writes, `UNKNOWN`/replay, credential and identity
  isolation, sensitive-data export and irreversible destructive operations — `CLAUDE.md` §7,
  ADR-0014 (the never-resend and positive-only reconcile rules among them), ADR-0018.

For a side-effecting mutation to which ADR-0018 applies, visual acceptance, restore and retention
are one readiness check on the final main immediately before the bounded LIVE action, not separate
user approvals. They are not mutation gates for a routine read-only operation and are not repeatedly
re-recorded after ordinary provider-zero merges; before an applicable final closeout their state is
simply not current for LIVE and therefore authorizes nothing (rule §14.5.1).

### 7. Relation to ADR-0020

ADR-0020 gave one standing authorization, for provider-zero slices next in ROADMAP order, and left
every other step to a new user decision. This ADR widens the standing authorization to every step
that is not one of §2. ADR-0020's conditions that are safety conditions stay for a side-effecting
LIVE mutation or canary and its material residual-risk acceptance. A routine read-only provider
operation is no longer promoted to §2 D only because it is a provider call. Its condition that each
slice is its own PR stays; audit strength follows rule §14.2.

Where ADR-0020, a roadmap line or a rule file says a step needs "its own authorization", read it
with §1: the standing authority is that authorization unless the step is one of §2.

### 8. Default mode and tracks

```text
auto_merge = true
auto_next  = true
```

- A merge still needs every MERGE_GUARD condition on the same exact HEAD and packet digest.
- `auto_next` never runs a §2 D action by itself.
- One Host runs one track. Parallel tracks run as separate Host directories, each with its own
  state, worktrees, PR, packet, audit identity, CI and merge. They never share a PR. A hold on one
  track does not stop another. When one track's merge moves main, the others bring their branch up
  to date, renumber a colliding migration, test and re-audit, without asking.

## Invariants

```text
OA-01  The user decides product features, product behaviour and protected side-effecting or irreversible actions. Nothing else stops a loop for the user.
OA-02  An implementation choice for canonically defined work is the agent's, and is verified at the risk tier of rule §14.2.
OA-03  A stop is HUMAN_DECISION_REQUIRED or TECHNICAL_HOLD, by category. A count never makes a stop human.
OA-04  No packet depends on a human classification record. A marker is provenance and never a hold.
OA-05  Existing authority records are history: never deleted, never edited. No new [OWNER-AMENDMENT] is created or requested.
OA-06  No automated actor writes a marker-first body.
OA-07  Work that needs a product feature or policy outside the canonical documents is not built: HUMAN_DECISION_REQUIRED.
OA-08  HIGH_RISK keeps the exact-HEAD audit, packet digest, same-identity DUAL PASS, evidence_seen, PASS-only cache, FULL CI, pre-merge regeneration, MERGE_GUARD, expected_head_sha and POST_MERGE_VERIFY; lower tiers never present themselves as that proof.
OA-09  No protected execution action of rule §7.2 is ever started by auto_merge or auto_next.
OA-10  Tracks are separate: their own branch, worktree, PR, packet, audit identity and merge. One track's hold never stops another.
```

## Consequences

- The user is asked only what §2 lists. The process no longer asks for amendments, classification
  comments, repair approvals, merge approvals or the start of the next slice.
- An agent decides more by itself. What it decides is bounded by the canonical documents and
  checked at the risk tier of rule §14.2; what it may not decide is a closed list the Host enforces.
- A wrong implementation choice is caught by an audit, not by a question. A loop can therefore
  spend audit and repair cycles on a problem a person would have resolved with one answer. The
  circuit breaker of §4 bounds that cost.
- The audit input of a slice is its diff, its PR body, which is always a required source, the
  Host's slice specification or remediation authorization when one exists, the Host's baseline
  canon at the audited base, and the evidence and canonical documents the whole declaration
  cites. Choosing what to cite is the agent's work,
  and it cannot make an audit easier: an auditor that needs evidence or canon the packet does not
  carry returns `INSUFFICIENT`, never a PASS on the diff alone.
