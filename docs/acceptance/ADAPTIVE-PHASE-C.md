# Adaptive Collector Phase C — KM통상 shadow evidence

- Status: **PENDING — NOT ACCEPTED.** This is an evidence scaffold only. No real bundle, switch
  entry, evidence window, supplier read, sample or verdict is recorded here, and none has happened.
  It records evidence only after stage C4 is authorized and executed; a green PR, a green CI or a
  synthetic harness run never accepts anything.
- Issue: #110. Contract: `docs/adr/0017-adaptive-collector-profile-extraction-and-shadow-validation.md`
  §7, §10, §11 (AC-01 to AC-29).
- Plan: `docs/review/ADAPTIVE-PHASE-C-OPENING-BY-CLAUDE.md` (PR #119). Q1–Q7 decisions: review
  `5312911203`. ADR-0006 stopped-app supplement: review `5313045448`. Opening closeout: `5826469328`.
- Stage authorizations: C0 `5826469852` (closed `5841947278`); C1 PREP-0 hardening `5841947773`. C1, C2, C3 and C4 are **not authorized**.
- Tooling: the Phase C harness `scripts/phase_c.py` / `scripts/phasec/` (C0). It is the only caller
  of the Phase C operator actions, and it runs only with the ICBM application stopped. After
  review `5313663701`:
  - every command runs only at the campaign's exact clean code SHA;
  - the campaign ledger is a crash-durable, single-writer, append-only SQLite file in the
    campaign root;
  - a stage opens only through a typed grant, C0 → C4, once each, recorded as the exact bytes
    that were read. Its authorization is an opaque anchor (`issuecomment-<id>` /
    `pullrequestreview-<id>`), used once and never compared by size. The C1 grant freezes exactly
    two target digests, C2 the exact EPR, samples and PASS run, and C3 and C4 the one window;
  - the frozen ceilings are enforced by ledger reservations;
  - a campaign acts only on objects its own ledger bound;
  - every command that changes the data root is reserved in the data root
    (`adaptive_phase_c_commands`) under one stable correlation before the change, and settled
    after it. After a crash, a command without a result is reconciled from owner truth:
    - proven applied → recorded as recovered;
    - proven not applied → `NOT_APPLIED`, and its reservation is released for a safe retry;
    - ambiguous → the campaign goes on `HOLD`, and the command stays unresolved in the data root.

    While any command is unresolved, no campaign on that data root runs an evidence command, so
    a new campaign cannot walk around it.

- Send accounting (C1 PREP-0; migration 0028):
  - An ordinary collection becomes Phase-C-accounted only when its frozen capture request binds it to a campaign that registered its read ceilings.
  - Each actual send is reserved in the data root before it is sent, and each reservation is committed first:
    - `PRODUCT_READ` and `IMAGE_REQUEST` at the collection transport's budget;
    - `CONNECT_CONTROL_READ` and `CONNECT_PROTECTED_READ` at the CONNECT fetch.
  - `CONNECT_AUTHENTICATE` is a hard zero, refused before any login is attempted.
  - Retries and restarts consume the same frozen ceilings. `IMAGE_REQUEST` is bounded per attempt.
  - A missing binding, a target mismatch, an unreadable owner or a ceiling refuses before the send.
  - Ordinary collections are unchanged.
- A stage grant is accepted only when its exact bytes hash to the SHA-256 its canonical authorization published (`--published-sha256`).
- Profile persistence (C1 PREP-1, review `5843047094`): `store-profile` is the one reviewed operator path that stores the campaign's operator-authored PTR/EPR documents. It runs only in C1, under the stopped-app lease, through the P2 owner's `save_template` / `save_draft`, from files outside the repository and every data root. Each file is read once. A document is refused if it has another supplier, or any hook binding, or if its EPR pins a template the campaign does not store. The documents must recompute to exactly the PTR/EPR digests GPT and Claude reviewed (`--expect-template` / `--expect-epr`). That check runs before any intent or write, and the reviewed profile-set digest appears in the approval phrase, the intent and the outcome (review `5324679231`). The owner's digests must equal the documents' own. `validate` accepts only an EPR the campaign stored. `profile-digest` computes a document's digest offline. It requires the intended `--data-root` as a path-policy input and never opens it.

---

## 1. What Phase C would claim

For one exact, VALIDATED KM통상 bundle, over ordinary operator collections only, the Adaptive
engine agrees with the canonical extractor under ADR-0017 §11.3. The claim requires at least one
closed `PASS` window of K = 3 eligible collections, one of them after a process restart. It also
requires no window to be `FAIL`, and no run to hold a blocking cause. The canonical extractor stays
the only revision writer. Phase C accepts no `ACTIVE`, no cutover and no second supplier.

## 2. Stages and ceilings (frozen per campaign; review `5312911203` Q5)

| stage | what | real reads | ceilings |
| --- | --- | --- | --- |
| C0 | tooling, synthetic tests only | 0 | — |
| C1 | profile + 2 samples from 2 fixed target identities | ordinary operator collections | 2 submissions, 4 PRODUCT_READ, 4 + 4 CONNECT reads, 0 authenticate, 0 policy, 13 images / 2 MiB each / 24 MiB new per attempt |
| C2 | enable the exact bundle, declare the K = 3 window | 0 | — |
| C3 | 3 ordinary collections in the window, one after a restart | ordinary operator collections | 3 submissions, 6 PRODUCT_READ, 6 + 6 CONNECT reads, 0 authenticate, same image bound |
| C4 | reconcile, end, close, record | 0 | — |

A ceiling is never a target and is never widened inside a campaign. The shadow itself adds zero
supplier requests.

## 3. Evidence to record at C4 (review `5312911203` Q6)

Nothing below is filled until C4. Every entry is an identifier, digest, state or count. None is a
page body, cookie, header, session value, secret-bearing URL or private account datum.

### 3.1 Campaign
- campaign id; exact code / main SHA; the campaign ledger's final event hash;
- the C0–C4 authorizations, the architect and Claude reviews, and the CI runs.

### 3.2 The bundle
- EPR digest; every PTR digest;
- the freshness tuple (schema, extractor revision, extractor fingerprint, hook fingerprint (none:
  hook-free first, Q2), sample-set digest, capture revision);
- the ValidationSample digests; the ValidationRun id and verdict; the synthetic V4 controls used.

### 3.3 Switch and window
- the shadow switch entry id and the exact frozen bundle key;
- the evidence window id and K; its DECLARED / ENDED / CLOSED events; any supersession.

### 3.4 Every eligible collection
One row per `collection_run_id`, including every INCOMPLETE and superseded one:
- canonical outcome and revision id (or NO_REVISION); the frozen decision and first reservation;
- process-run identity (the restart evidence); shadow verdict; shadow-step wall-clock duration
  (observational only);
- effective ledger state, cause and last event sequence; full event history;
- any resolution artifact `evidence_ref` (`phase-c:<campaign-id>:resolution:<sha256>`) and digest.

### 3.5 Integrity
- raw-retention status and nearer bound at close; every reconciliation result;
- the canonical-invariance checks; the zero-extra-read evidence.

### 3.6 Verdicts
- the window verdict and denominator; the bundle verdict and its reasons.

## 4. Current state

| item | state |
| --- | --- |
| KM통상 EPR/PTR | none |
| ValidationSample from KM통상 | none |
| shadow switch entry | none (every supplier is off) |
| evidence window | none |
| supplier reads for Phase C | 0 |
| verdict | none |
