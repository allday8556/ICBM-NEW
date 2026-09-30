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
- **D.** a real external action:
  - a LIVE provider mutation, a real provider or marketplace call, a real canary;
  - a real supplier or provider read whose acceptance needs its own grant;
  - accepting the residual risk of such an action;
  - a payment or a cost;
  - sending real data to an external service;
  - a destructive operation, a force-push, a branch deletion;
- **E.** a hold the user placed on a PR themselves (the owner's hold file). It is the user's own
  stop, so only the user lifts it.

The Host calls this `HUMAN_DECISION_REQUIRED`. A technical choice is never one of them, and a stop
that names none of these categories is not the user's, whatever it calls itself.

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

Those choices are verified, not approved: GPT and Claude audit every one of them on the exact HEAD.

| role | owner |
| --- | --- |
| product decision | the user |
| implementation decision | the agent |
| verification | GPT and Claude |

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

### 5. No human classification

The marker-and-classification gate is removed from the packet.

- A slice declares its evidence by citing it in its PR body. The packet's sources are the
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
- Kept exactly: the exact-HEAD audit; the Audit Packet and its digest; the two-part audit identity;
  the GPT audit and the independent Claude audit; the same-identity DUAL PASS; `evidence_seen`
  coverage; the PASS-only cache; FULL CI; the pre-merge packet regeneration; the fresh-main check;
  MERGE_GUARD; the merge with `expected_head_sha`; POST_MERGE_VERIFY.
- Kept exactly: the authority write guard. No automated actor writes a marker-first body.
- Kept exactly: every provider, LIVE and destructive-operation gate — `CLAUDE.md` §7, ADR-0014
  (the never-resend and positive-only reconcile rules among them), ADR-0018.

### 7. Relation to ADR-0020

ADR-0020 gave one standing authorization, for provider-zero slices next in ROADMAP order, and left
every other step to a new user decision. This ADR widens the standing authorization to every step
that is not one of §2. ADR-0020's conditions that are safety conditions stay: a real provider call,
LIVE, a real canary and the residual-risk acceptance are §2 D. Its condition that each slice is its
own PR, audited by both auditors, stays.

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
OA-01  The user decides product features, product behaviour and real external actions. Nothing else stops a loop for the user.
OA-02  An implementation choice for canonically defined work is the agent's, and is verified by both auditors on the exact HEAD.
OA-03  A stop is HUMAN_DECISION_REQUIRED or TECHNICAL_HOLD, by category. A count never makes a stop human.
OA-04  No packet depends on a human classification record. A marker is provenance and never a hold.
OA-05  Existing authority records are history: never deleted, never edited. No new [OWNER-AMENDMENT] is created or requested.
OA-06  No automated actor writes a marker-first body.
OA-07  Work that needs a product feature or policy outside the canonical documents is not built: HUMAN_DECISION_REQUIRED.
OA-08  The exact-HEAD audit, the packet digest, the same-identity DUAL PASS, evidence_seen, the PASS-only cache, FULL CI, the pre-merge regeneration, MERGE_GUARD, expected_head_sha and POST_MERGE_VERIFY are unchanged.
OA-09  No provider, LIVE, canary, cost, real data transfer or destructive action is ever started by auto_merge or auto_next.
OA-10  Tracks are separate: their own branch, worktree, PR, packet, audit identity and merge. One track's hold never stops another.
```

## Consequences

- The user is asked only what §2 lists. The process no longer asks for amendments, classification
  comments, repair approvals, merge approvals or the start of the next slice.
- An agent decides more by itself. What it decides is bounded by the canonical documents and
  checked twice on every HEAD; what it may not decide is a closed list the Host enforces.
- A wrong implementation choice is caught by an audit, not by a question. A loop can therefore
  spend audit and repair cycles on a problem a person would have resolved with one answer. The
  circuit breaker of §4 bounds that cost.
- The audit input of a slice is its diff, its PR body, which is always a required source, the
  Host's baseline canon at the audited base, and the evidence and canonical documents that body
  cites. Choosing what to cite is the agent's work,
  and it cannot make an audit easier: an auditor that needs evidence or canon the packet does not
  carry returns `INSUFFICIENT`, never a PASS on the diff alone.
