# Gate 3 area 3 — populated visual and responsive acceptance

**Status: `PENDING`.** The owner, the harness and their contract exist (Issue #89, authorization
`5843380581`, ADR-0018 §9 and §12). **No exact-main run has been accepted or recorded.** A PR-head
run — a local development run or a CI artifact of a pull request — is evidence of that head only.
It is never an exact-main proof.

## 1. What is proven, and by what

`VISUAL_ACCEPTANCE_RECORDED` is one layer of both `ASSET_MUTATION_READY` and
`CREATE_MUTATION_READY` (ADR-0018 §10). It holds only while all of the following are true.

**A reviewed record exists.** It is a row of `visual_acceptances` (migration
`0030_g3_visual_acceptance`). The table is append-only, and its triggers refuse any update or
delete.

**The record matches the accepted code SHA, the running code and the schema.**
- Its **commit** equals the commit the application runs at. That is ADR-0018 §9's accepted code
  SHA, read once at composition from the checkout's own git metadata (`app/core/code_identity.py`,
  `checkout_sha`).
- As an additional integrity binding, its **running code digest** equals the digest the
  application computed for the code it runs. That digest covers the `app` and `integrations`
  packages, the served UI directory and the dependency pins, with line endings normalized.
- Its schema head equals the application's current schema head.

**What makes a record stale.**
- Any new commit, including a documents-only, tests-only or harness-only one, is another accepted
  code SHA (§9: "again … when the accepted code SHA changed").
- A change to executed or served code in the working tree moves the digest.
- An install with no readable checkout has no SHA, and no record is ever current for it.

**The record was written by the one path.** The only path is
`icbm live record-visual-acceptance`, an owning command that holds the data directory. It records
a report only when every one of these holds:
1. The report verifies against the contract below (`app/live/visual.py`, `verify_report`).
2. Its code digest is the running code's.
3. Its schema head is current.
4. Its commit is the commit the application runs at.
5. A reviewer and the GitHub comment that accepted it are named.

If any fails, it refuses and records nothing. No HTTP route, UI action or other module can record
or assert one; a repository rule keeps it so.

A recorded acceptance is a **proof, never permission**. Even with it, the execution mode stays
`M0_DRY_RUN_ONLY` and CREATE/SEARCH stay `NOT_ADOPTED`. Eligibility is unproven and no ASSET sender
is wired, so every stage stays `BLOCKED`.

## 2. The contract (`app/live/visual.py`)

**Viewports.** `landscape-1920x1080` and `portrait-1080x1920` are required. A run may add sizes,
and then every surface must be run at those too.

**Surfaces.** Each required selector must render at least one element, so an empty screen never
passes.

| surface | screen | populated state that must render |
| --- | --- | --- |
| `collect` | 수집관리 | recorded runs; the focused run's facts status |
| `db` | 통합DB | the Product's members, its images fact, its review items |
| `register` | 등록관리 | the unit, its candidate preflight, its §26 execution scope, its review items, the canary readiness, the protected-write brake, every grant with its ASSET readiness, the unit-independent proofs |
| `dashboard` | 대시보드 | the review counts, with their state |
| `soldout` | 품절 | the stock review count, with its state |
| `settings-policy` | 설정 · 정책 | the account's target policy state |
| `settings-metadata` | 설정 · 상품 | the reviewed category metadata |

**Checks, at every surface and viewport.** Each must be present and passed.
- `rendered`: the screen rendered.
- `populated`: every required selector rendered at least one element.
- `no_console_error`: no console error was logged.
- `no_external_request`: the browser made no request outside the served application.
- `no_horizontal_overflow`: nothing on the page or in any state element is horizontally clipped or
  off-screen.
- `state_visible`: every element carrying server-owned state is visible and not clipped
  vertically.
- `state_not_truncated`: no such element is cut off by an overflow clip or ellipsis.
- `state_not_covered`: no such element is covered by or overlapping another.
- `title_help_icon`: the page title has exactly one labelled help icon.
- `navigation_intact`: the ten navigation items are present, with this screen active.

**What counts as server-owned state.** Every element that matches one of these selectors:
`[data-reason]`, `[data-state]`, `[data-review-state]`, `[data-requirement]`, `[data-scope-state]`,
`[data-preflight]`, `[data-outcome]`, `[data-facts-status]`, `[data-policy-state]`,
`[data-canary]`, `[data-capability]`, `[data-brake-state]`, `[data-grant-state]`, `[data-proof]`,
`[data-missing]`, `[data-coverage]`, `.chip` and `.note`. A native `<option>` is judged by the
`<select>` that shows it.

**What the whole run must show.**
- No external request.
- No console or page error.
- The served application's own egress counter stays at zero.
- The checkout is clean and unchanged during the run.
- The report is sealed by its SHA-256 digest.
- The report passes the outbound sanitizer: no secret material and no URL. Elements are described
  by their tag, class and `data-*` attributes only.

A cosmetic difference from the approved prototype is not a failure. A hidden, truncated, covered
or clipped server-owned state is.

## 3. The populated scenario (`scripts/g3visual/scenario.py`)

The scenario is invented and provider-zero. Every step is written through the application's own
owners and routes into a fresh dedicated root:
- two recorded collection runs (one with a REVIEW_REQUIRED fact) and their Products and Items;
- an image selection with a QA PASS, and a price;
- the account's durable target policy and a reviewed category's metadata;
- a Draft and its authored preparation, whose candidate preflight reports what the durable policy
  still lacks;
- the account's CREATE scope PAUSED for AUTH;
- the protected-write brake ENGAGED;
- an evidence-retention proof;
- the review counts the producers derive from all of it.

**Declared seams**, each written by its owner's store and named in the report:
- `connect_binding`: the committed M2 binding. A real SmartStore call produces it, and no provider
  call may be made.
- `live_grants`: an ACTIVE and a REVOKED ASSET grant. Under a durable policy no candidate can be
  READY at this main, so the grant service would refuse them. Every readiness they show is still
  the server's own derivation, and it is `BLOCKED`.

## 4. Procedure for the accepted run (after the area 3 merge)

1. Exact-main CI is green, including `g3-visual-acceptance` (its artifact is exact-main evidence of
   that commit).
2. Run the harness on a clean checkout of exactly that main commit, into a fresh root under
   `%USERPROFILE%\ICBM-acceptance\<id>`, and verify the report:

   ```text
   python scripts/g3_visual_acceptance.py run --root %USERPROFILE%\ICBM-acceptance\g3-visual-<n>
   python scripts/g3_visual_acceptance.py verify <root>\g3-visual-report.json
   ```

3. Publish the report and its screenshots for review, and record the architect's acceptance
   comment.
4. With the application stopped, and with the checkout at the report's exact commit, record it:

   ```text
   icbm live record-visual-acceptance --report <root>\g3-visual-report.json
       --approved-by <reviewer> --authorization-ref <comment id> --actor <operator>
   ```

   The record is current only while the application runs at exactly that commit. Publishing the
   evidence under `docs/` in a later commit is itself a new accepted code SHA. A run meant to gate a
   mutation stage must therefore be taken, reviewed and recorded at the commit the operator runs
   when that stage is authorized.

## 5. Recorded runs

None.
