## 14. Operating authority

Decided by the user on 2026-09-30 and recorded in
`documents/decisions/adr/0022-agent-operating-authority.md`. The control loop that applies it is
`documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md`.

### 14.1 Who decides

```text
product decision          = the user
implementation decision   = the agent
verification              = GPT audit + independent Claude audit, on the exact HEAD
```

When the user starts a track of work, that start authorizes all of the work the canonical documents
already define for it, until it is done. The user is not a per-step approver.

### 14.2 The loop

```text
next canonical work → implement → exact HEAD → Audit Packet
→ GPT audit + independent Claude audit
→ BLOCKER: repair → new HEAD → new packet → both audits again
→ DUAL PASS → READY → FULL CI → GREEN → MERGE_GUARD
→ merge with expected_head_sha → POST_MERGE_VERIFY
→ fresh main → next canonical work
```

`auto_merge` and `auto_next` are on by default.

### 14.3 Never ask the user

Do not ask the user, and do not ask the user to post anything on GitHub, about:

- implementation, refactoring, internal architecture;
- schema, migration or endpoint design for an already-approved feature;
- tests, lint, types, imports, paths, repository rules;
- migration numbering, merging the current main, a mechanical conflict;
- repairing an audit BLOCKER;
- packets, source discovery, evidence bookkeeping, digests;
- CI, a stale HEAD, a re-audit, FULL CI, MERGE_GUARD;
- the merge, POST_MERGE_VERIFY, or starting the next canonical work.

No `[OWNER-AMENDMENT]` and no classification comment is requested. Existing ones are history.

### 14.3.1 What an audit may block on

Only real data damage, a real duplicate registration or wrong external transmission, a security or
credential leak, a core function that does not work, or a test or CI failure from a real code
defect (ADR-0022 §4.1). Wording, citation format, non-essential packet detail, "could be more
rigorous" and "prove the decided requirement in more detail" are notes, not blockers.

### 14.4 Stop for the user only for

- a product feature the canonical requirements do not contain;
- a user-visible behaviour, UX or policy with several real product directions that no canonical
  text decides;
- a change beyond the user's existing requirements;
- a real external action: a LIVE provider mutation, a real provider or marketplace call, a real
  canary, a real supplier or provider read whose acceptance needs its own grant, the acceptance of
  the residual risk of such an action, a cost, a transfer of real data to an external service, a
  destructive operation, a force-push, a branch deletion;
- a hold the user placed on a PR themselves.

Do not widen the product to avoid a question: work that needs a feature or policy outside the
canonical documents is not built.

### 14.5 Reading the other rules

§7 (execution safety) is unchanged: every approval it lists is a real external or destructive
action and stays the user's.

Where §1, §10, the roadmap or an ADR asks for a separate authorization, an architect resolution or a
stop before an **implementation** matter, §14.1 governs: decide it, state the choice, and let the
audits verify it. Where they ask for it before one of the actions of §14.4, they stand.

A technical problem is recovered or retried, never handed over. A repeated BLOCKER is re-analysed
and repaired another way.

### 14.6 Worktrees

Worktrees, clones, audit packets, browser profiles, test temporaries and logs are never created on
the Desktop. Worktrees live under `%USERPROFILE%\ICBM-worktrees\<track>\`. After a merge and its
POST_MERGE_VERIFY, a worktree that is clean, with nothing uncommitted and nothing unpushed, is
removed and pruned. An active worktree is never removed.
