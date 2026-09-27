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
| 1 | **CREATE adoption** — `SMARTSTORE_PRODUCT_CREATE_V2` — **LANDED** (see the record note below) | the official CREATE request/response contract; the error classification that separates a definitive rejection from an ambiguous outcome (ADR-0018 §6.1); `UNKNOWN` is never resent (ADR-0014 §28, G3-07); the runtime, tests and docs of that adoption, including the endpoint's adoption status | any SmartStore call; LIVE; canary; residual-risk acceptance; SEARCH; M6 |
| 2 | **SEARCH positive-only reconcile adoption** — `SMARTSTORE_PRODUCT_SEARCH` | limited to the positive reconcile of ADR-0014 §28.2–§28.4 and its exact request, response and pagination contract; a separate slice after CREATE | any SmartStore call; LIVE; canary; residual-risk acceptance; zero-result absence inference |

> **Record note — slice 1 landed.** The CREATE adoption slice was implemented under this standing
> authorization, provider-zero, as its own PR. `SMARTSTORE_PRODUCT_CREATE_V2` is `ADOPTED` under the
> contract frozen in `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4.3, its outcome classification
> in `ERRORS.md` §15.1.1, and the amendment notes in ADR-0014 §17.2 and ADR-0018 §6.1. It made no
> provider call, changed no schema, and closed no other condition: the verdict stays `INSUFFICIENT`,
> execution stays `DRY_RUN`, `product_registration.write` stays `UNVERIFIED`, `docs/acceptance/M5.md`
> stays `PENDING`, and the canary stays `BLOCKED`. **Slice 2, the SEARCH positive-only reconcile
> adoption, is the next step in this order** and is still its own separately authorized PR (SA-09).

After these two, the **mandatory pre-canary prerequisites that no slice has closed** stay ahead of
any canary: the mutation-stage prerequisites of ADR-0018 §10 and the read-back success proof of
ADR-0014 §11. This ADR authorizes none of them, and none may be skipped. Neither contract fixes an
order among them — each refuses on its own — so they take no numbered position in the order above:

| still-missing prerequisite | why it is mandatory | why this ADR does not authorize it |
| --- | --- | --- |
| the **production ASSET sender** — `app/container.py` wires `UnwiredAssetSender`, which declares the adopted wire endpoint and refuses every send (`LIVE_SENDER_NOT_WIRED`) | `ASSET_MUTATION_READY` is a mandatory send-time layer (ADR-0018 §10, G3-19) and stays `BLOCKED` while no ASSET sender is wired (ADR-0018 §10, Consequences) | a sender that transmits to the provider is not provider-zero and no canonical contract decides its boundary, so §2.3 is not met and §3 stops it for the user |
| the **durable canary-eligibility owner** (ADR-0018 §5) — `CANARY_NON_REGULATED`; `app/live/proofs.py` has no owner and answers unproven | both stages require `CANARY_NON_REGULATED` (ADR-0018 §10, G3-13); without that proof the canary stays `BLOCKED`, and an operator assertion is never it | the eligibility record's data model is explicitly undecided (ADR-0018 §13), so §2.5 is not met and §3 stops it for the user |
| the **authoring-revision owners** for the category mapping and the detail composition (ADR-0014 §27) — the durable target policy holds both as `null` (ADR-0015 §2), so the candidate preflight answers `AUTHORING_REVISIONS_UNOWNED` (`app/register/preparation.py`) | each stage's own gate is a mandatory requirement (ADR-0018 §10): the ASSET stage needs a candidate preflight `READY`, the CREATE stage a final preflight `READY` and a `PREPARED` Intent. While either revision is unowned no unit is ever `READY`, freezing stays fail-closed, and no Snapshot and no Intent can exist — so both stages stay `BLOCKED` on this alone | ADR-0014 §27 records real owners for both revisions as "a later, separately authorized decision" and no canonical contract decides their data model, so §2.3 and §2.5 are not met and §3 stops it for the user |
| the **executable committed-session read-back** (ADR-0014 §11) — `app/container.py` passes `bearer=lambda: None`, so `SmartStoreReadback.available()` is `False` and `verify` refuses; the canary readiness reports `READBACK_EXECUTABLE` with `READBACK_SESSION_NOT_WIRED` | read-back is the success proof (ADR-0014 §11): a CREATE that cannot be read back is never `CONFIRMED`, so a canary run without it could only end `UNKNOWN` or unverified — and `docs/acceptance/M5.md` §6 records it as not proven | a committed provider session that actually reads back is not provider-zero, so §2.3 is not met and §3 stops it for the user |
| a **read-back comparison that proves published state** (ADR-0014 §11) — the adopted normalizer's canonical form carries no published state, so `proves_published_state()` is `False` and execution refuses with `REGISTER_PUBLISHED_STATE_UNPROVEN`; the canary readiness reports `PUBLISHED_STATE_PROVABLE` as unproven | published state is an `EXACT` comparison class of ADR-0014 §11, so without it no critical-field comparison can pass and no canary can be confirmed rather than invented | no canonical contract decides which endpoint content proves the field; it becomes `True` only through a separately authorized adoption slice that proves it, so §2.3 and §2.5 are not met and §3 stops it for the user |

Their order relative to each other is part of the user's decision on each; all of them precede any
canary.

Only then do the steps that remain before M5 acceptance — the residual-risk acceptance, the bounded
LIVE grant use, the real canary (ADR-0018 §12 area 5, permitted "only after every prerequisite is
green") and the M5 acceptance run — follow, and they are all outside this standing authorization
(§3). Nothing here shortens that remaining work: every other requirement of ADR-0018 §10 — the
stage's grant, the released protected-write brake, a current restore proof, evidence-retention
readiness, a recorded visual acceptance at the accepted SHA, the durable ASSET upload-attempt owner
and leaving `M0_DRY_RUN_ONLY` — keeps its own condition and its own decision.

> **Correction note (post-merge full audit of main `a523c55add2b`).** The paragraph and the second
> table above replace an earlier sequence that named only the residual-risk acceptance, the bounded
> LIVE grant use, the real canary and the M5 acceptance run, and so omitted two mandatory ADR-0018
> §10 prerequisites. This correction grants nothing: both prerequisites are recorded as still
> missing and as user decisions (§3, SA-10), and no invariant of ADR-0014 or ADR-0018 is changed.
>
> **Correction note (post-merge full audit of main `a10e4b79dbd3`).** The second table's third row
> — the **authoring-revision owners** of ADR-0014 §27 — was added because the table listed only two
> prerequisites while a third, each stage's own preflight gate (ADR-0018 §10), cannot be met at all
> while the category-mapping and detail-composition revisions have no owner. This correction grants
> nothing either: the row records a prerequisite that is still missing and is a user decision (§3,
> SA-10), it closes no gap, and it changes no invariant of ADR-0014, ADR-0015 or ADR-0018.
>
> **Correction note (post-merge full audit of main `cfb0aa4f3af1`).** The second table's last two
> rows — the **executable committed-session read-back** and the **read-back comparison that proves
> published state** (ADR-0014 §11) — were added because the table listed only the ADR-0018 §10
> stage prerequisites while `docs/acceptance/M5.md` §6 already recorded both of these as not proven,
> and a CREATE that cannot be read back and compared is never `CONFIRMED`. The paragraph above the
> table was widened from "mutation-stage prerequisites of ADR-0018 §10" to the pre-canary
> prerequisites of ADR-0018 §10 **and** ADR-0014 §11 for the same reason. This correction grants
> nothing: both rows record prerequisites that are still missing and are user decisions (§3, SA-10),
> they close no gap, and they change no invariant of ADR-0014 or ADR-0018.

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
SA-10  §4's remaining-work order never omits a mandatory pre-canary prerequisite of ADR-0018 §10 or ADR-0014 §11; the production ASSET sender, the durable canary-eligibility owner, the ADR-0014 §27 authoring-revision owners, the executable committed-session read-back and a read-back comparison that proves published state are still missing, are not authorized here, and the canary stays BLOCKED until every condition of ADR-0018 §6 and §10 and the ADR-0014 §11 success proof is green
```

## Consequences

- The next provider-zero slice proceeds without a new per-slice user document; everything listed in
  §3 still stops for the user.
- The audits of each slice judge it against its canonical scope; a scope violation or a weakened
  safety rule is a blocker as before.
- §4's remaining-work order is read against ADR-0018 §10, never instead of it. A prerequisite found
  missing from §4 is corrected in §4 before the next slice is selected, never worked around.
- Reverting to per-slice user decisions needs a superseding ADR.

## References

- `ROADMAP.md` §12, §14, §14.1, §14.2
- `CLAUDE.md` §1, §7.2, §11
- ADR-0014 §17.2, §27, §28 (amendment note at §28.8)
- ADR-0015 §2 (the target policy holds both authoring revisions as `null`)
- ADR-0018 §6, §6.1, §10, §12 (amendment note at §12)
- `docs/platforms/smartstore/ENDPOINT_MATRIX.md`, `docs/acceptance/M5.md` §9
