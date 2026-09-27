# ADR-0020 — ROADMAP standing authorization for provider-zero slices

Status: **ACCEPTED** — decided by the repository owner (the user) on 2026-09-27, on canonical main
`9e5b70198d92131a143cf82ee5c80cfa64f23bb8`, whose post-merge audit closed GPT PASS + Claude PASS with
no blocker. This is a governance contract: docs only, runtime-zero.
- **Its authority becomes effective only after this exact PR is audited (GPT exact-head),
  independently cross-audited (Claude) and merged.**
- It amends ADR-0014 and ADR-0018 **by amendment notes only**. Their text, their rulings and their
  invariants (M5-xx, G3-xx) are neither rewritten nor renumbered.

Decision owner: the user (repository owner, product decisions and protected approvals — `CLAUDE.md`
§1). Recorded by Claude Code.

---

## Context

The canonical documents gate every remaining step behind its own authorization:

- `ROADMAP.md` §14 and §14.1: each later slice / each gap "needs its own authorization";
- `CLAUDE.md` §11: none of the M5 gaps "may be closed without its own authorization";
- ADR-0018 §6.1 and §12: CREATE and the positive-only reconcile path "each need their own
  separately authorized adoption slice";
- ADR-0014 §28.8: the amendment itself authorizes no runtime, schema, migration or endpoint adoption.

Until now each such authorization was a new, one-off user or architect decision. The work between
them — select the next roadmap step, implement it provider-zero, audit it, merge it — was already
contract-bound and audited, yet stopped at every step only to ask for permission.

## Decision

### 1. The rule that stays

**Every slice still needs its own authorization, and every slice is still its own PR.** This ADR
does not remove, merge or weaken any "separately authorized", "own authorization" or "one at a time"
rule in the canonical documents. It defines one standing way to satisfy it.

### 2. The standing authorization

A slice is **authorized by this standing authorization** — with no further user decision — when
**all** of the following hold on the exact canonical main it starts from:

1. **Next in canonical order.** It is the next incomplete step in the order of `ROADMAP.md` §12 and
   §14, read fresh from that exact main. No earlier selection, lookahead or cached plan is reused.
2. **Already decided.** The canonical documents (`ROADMAP.md`, `docs/ARCHITECTURE.md`, `docs/adr/*`,
   `docs/acceptance/*`) already define its scope, its safety invariants and its owner boundary.
3. **Provider-zero.** It adds local runtime, tests and docs only. It makes no real provider or
   marketplace call.
4. **Endpoint adoption only as already contracted.** It may adopt an endpoint in code (request,
   response, error classification, typed adapter) only where a canonical contract already defines
   that adoption and its limits. Adoption is never a call: execution stays `DRY_RUN`, LIVE stays
   refused, and the provider-evidence verdict is neither overturned nor re-decided.
5. **Schema only as already contracted.** It may add a schema change or migration only where a
   canonical contract already decides it concretely. A table, column, enum or data model that the
   contract leaves undecided is not authorized by this ADR.
6. **Its own PR, one at a time.** It is one slice in one PR, never merged with another slice and
   never run in parallel with another slice.
7. **The full gate.** The PR passes CI, the GPT exact-head audit and the independent Claude
   cross-audit (DUAL PASS) and is merged only under the exact-HEAD/main freshness merge guard. After
   the merge, the new main passes its post-merge audit before the next slice is selected — again
   fresh from that new main.

The record of each use is durable and per slice: the selection on the exact main (the slice, the
canonical sources, the allowed paths and whether a schema change is contract-decided), the
independent cross-check of that selection, the PR itself and its two audits.

### 3. What it never authorizes — these stay explicit user (and, where stated, architect) decisions

- any **real provider or marketplace call**;
- any **LIVE** switch or LIVE grant use;
- any **real canary** (ADR-0018 §12 area 5);
- the **residual-risk acceptance** of ADR-0018 §6.1 / ADR-0014 §28.7;
- any **new architecture or policy decision**;
- a step on which **the canonical documents conflict**;
- a next step whose **scope is unclear**;
- a **schema or data model design** that no canonical contract has decided;
- any **scope expansion** beyond the slice's canonical definition;
- every approval of `CLAUDE.md` §7.2.

A slice that needs any of these stops before implementation and asks the user, stating the exact
condition and its canonical source.

### 4. The current order under this ADR

At the canonical main this ADR was decided on, the next provider-zero slices of `ROADMAP.md` §14
item 1 (M5) are, in order and each as its own PR:

| order | slice | scope | not in scope |
| --- | --- | --- | --- |
| 1 | **CREATE adoption** — `SMARTSTORE_PRODUCT_CREATE_V2` | the official CREATE request/response contract; the error classification that separates a definitive rejection from an ambiguous outcome (ADR-0018 §6.1); `UNKNOWN` is never resent (ADR-0014 §28, G3-07); the runtime, tests and docs of that adoption, including the endpoint's adoption status | any SmartStore call; LIVE; canary; residual-risk acceptance; SEARCH; M6 |
| 2 | **SEARCH positive-only reconcile adoption** — `SMARTSTORE_PRODUCT_SEARCH` | limited to the positive reconcile of ADR-0014 §28.2–§28.4 and its exact request, response and pagination contract; a separate slice after CREATE | any SmartStore call; LIVE; canary; residual-risk acceptance; zero-result absence inference |

After these, the steps that remain before M5 acceptance — the residual-risk acceptance, the bounded
LIVE grant use, the real canary and the M5 acceptance run — are all outside this standing
authorization (§3).

An adoption slice updates the adoption status of **its own endpoint only** — in
`docs/platforms/smartstore/ENDPOINT_MATRIX.md`, `docs/acceptance/M5.md` and the invariant text and
contract-test pins that record that endpoint as `NOT_ADOPTED` — by amendment note, never by silent
rewrite. It does not change the `INSUFFICIENT` verdict, the never-resend rule, the rule that a
zero-result lookup never proves absence, or the canary's `BLOCKED` state, which keeps every other
condition of ADR-0018 §6 and §10.

## Invariants

```text
SA-01  every slice still needs its own authorization and is its own PR; this ADR only defines a standing way to meet that rule
SA-02  a slice is authorized here only when it is the next step in ROADMAP order read fresh from the exact main, with no cached selection
SA-03  a slice is authorized here only when the canonical documents already decide its scope, safety invariants and owner boundary
SA-04  a slice authorized here is provider-zero: no real provider or marketplace call, execution stays DRY_RUN
SA-05  endpoint adoption is authorized here only where a canonical contract already defines it; adoption is never a call and never re-decides the provider-evidence verdict
SA-06  schema or migration is authorized here only where a canonical contract already decides it concretely
SA-07  LIVE, a real canary, the residual-risk acceptance, new architecture or policy, conflicting canon, unclear scope, undecided data model and scope expansion always stop for the user
SA-08  each slice passes CI, the GPT exact-head audit and the independent Claude cross-audit, merges only under the exact-HEAD/main guard, and the new main is audited before the next selection
SA-09  CREATE adoption and SEARCH positive-only reconcile adoption are two separate slices, CREATE first
```

## Consequences

- The next provider-zero slice proceeds without a new per-slice user document; everything listed in
  §3 still stops for the user.
- The audits of each slice judge it against its canonical scope; a scope violation or a weakened
  safety rule is a blocker as before.
- Reverting to per-slice user decisions needs a superseding ADR.

## References

- `ROADMAP.md` §12, §14, §14.1, §14.2
- `CLAUDE.md` §1, §7.2, §11
- ADR-0014 §17.2, §28 (amendment note at §28.8)
- ADR-0018 §6, §6.1, §10, §12 (amendment note at §12)
- `docs/platforms/smartstore/ENDPOINT_MATRIX.md`, `docs/acceptance/M5.md` §9
