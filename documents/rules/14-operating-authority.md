## 14. Operating authority

Decided by the user on 2026-09-30 and recorded in
`documents/decisions/adr/0022-agent-operating-authority.md`. The control loop that applies it is
`documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md`.

### 14.1 Who decides

```text
product decision          = the user
implementation decision   = the agent
safety/correctness verification = the validation tier of §14.2
```

When the user starts a track of work, that start authorizes all of the work the canonical documents
already define for it, until it is done. The user is not a per-step approver.

This is the single operational role rule. Other rules, ADRs and Host documents reference this
section rather than restating a different assignment.

### 14.2 Risk-based validation

Use the least strong tier that covers the change. A lower tier never authorizes an action reserved
to the user by §14.4, and a mixed change uses its highest applicable tier.

| tier | scope | required validation |
| --- | --- | --- |
| **BASIC** | documentation, infrastructure and a simple reversible internal-only change with no external-write, `UNKNOWN`, credential, identity or destructive-data effect | focused/default tests and applicable lint/type checks; the selected CI scope must be green |
| **PROVIDER_ZERO** | feature or integration code tested without a real external side effect, including read-only/provider-zero paths and ordinary reversible migrations | functional tests plus CI; add review where the changed boundary, contract or complexity needs it |
| **HIGH_RISK** | a side-effecting LIVE write; `UNKNOWN`, replay or external identity handling; credential/secret handling; an irreversible/destructive data operation; or weakening/removal of a core safety gate | exact-HEAD Audit Packet, strong GPT audit plus independent Claude audit, same-identity DUAL PASS, FULL CI and MERGE_GUARD |

The Agent Host V3 strong path is the `HIGH_RISK` path. BASIC and PROVIDER_ZERO work uses the normal
PR and CI path and does not manufacture an exact-HEAD dual audit merely because the Host exists.
A mixed change uses its highest real risk. Uncertainty alone does not create HIGH_RISK: the
classification names a credible failure path. If the change cannot rule out an external side
effect, credential exposure, identity collision or irreversible damage, that credible path is
HIGH_RISK until separated or disproved.

**Transitional V3 auto-next baseline.** A BASIC or PROVIDER_ZERO PR may merge after only its own
tier's validation. The current V3 runtime nevertheless permits lookahead only when
`full-audit-baseline.json` says that the current main is `DUAL_PASS`. After a lower-tier merge moves
main, `run-lookahead-main-v1.ps1` therefore reports `NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED`. This is a
`TECHNICAL_HOLD`, not a merge prerequisite, a HIGH_RISK reclassification, or strong proof for
the lower-tier PR. A supervising agent, without asking the user, runs the existing
`run-full-audit-v1.ps1` with its default DELTA/FULL self-escalation policy and without `-ForceFull`.
On DUAL PASS it confirms `main=current main` and `status=DUAL_PASS` in the baseline, resumes the Host,
and lets lookahead select the next canonical work. BLOCKED, INSUFFICIENT, another technical hold or
main movement starts no next work and follows the existing retry, backoff and circuit-breaker rules.
A fully unattended Host with no supervising agent simply waits at that technical hold. When the Host
itself merged a HIGH_RISK PR, its existing post-merge full-main audit already establishes the current
main baseline, so no duplicate standalone audit runs before auto-next.

### 14.2.1 Repository-wide application

This classification applies to every repository activity: implementation, documentation, ADRs,
migrations, CI, PR review, manual work and any agent or automation. Agent Host is one consumer of
the rule, not its boundary.

Calling something "safety", "critical" or "production" does not raise its tier by itself. A gate
must name the protected asset, a credible failure path and the scoped operation it blocks. A
hypothetical risk or extra defence-in-depth measure is a NOTE unless the changed code makes that
failure reachable. Older or more detailed documents are read through this proportionality rule;
their concrete runtime invariants still apply to the actual operation they govern, but they do not
become universal PR or approval gates.

A non-mutating read-back, health check or lookup is not HIGH_RISK by default. User approval remains
required when the operation creates material cost, transfers personal or other non-public sensitive
data, changes provider state, or is itself inside an explicitly approved LIVE acceptance scope;
§7 owns those execution boundaries.

The strong loop is:

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

Only a material defect in the changed code on a reachable path may block: actual data damage, a
duplicate registration or wrong external transmission, a security or credential leak, a core
function that does not work, a test or CI failure from a real code defect, or an actual bypass of a
core safety gate required by the scoped operation (ADR-0022 §4.1). A hypothetical concern, a
defence-in-depth improvement, an unrelated pre-existing issue, wording, citation format,
non-essential packet detail, "could be more rigorous" and "prove the decided requirement in more
detail" are notes, not blockers. A NOTE is recorded but never triggers repair, re-audit or a merge
hold.

### 14.4 Stop for the user only for

- a product feature the canonical requirements do not contain;
- a user-visible behaviour, UX or policy with several real product directions that no canonical
  text decides;
- a change beyond the user's existing requirements;
- a protected execution action listed by §7.2;
- a hold the user placed on a PR themselves.

Do not widen the product to avoid a question: work that needs a feature or policy outside the
canonical documents is not built.

### 14.5 Reading the other rules

§7 owns the exact protected-action list. The Host's category names are an implementation encoding
of §7.2, not a second list. A routine read-only provider call, read-back, health check
or already-approved integration lookup with no mutation, sensitive-data export or material new cost
is an implementation/operation step, not a new product decision and not a separate approval.

Where §1, §10, the roadmap or an ADR asks for a separate authorization, an architect resolution or a
stop before an **implementation** matter, §14.1 governs: decide it, state the choice, and let the
audits verify it. Where they ask for it before one of the actions of §14.4, they stand.

A technical problem is recovered or retried, never handed over. A repeated BLOCKER is re-analysed
and repaired another way.

### 14.5.1 Final LIVE evidence

Visual acceptance, restore proof and retention proof remain mandatory where ADR-0018 makes them
causally relevant to the side-effecting mutation being opened. They are one final readiness check,
not three extra user approvals, and are established on the final main immediately before the
bounded LIVE action after the implementation line-up is complete. A read-only provider operation
does not owe mutation readiness. An ordinary BASIC or PROVIDER_ZERO merge may mean an old exact-main
proof no longer covers the new commit, but it does not require the proof to be run or recorded again
at that intermediate commit. Until an applicable final LIVE closeout, the truthful state is simply
"not current for LIVE". No code path may interpret that as permission to send.

The actual visual, restore and retention proof and durable record are performed once on the final
LIVE main. This timing rule never removes focused tests or CI when code implementing those controls
changes.

### 14.6 Worktrees

Worktrees, clones, audit packets, browser profiles, test temporaries and logs are never created on
the Desktop. Worktrees live under `%USERPROFILE%\ICBM-worktrees\<track>\`. After a merge and its
POST_MERGE_VERIFY, a worktree that is clean, with nothing uncommitted and nothing unpushed, is
removed and pruned. An active worktree is never removed.
