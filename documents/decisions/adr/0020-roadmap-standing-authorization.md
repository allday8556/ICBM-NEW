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

> **Amendment note (ADR-0022, 2026-09-30).** The standing authorization is widened by
> `documents/decisions/adr/0022-agent-operating-authority.md` §7: a step that is not one of the
> user's decisions (ADR-0022 §2) is authorized by the standing operating authority, whether or not
> it meets every condition below. Under the 2026-10-01 calibration, rule §7.2 owns the active list
> of protected execution actions and rule §14.2 owns validation strength. A routine read-only
> provider call is not protected by name alone; the historical status tables below do not create a
> second approval or audit list.

---

## Context

The canonical documents gate every remaining step behind its own authorization:

- `documents/roadmap/ROADMAP.md` §14 and §14.1: each later slice / each gap "needs its own authorization";
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

1. **Next in canonical order.** It is the next incomplete step in the order of `documents/roadmap/ROADMAP.md` §12 and
   §14, read fresh from that exact main. No earlier selection, lookahead or cached plan is reused.
2. **Already decided.** The canonical documents (`documents/roadmap/ROADMAP.md`, `documents/architecture/ARCHITECTURE.md`, `documents/decisions/adr/*`,
   `documents/acceptance/*`) already define its scope, its safety invariants and its owner boundary.
3. **Provider-zero.** It adds local runtime, tests and docs without a real external side effect. A
   routine read-only provider call may be used when rule §7.1 allows it; a provider mutation stays
   outside this clause.
4. **Endpoint adoption only as already contracted.** It may adopt an endpoint in code (request,
   response, error classification, typed adapter) only where a canonical contract already defines
   that adoption and its limits. Adoption is never a call: execution stays `DRY_RUN`, LIVE stays
   refused, and the provider-evidence verdict is neither overturned nor re-decided.
5. **Schema only as already contracted.** It may add a schema change or migration only where a
   canonical contract already decides it concretely. A table, column, enum or data model that the
   contract leaves undecided is not authorized by this ADR.
6. **Its own PR, one at a time.** It is one slice in one PR, never merged with another slice and
   never run in parallel with another slice.
7. **The applicable gate.** The PR passes the risk tier selected by rule §14.2. PROVIDER_ZERO uses
   functional tests, CI and any review needed by its changed boundary; only HIGH_RISK uses the
   exact-HEAD DUAL PASS and strong merge guard. The next slice is selected from fresh main.

The record of each use is durable and per slice: the selection on the exact main (the slice, the
canonical sources, the allowed paths and whether a schema change is contract-decided), the
independent cross-check of that selection, the PR itself and its two audits.

### 3. What it never authorizes

- any protected execution action owned by rule §7.2, including opening or widening a bounded
  side-effecting **LIVE** mutation scope or real canary;
- the **residual-risk acceptance** of ADR-0018 §6.1 / ADR-0014 §28.7;
- any **new architecture or policy decision**;
- a step on which **the canonical documents conflict**;
- a next step whose **scope is unclear**;
- a **schema or data model design** that no canonical contract has decided;
- any **scope expansion** beyond the slice's canonical definition;
- every approval of rule §7.2.

A slice that needs any of these stops before implementation and asks the user, stating the exact
condition and its canonical source.

The detailed tables below record the repository state when this ADR was adopted. Their statements
that a provider session or provider call was outside this standing authorization are historical
scope notes, not an active rule that overrides §7.2 or §14.2.

### 4. The current order under this ADR

At the canonical main this ADR was decided on, the next provider-zero slices of `documents/roadmap/ROADMAP.md` §14
item 1 (M5) are, in order and each as its own PR:

| order | slice | scope | not in scope |
| --- | --- | --- | --- |
| 1 | **CREATE adoption** — `SMARTSTORE_PRODUCT_CREATE_V2` | the official CREATE request/response contract; the error classification that separates a definitive rejection from an ambiguous outcome (ADR-0018 §6.1); `UNKNOWN` is never resent (ADR-0014 §28, G3-07); the runtime, tests and docs of that adoption, including the endpoint's adoption status | any SmartStore call; LIVE; canary; residual-risk acceptance; SEARCH; M6 |
| 2 | **SEARCH positive-only reconcile adoption** — `SMARTSTORE_PRODUCT_SEARCH` | limited to the positive reconcile of ADR-0014 §28.2–§28.4 and its exact request, response and pagination contract; a separate slice after CREATE | any SmartStore call; LIVE; canary; residual-risk acceptance; zero-result absence inference |

After these two, the **mandatory pre-canary prerequisites that no slice has closed** stay ahead of
any canary: the mutation-stage prerequisites of ADR-0018 §10 and the read-back success proof of
ADR-0014 §11. This ADR authorizes none of them, and none may be skipped. Neither contract fixes an
order among them — each refuses on its own — so they take no numbered position in the order above:

| still-missing prerequisite | why it is mandatory | why this ADR does not authorize it |
| --- | --- | --- |
| ~~the **production ASSET sender** (ADR-0018 §10)~~ — **closed by its own slice** (ROADMAP §14 item 4, PR #196; ADR-0022 §7): `app/container.py` wires `SmartStoreAssetSender`, the adopted image upload behind the §3.4 attempt owner, **to the CONNECT owner's read-only committed bearer**: it can transmit exactly while CONNECT holds a proven current committed session in this process, and without one it is unavailable and refuses every send (`LIVE_SENDER_NOT_WIRED`) | it was mandatory because `ASSET_MUTATION_READY` is a mandatory send-time layer (ADR-0018 §10, G3-19) and stays `BLOCKED` while the ASSET sender cannot transmit (ADR-0018 §10, Consequences); with a session every other layer — `M0_DRY_RUN_ONLY` first — still refuses | it was outside this ADR because a sender that transmits to the provider is not provider-zero and no canonical contract decides its boundary, so §2.3 is not met and §3 stops it for the user. The wired adapter transmits nothing; giving it a committed session is that user decision — ADR-0022 §7 later made the wiring implementation, and a real upload stays the user's bounded LIVE decision |
| ~~the **durable canary-eligibility owner** (ADR-0018 §5)~~ — **closed by its own slice** (ADR-0018 §5.1; Issue #89 architect resolution `5910018106`; migration `0033`): the durable owner exists and `DurableStageProofs.canary_non_regulated` reads it for the exact lineage of each stage | it was mandatory because both stages require `CANARY_NON_REGULATED` (ADR-0018 §10, G3-13); without that proof the canary stays `BLOCKED`, and an operator assertion is never it. The layer is still unproven for every lineage that has no current `PROVEN_OUTSIDE` record | it was outside this ADR because the eligibility record's data model was explicitly undecided (ADR-0018 §13); that decision is the architect resolution above, never this standing authorization |
| ~~the **authoring-revision owners** for the category mapping and the detail composition (ADR-0014 §27)~~ — **closed by its own slice** (ADR-0014 §27.1; Issue #89 architect resolution `5907626428`; migration `0032`): the durable owner exists and the server stamps its current revisions into every target-policy revision it appends | it was mandatory because each stage's own gate is a mandatory requirement (ADR-0018 §10) and, while either revision was unowned, the candidate preflight answered `AUTHORING_REVISIONS_UNOWNED`, no unit was ever `READY` and no Snapshot and no Intent could exist. A target-policy revision appended before the owner existed still answers it until a new revision is appended: nothing is backfilled | it was outside this ADR because ADR-0014 §27 recorded real owners for both revisions as "a later, separately authorized decision" with no canonical data model; that decision is the architect resolution above, never this standing authorization |
| ~~the **executable committed-session read-back** (ADR-0014 §11)~~ — **closed by its own slice** (ROADMAP §14 item 4, PR #196; ADR-0022 §7): `app/container.py` passes the CONNECT owner's read-only committed bearer, so `SmartStoreReadback.available()` is `True` exactly while CONNECT holds a proven current committed session in this process; otherwise it is `False`, `verify` refuses and the canary readiness reports `READBACK_EXECUTABLE` with `READBACK_SESSION_NOT_WIRED` | it was mandatory because read-back is the success proof (ADR-0014 §11): a CREATE that cannot be read back is never `CONFIRMED`, so a canary run without it could only end `UNKNOWN` or unverified — and `documents/acceptance/milestones/M5.md` §6 derives it per process | it was outside this ADR because a committed provider session that actually reads back is not provider-zero, so §2.3 was not met and §3 stopped it for the user; ADR-0022 §7 made the wiring implementation and a committed-session read-back a routine read-only operation |
| ~~a **read-back comparison that proves published state** (ADR-0014 §11)~~ — **closed by its own slice** (ADR-0014 §11 amendment note; Issue #89 architect resolution `5915900049` D1): every CREATE projection registers the SmartStore channel with `channelProductDisplayStatusType = ON`, the Snapshot's own projection states the expected published state `SALE/ON` (`expected_published_state`), `proves_published_state()` is `True`, and only a read-back that carries exactly `SALE` and `ON` adds a published state to the comparison; the canary readiness reports `PUBLISHED_STATE_PROVABLE` as satisfied | it was mandatory because published state is an `EXACT` comparison class of ADR-0014 §11: without it no critical-field comparison can pass and no canary can be confirmed rather than invented. `SALE` with `SUSPENSION`, any other sale status and a missing or unreadable half still prove nothing, and execution still refuses with `REGISTER_PUBLISHED_STATE_UNPROVEN` | it was outside this ADR because which display status ICBM registers was a product decision with no owner; that decision is the architect resolution above, never this standing authorization. Being provable confirms no registration by itself: the executable committed-session read-back is still missing |

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
> stage prerequisites while `documents/acceptance/milestones/M5.md` §6 already recorded both of these as not proven,
> and a CREATE that cannot be read back and compared is never `CONFIRMED`. The paragraph above the
> table was widened from "mutation-stage prerequisites of ADR-0018 §10" to the pre-canary
> prerequisites of ADR-0018 §10 **and** ADR-0014 §11 for the same reason. This correction grants
> nothing: both rows record prerequisites that are still missing and are user decisions (§3, SA-10),
> they close no gap, and they change no invariant of ADR-0014 or ADR-0018.

> **Amendment note (authoring-revision owners slice; Issue #89 `5907626428`).** The second table's
> third row is closed: the architect resolution decided the owners' data model, and the slice
> implemented it provider-zero (ADR-0014 §27.1, migration `0032`). Four prerequisites are still
> missing — the production ASSET sender, the durable canary-eligibility owner, the executable
> committed-session read-back and a read-back comparison that proves published state — and this
> note grants none of them. Closing that row makes no unit `READY` by itself: every other preflight
> rule and every other ADR-0018 §10 requirement keeps its own condition.

> **Amendment note (canary-eligibility owner slice; Issue #89 `5910018106`).** The second table's
> second row is closed: the architect resolution decided the eligibility record's data model, and
> the slice implemented it provider-zero (ADR-0018 §5.1, migration `0033`). Three prerequisites
> are still missing — the production ASSET sender, the executable committed-session read-back
> and a read-back comparison that proves published state — and this note grants none of them.
> Closing that row proves no lineage by itself: `CANARY_NON_REGULATED` holds only for an exact
> lineage with a current `PROVEN_OUTSIDE` record, and every other ADR-0018 §10 requirement keeps
> its own condition.

> **Amendment note (production ASSET sender adapter).** The first row of the second table is
> **not closed**. Its text now states what exists: the adopted image upload is wired as the
> ASSET sender, provider-zero, with no committed session, so it is unavailable and transmits
> nothing. A sender that transmits is still missing and still the user's decision (§3), as is
> the committed session it would need. Three prerequisites are therefore still missing — the
> transmitting ASSET sender, the executable committed-session read-back and a read-back
> comparison that proves published state — and this note grants none of them.

> **Amendment note (published-state read).** The last row of the second table is **not closed**.
> Its text now states what exists: the origin read retains and reads the sale status and the
> SmartStore display status at their documented paths (ADR-0014 §11 amendment note; evidence
> Issue #89 `5911962320`). What is still missing is the explicit expectation — the display status
> a unit is registered with has no owner — so no published state can be proven and nothing can be
> confirmed. This note grants nothing.

> **Amendment note (published-state proof; Issue #89 `5915900049` D1).** The last row of the
> second table is closed, which supersedes the first sentence of the published-state read note
> above; the rest of that note stays true. The architect resolution decided the display status
> every registration carries (`ON`), and the slice owns it provider-zero in the CREATE projection
> and states the expected published state, `SALE/ON`, from the Snapshot's own projection
> (ADR-0014 §11 amendment note). Two prerequisites are still missing — the transmitting ASSET
> sender and the executable committed-session read-back — and this note grants none of them.
> Closing that row confirms no registration by itself: a read-back still needs a committed
> session, and every other ADR-0018 §10 requirement keeps its own condition.

> **Amendment note (committed-session bearer seam; ROADMAP §14 item 4, PR #196).** The second
> table's first and fourth rows are closed. ADR-0022 §7 made wiring the existing committed session
> implementation, and the slice gave the existing CONNECT owner a read-only bearer accessor as the
> one bearer source of the CREATE, read-back, SEARCH and ASSET seams — no new token owner and no
> new credential store. No row of the second table is still missing. Closing those rows grants
> nothing: a bearer exists only while CONNECT holds a proven current committed session, and
> `M0_DRY_RUN_ONLY`, the brake, the grant and every other ADR-0018 §10 requirement still refuse
> every mutation. The residual-risk acceptance, the bounded LIVE grant use, the real canary and
> the M5 acceptance run stay the user's decisions.

An adoption slice updates the adoption status of **its own endpoint only** — in
`documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md`, `documents/acceptance/milestones/M5.md` and the invariant text and
contract-test pins that record that endpoint as `NOT_ADOPTED` — by amendment note, never by silent
rewrite. It does not change the `INSUFFICIENT` verdict, the never-resend rule, the rule that a
zero-result lookup never proves absence, or the canary's `BLOCKED` state, which keeps every other
condition of ADR-0018 §6 and §10.

## Invariants

```text
SA-01  every slice still needs its own authorization and is its own PR; this ADR only defines a standing way to meet that rule
SA-02  a slice is authorized here only when it is the next step in ROADMAP order read fresh from the exact main, with no cached selection
SA-03  a slice is authorized here only when the canonical documents already decide its scope, safety invariants and owner boundary
SA-04  a slice authorized here creates no real external side effect: routine read-only calls may proceed under rule §7.1, while provider mutation stays DRY_RUN/refused
SA-05  endpoint adoption is authorized here only where a canonical contract already defines it; adoption is never a call and never re-decides the provider-evidence verdict
SA-06  schema or migration is authorized here only where a canonical contract already decides it concretely
SA-07  LIVE, a real canary, the residual-risk acceptance, new architecture or policy, conflicting canon, unclear scope, undecided data model and scope expansion always stop for the user
SA-08  each slice passes the applicable rule §14.2 tier; only HIGH_RISK requires exact-HEAD DUAL PASS, FULL CI and MERGE_GUARD before merge. A lower-tier merge itself requires no post-merge full-main audit, while transitional V3 auto-next waits for the current-main DUAL_PASS baseline and a supervising agent re-establishes it with the default DELTA/FULL standalone audit before the next selection
SA-09  CREATE adoption and SEARCH positive-only reconcile adoption are two separate slices, CREATE first
SA-10  §4's remaining-work order never omits a mandatory pre-canary prerequisite of ADR-0018 §10 or ADR-0014 §11; the production ASSET sender and the executable committed-session read-back were not authorized here and were closed by their own slice, ROADMAP §14 item 4 under ADR-0022 §7 (the ADR-0014 §27 authoring-revision owners were closed by their own slice, ADR-0014 §27.1, the durable canary-eligibility owner by its own, ADR-0018 §5.1, and a read-back comparison that proves published state by its own, the ADR-0014 §11 amendment note), and the canary stays BLOCKED until every condition of ADR-0018 §6 and §10 and the ADR-0014 §11 success proof is green
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

- `documents/roadmap/ROADMAP.md` §12, §14, §14.1, §14.2
- `CLAUDE.md` §1, §7.2, §11
- ADR-0014 §17.2, §27, §28 (amendment note at §28.8)
- ADR-0015 §2 (the target policy's two authoring revision references; `null` in a revision appended before the ADR-0014 §27.1 owners)
- ADR-0018 §6, §6.1, §10, §12 (amendment note at §12)
- `documents/contracts/platforms/smartstore/ENDPOINT_MATRIX.md`, `documents/acceptance/milestones/M5.md` §9
