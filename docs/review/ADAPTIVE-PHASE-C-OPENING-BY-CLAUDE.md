# Adaptive Collector — Phase C opening proposal (KM통상 shadow)

- Status: **PROPOSAL** (awaiting architect review)
- Author: Claude Code
- Issue: #110 (after P3 closeout `5825907707`; canonical main `cf37a1c81172c42faa2c1728f1185aa5f28d037e`)
- Contract: `docs/adr/0017-adaptive-collector-profile-extraction-and-shadow-validation.md` §2, §7, §10, §11
- This document authorizes nothing. It enables no switch, declares no window and reads no supplier.
  It lists what a Phase C opening needs, what P1–P3 already provide, and the decisions the
  architect must make before any real Phase C read.

---

## 1. What Phase C is (ADR-0017 §2, §11)

Phase C is the KM통상 shadow. Over **ordinary operator collections** — never a read made for the
shadow's sake — the Adaptive engine runs one exact, VALIDATED bundle beside the canonical KM통상
extractor. The evidence ledger then counts every eligible collection of a declared window. The
canonical extractor stays the only revision writer. Phase C proves or disproves one bundle; it
does not cut over (`ACTIVE` needs a separate cutover ADR).

A window can `PASS` only with at least K = 3 eligible collections, all counting as successes, and
at least one of them after a process restart (§11.3). The bundle passes only if at least one
closed window passes and no window blocks (§11.3, AC-26, AC-29).

## 2. What already exists on main (P1–P3, all merged)

| need | provided by | state |
| --- | --- | --- |
| Offline engine, strict profiles, V1–V8 validation, freshness, derived VALIDATED | P1 `app/collect/adaptive/` | ready |
| Profile, lint, lifecycle, sample and run persistence | P2 `app/collect/adaptive_store/` (0024) | ready |
| Per-supplier switch; the only way into `SHADOW`; freeze at the first reservation | P3 `adaptive_shadow/switch.py`, run store (0025) | ready; **no entry exists** |
| One-fetch shadow step after the canonical commit; §10.3/§10.4 comparison | P3 `runner.py`, `compare.py` | ready; runs only for an ENABLED frozen run |
| Raw store (90 days / 5,000), ledger, windows, reconciliation, verdicts | P3 `store.py`, `evidence.py` | ready; **no window exists** |
| Startup reconciliation and pruning | P3 lifespan wiring | ready |

## 3. What does not exist yet (the gaps a Phase C opening must close)

1. **No KM통상 EPR/PTR.** A profile has to be authored for KM통상's product document: templates,
   locators, vocabularies, image regions and roles, identity sources, and hooks if any. Nothing
   Adaptive is authored for a real supplier yet.
2. **No path to capture a real `ValidationSample`.** `capture_sample` exists (P1) and samples can
   be stored (P2), but no runtime path hands a real KM통상 document to the capture owner. The body
   is never persisted (ADR-0010 §3, S5), so a sample has to be cut **in memory, during an ordinary
   operator collection**. The operator must approve a product scope and author the expected facts
   from the source, not with AI and not from the profile (§7.3). This needs:
   - a wiring point;
   - an operator scope and expectation input;
   - a "capture requested for this run" decision that is frozen, as the shadow decision is.
3. **No negative controls from the real site.** V4 needs a typed `LOGIN` and a typed
   `NON_PRODUCT` page. Whether these are captured from KM통상 or synthetic is a decision
   (question Q3).
4. **No operator surface for the shadow owner.** Enabling or disabling the switch, declaring,
   ending and closing a window, and recording a resolution are owner APIs only. Phase C needs a
   bounded, recorded operator path (Q4). The shadow owner writes no audit event by design, and the
   actor and correlation are recorded on each switch entry, window event and ledger event.
5. **No hook manifest for KM통상.** The production hook registry is empty. If the profile needs a
   hook (for example identity decoding), a manifest slice is required; the hook-growth guards
   G5–G7 apply.
6. **No Phase C evidence document.** The accepted outcome belongs in `docs/acceptance/`, following
   the M3/M4 campaign practice (Q6).

## 4. Proposed sequence (each stage separately authorized; reads only where stated)

| stage | what | real reads |
| --- | --- | --- |
| **C0 — tooling slice** (code, 0 reads) | (a) in-memory sample capture wiring on an operator-requested ordinary collection, frozen per run at its first reservation; (b) an operator harness under `scripts/` for switch, window and resolution actions, with its own campaign directory (`%USERPROFILE%\ICBM-acceptance\<campaign-id>`); (c) the KM통상 hook manifest, if Q2 needs one. Synthetic tests only. | **0** |
| **C1 — profile and samples** | Author the KM통상 EPR/PTR from ordinary collections of products the operator chooses. Capture ≥ 2 samples, at least one per template (V3). The operator approves each scope and authors each sample's expected facts from the source. Record V1–V8 runs. | **ordinary operator collections only**, a bounded count fixed by the authorization; one read each, paced by `SAME_PRODUCT_INTERVAL_S` |
| **C2 — enable and declare** | Once a `PASS` validation run exists for the exact freshness: `enable` the switch for that exact EPR, then `declare` one window (K = 3) for that exact bundle. Both steps are recorded with actor and correlation. | **0** |
| **C3 — shadow collections** | At least K = 3 ordinary operator collections inside the window, at least one after a process restart. Every eligible run counts; none is excluded. Mismatches are resolved from the source within the raw bound. | **ordinary operator collections only**, bounded |
| **C4 — close and record** | `END`, then `CLOSE` (both reconcile first), the bundle verdict, and a `docs/acceptance/` record listing every window by `collection_run_id` with its effective state, cause and event history. | **0** |

Stop conditions, at any stage:
- a permanent blocking cause, `IMAGE_UNMATCHABLE`, `SHADOW_MISSING`, `PRUNED_BEFORE_RESOLUTION` or a `FAIL` window. The bundle can then never pass; a fix is a content-different EPR and a new window (AC-29);
- any canonical invariance breach;
- any read the authorization did not count.

## 5. What the opening would not do

- No `ACTIVE`, no cutover, no Adaptive canonical `ProductFactsRevision`.
- No second supplier (Phase D stays deferred; `CLAUDE.md` §12).
- No AI, OCR or vision.
- No read made for the shadow's sake. The shadow reads only the one document an ordinary
  collection already read (S1).
- No change to the canonical extractor's authority, the source-revision pointer or any
  marketplace path.

## 6. Questions for the architect

- **Q1 — C0 first?** The recommendation is yes. A tooling slice with zero reads should come
  before any real read, so that C1–C4 run on audited code only.
- **Q2 — hooks.** Should the KM통상 profile be authored hook-free if at all possible (G4:
  profile-only means zero bindings), with a hook manifest only if V2/V3 prove one necessary?
- **Q3 — negative controls.** Should V4's `LOGIN` and `NON_PRODUCT` pages be captured from real
  KM통상 ordinary reads, sanitized, or should they be synthetic pages built on the site's
  structure?
- **Q4 — operator path.** Should switch, window and resolution actions be an acceptance-style
  harness script, as M2–M5 used, or an application route? The recommendation is a harness first:
  no product UI.
- **Q5 — read budget.** What is the exact maximum number of ordinary collections for C1 and C3,
  and on which products? The recommendation is the accepted M3 product plus operator-chosen
  products, one read each per interval.
- **Q6 — evidence record.** Should the evidence go in `docs/acceptance/ADAPTIVE-PHASE-C.md`, with
  the campaign identity, every window, every run and every event, following the M3 closeout form?
- **Q7 — shadow-mismatch resolution.** A human resolves each mismatch from source evidence (§10.3),
  and a resolution is never a ReviewItem. Who resolves, and what counts as the recorded
  `evidence_ref`?

## 7. Requested decision

An architect decision on Q1–Q7. If C0 is accepted, a separate authorization for the C0 tooling
slice, with no reads. Phase C stays closed until each of C1–C4 is authorized explicitly.
