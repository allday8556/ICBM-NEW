# ADR-0043 — The seller's target margin, the in-app LIVE approval, and release-time acceptances

Status: **PROPOSED** 2026-10-16. The owner's decisions are recorded (Issue #219 `6200578768`, `6201001579`). It becomes **ACCEPTED** when it merges after the GPT + Claude cross-audit.

This ADR is the contract for three things:
- **D1:** the seller sets the target net margin in Settings, and ICBM enforces no minimum margin;
- **D2:** a seller registers products for real from the screen. One approval covers one product or a selection of up to 500. This replaces a protected CLI command run by an operator; the command stays available;
- **D3:** the two acceptances a real registration still needs are recorded where they belong:
  - the visual acceptance: once per release build, by the developer, shipped with the build;
  - the residual-risk acceptance: once per store account, in the app, by the owner.

It lands before its code. Each slice of §9 cites it as canon. It **amends**:
- ADR-0018 §2, §3.2, §3.3, §4.1, §6.1, §9 and §10, and invariants G3-18, G3-30 and G3-31;
- ADR-0013 §7 and §8;
- ADR-0014 §24, §28.7 and §29;
- ROADMAP "Pricing contract" and §14.2;
- ARCHITECTURE §13;
- `documents/acceptance/gates/G3-VISUAL.md`.

It adds one note to `documents/rules/07-execution-safety.md` §7.1–§7.2. §7 states each amendment and the slice that makes it effective. It supersedes none of them.

Decision owners:
- **Product direction:** the owner, in Issue #219.
  - `6200578768` (2026-10-16, "첫 실등록까지 남은 판단 3가지"):
    - **D1:** the target margin is the seller's Settings value, with no minimum floor (원문: "마진 최소 하한 없애 사용자결정에 맡겨"). A loss — a margin below 0% — is a warning the seller sees, never a block.
    - **D2:** real registration is approved on the screen. ADR-0018 §4.1 (CLI-only grants) is amended on the model of ADR-0042 §11 A4.
      - An approval issues exact ASSET and CREATE grants (budget 1, short expiry) and opens the bounded LIVE window inside them.
      - The server runs the image upload, the restore drill and the retention proof.
      - Every §4.3 layer is still checked per mutation, and the brake stays a separate explicit action.
    - **D3:** the visual acceptance is recorded by the developer once per release build and shipped with it. The residual-risk acceptance is confirmed by the owner once per account, in the app.
  - `6201001579` (2026-10-16, "ADR-0043 세부 규칙"), O1–O5 (§10):
    - **O1:** the in-app approval counts as the owner's approval without a GitHub record, with a note in rule §7.1–§7.2.
    - **O2:** a margin below 0% is allowed, with a warning.
    - **O3:** existing stores keep today's rule (35%) until the seller saves the margin once.
    - **O4:** the architect's half of the residual-risk acceptance is given at contract level by `6200578768`; the user's half is recorded in the app, once per account.
    - **O5:** one approval covers **at most 500 products**. The owner's question: "600개를 올려야 되는데 승인만 600번을 해야 해?" Under that one approval:
      - each product gets its own exact grants of budget 1, and products run in order;
      - the batch stops at an uncertain result, and 「중지」 is always available;
      - past the four-hour window cap it pauses, and 「계속」 is one more press;
      - the first real registration of an account is a single-product test.
  - `6109102895`: the seller-risk principle. Where the seller bears the risk, ICBM shows the facts and the user decides. ICBM's own safety invariants stay hard.
  - The principle "실제 사용자 기준" (2026-10-15): each slice is accepted by a seller walkthrough on the real UI (§0, §9).
- **Design decisions A1–A16** (§10): made by the implementation agent under ADR-0022, recorded here as decisions.
- **Already decided by canon, and unchanged here:**
  - ADR-0018: the grant, the brake, the send-time stack, the restore drill, retention, the ASSET upload-attempt owner and its replay fence.
  - ADR-0014 §9–§11, §15, §26 and §28: attempts, `UNKNOWN`, read-back, the sanitizer, the execution-scope brake and positive-only reconcile.
  - ADR-0013 §7: the canonical pricing rule (`documents/rules/06-immutable-domain-rules.md` §6.1).
  - ADR-0039: the supplier minimum-sale-price policy is the user's choice.
  - ADR-0042 §11 A4: the UI-issued batch approval of the UPDATE stage, the model D2 follows. Its own rule (at most 50 listings, 30 minutes) is unchanged; this ADR governs CREATE.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

**Context.** After #368, #369, #370, S1b and S3b, a seller still cannot finish one real registration from the screen. Three things stop it.

**1. The margin is a constant.**
- `app/stages/products/pricing.py` fixes `TARGET_NET_MARGIN = Fraction(35, 100)` and `MINIMUM_NET_MARGIN = Fraction(10, 100)` under `pricing-rule/v1`.
- Both snapshot tables pin them by CHECK:
  - `pricing_snapshots`: migration `0013`; `app/stages/products/models.py` `rule_known`, `policy_margins`, `guard_exact`, `guard_reasons_exact`;
  - `atomic_sku_pricing_snapshots`: migration `0047` lines 188–251; `atomic_sku_economics_models.py`.
- A guard of `LOSS` or `BELOW_MIN_MARGIN` makes pricing readiness `BLOCKED` (`app/stages/products/readiness.py`, ADR-0013 §8).
- The seller can choose neither value.

**2. Every grant is issued by an operator command.**
- These commands exist only in `app/interface/cli.py`: `issue-asset-grant`, `issue-create-grant`, `upload-assets`, `restore-drill-asset`, `restore-drill-create`, `prove-retention` and `release-brake`. ADR-0018 §4.1's amendment notes say "never a new screen or route".
- `upload-assets` runs only with the server stopped.
- Grants and a brake release accept only a GitHub comment id as their authorization reference (`live_grants.authorization_ref_is_comment_id`, `protected_write_brakes.release_is_authorized`).
- A seller with hundreds of products would need one operator session per product.

**3. Both acceptances are developer-side records.**
- The visual acceptance is current only at the exact git commit the process runs at (`app/capabilities/live_safety/visual.py` lines 333–340; `app/container.py` lines 1128–1133; `app/platform/core/code_identity.py`). An installed build with no git checkout can never have one.
- The residual-risk proof needs two GitHub comment ids, the user's and the architect's (`residual_risk.py` lines 143–160). It is recorded only by `icbm live record-residual-risk-acceptance`.

What it authorizes:
- this contract;
- the slices of §9, each its own PR;
- the amendments of §7, each effective when the slice it names lands.

What it does not authorize:
- **No mutation outside ADR-0018.** Every upload and every CREATE is still admitted, at send time, by every layer of ADR-0018 §4.3, in the one unit that starts its attempt (§6).
- **No widening.**
  - No grant is wider, longer or larger than ADR-0018 already allows. Every grant is issued per product, per mutation, with budget 1 (§3.1).
  - No LIVE window is longer than four hours or spans a freeze (§3.5).
  - No layer is removed.
- **No shared grant.** A grant of one product never matches another. A batch is a sequence of single-product scopes under one approval, never a wider scope.
- **No implied brake release.** An approval never releases the brake (§3.9).
- **No blind resend.** An `UNKNOWN` CREATE and an `UPLOAD_UNKNOWN` stay governed by ADR-0014 §10, §17.1 and §28 and by ADR-0018 §3.3–§3.4. Either one stops the whole batch (§3.7).
- **No batch before the canary.** The first real registration of an account is a single-product approval (§3.1).
- **No batch beyond 500 products** in one approval.
- **No agent approval.** An agent never gives, clicks or simulates an in-app approval, a 「계속」, a brake release or a residual-risk acceptance (LAP-13).
- **No change to the canonical pricing rule.** `documents/rules/06-immutable-domain-rules.md` §6.1 is unchanged; only the target margin's source and the guards' effect change (§2).
- **No change to ADR-0042 S2c**, its UPDATE stage or its batch rule.
- **No Coupang change.** Track C's bounded Coupang mutation commands (`icbm coupang …`) are outside this ADR.

Recorded by: Claude Code (Track B). The number 0043 was confirmed free on `main` and in every remote branch immediately before writing. Migrations start at `0071`: ADR-0042 reserved `0067`–`0070`, and main's head is `0066_landed_cost`.

Date: 2026-10-16

---

## 0. 사용자 관점 / Seller walkthrough

This is what a seller does and sees. The screens are the existing ones: 설정, 통합DB, and 등록관리 with its list and editor. New elements are marked **new**. Korean strings are the intended copy; the slices may refine the wording, not the meaning.

These walkthroughs are the acceptance script of every slice (§9). Each slice runs the steps it delivers on the real UI, at desktop and mobile width, before it is accepted.

### 0.1 Once per store

**s1 — 설정 → 스마트스토어 → 가격 정책.**
- **The seller does:** **new:** types 「목표 순마진」, for example 25 (%), and saves.
- **The seller sees:**
  - 「저장됨 · 상품 가격을 다시 계산합니다」;
  - under the field: 「최소 마진 제한은 없습니다. 0% 미만이면 손실 판매입니다.」;
  - for a value below 0, before saving: 「손실 판매 목표입니다」, then 「그래도 저장」;
  - for a value no price can reach with this store's fees, a refusal: 「수수료를 빼면 이 마진을 만들 수 없습니다 (최대 …%)」;
  - until the first save: 「목표 마진을 설정에서 확인해 주세요 (지금은 35%)」.

**s2 — 통합DB → product.**
- **The seller does:** nothing.
- **The seller sees:** prices recomputed under the new margin, for example 「판매가 12,900원 · 예상 순마진 25.0%」. Neither of these blocks registration:
  - a product priced by the supplier's minimum sale price below the target shows 「목표 마진보다 낮음 (18.2%)」;
  - a loss shows 「손실 판매: 예상 순이익 −1,200원」.

**s3 — 설정 → 스마트스토어 → new 「실제 등록 위험 안내」.**
- **The seller does:** reads it and presses 「이해했고 수락합니다」.
- **The seller sees:** the statement of §5.2 in Korean and the store name, then 「수락함 · 10/16 14:02」 and 「수락 철회」. It is asked once per store, and never again unless the statement changes.

**s4 — 등록관리 → the 「실제 등록 잠금」 card (also in 설정 → 스마트스토어).**
- **The seller does:** **new:** presses 「실제 등록 잠금 해제」, then 「잠금 해제」 in the dialog.
- **The seller sees:**
  - the dialog: 「잠금을 풀어도 승인하기 전에는 아무것도 등록되지 않습니다. 언제든 다시 잠글 수 있습니다.」;
  - then 「잠금 해제됨」, with **new** 「실제 등록 잠그기」: always visible, one press, no dialog.

### 0.2 One product — always the first real registration of a store (the canary)

**p1 — 등록관리 editor.**
- **The seller does:** prepares the listing and presses 「등록 준비 확인」, as today.
- **The seller sees:** 「등록 준비 완료」 (the candidate preflight is `READY`), with the price row and its margin warning, if any.

**p2 — 등록관리 editor.**
- **The seller does:** nothing.
- **The seller sees:** **new** 「이 상품 실제 등록 승인」, enabled only when every precondition of §3.3 holds. Otherwise it is disabled, and each missing item is a plain line with a link:
  - 「실제 등록 잠금 해제 필요」
  - 「위험 안내 수락 필요」
  - 「스토어 연결 확인 필요」
  - 「전송 일시정지됨 · 전송 재개」
  - 「이전 등록 결과 확인 중」
  - 「이 스토어의 다른 승인이 진행 중」
  - 「이 버전은 화면 검수가 기록되지 않아 실제 등록을 할 수 없습니다. 업데이트를 받아 주세요.」

**p3 — the approval dialog.**
- **The seller does:** presses the button.
- **The seller sees**, **new**:
  - 스토어, 상품명, 판매가, 예상 순마진 (with its warning), 카테고리, 「이미지 n장 업로드」 and the seller code;
  - 「네이버 스마트스토어에 실제로 등록됩니다. 결과가 불확실하면 다시 보내지 않고 등록 여부를 확인합니다.」;
  - the buttons 「취소」 (focused) and 「승인하고 등록」. Enter and Esc cancel.

**p4 — the progress panel.**
- **The seller does:** presses 「승인하고 등록」.
- **The seller sees**, **new**:
  - the steps, each with its state: 「보존 확인」 → 「백업 확인」 → 「이미지 올리는 중 2/5」 → 「등록 내용 확정」 → 「백업 확인」 → 「상품 등록 전송」 → 「등록 결과 확인」;
  - 「승인 취소」, until the CREATE is handed to the transport;
  - 「실제 등록 잠그기」, always;
  - the editor's fields read-only: 「승인 진행 중에는 수정할 수 없습니다」.

**p5 — the progress panel and the registration status card (ADR-0014 §28.5).**
- **The seller does:** nothing.
- **The seller sees** one of:
  - 「등록성공」, with the product number and 「스토어에서 보기」;
  - 「재확인필요」: 「결과를 확인하지 못했습니다. 다시 보내지 않고 등록 여부를 확인합니다.」;
  - 「등록실패」: 「네이버가 거절했습니다 (등록되지 않음)」;
  - 「중단됨」, with its reason and what was sent: 「이미지 2장만 올라갔고 상품은 보내지 않았습니다」.

  Once the first 「등록성공」 is confirmed by read-back, the store's batch approval opens: 「이제 여러 상품을 한 번에 승인할 수 있습니다」.

**p6 — the same, in a second tab or after a delay.**
- **The seller does:** presses 「승인하고 등록」 again.
- **The seller sees** one of:
  - 「이 스토어의 승인이 이미 진행 중입니다」, with the running approval's progress;
  - 「승인 내용이 바뀌었습니다. 다시 확인해 주세요」, with the new preview;
  - 「승인 시간이 지났습니다. 다시 승인해 주세요」.

### 0.3 A selection — up to 500 products in one approval

**b1 — 등록관리 list.**
- **The seller does:** ticks products, or presses 「이 페이지 전체 선택」.
- **The seller sees:** **new** 「선택한 상품 실제 등록 승인 (312)」. It is disabled:
  - before the store's first verified registration: 「이 스토어의 첫 실제 등록은 상품 1개로 먼저 확인합니다」;
  - over 500: 「한 번에 최대 500개까지 승인할 수 있습니다」.

**b2 — the batch approval dialog.**
- **The seller does:** presses the button.
- **The seller sees**, **new**: 「312개 상품 실제 등록」, and:
  - 「준비되지 않은 8개는 제외됩니다」, each with its reason;
  - every included product's name, in order;
  - 「판매가 9,900원 ~ 59,000원」;
  - 「손실 경고 3개」 and 「목표 마진보다 낮음 5개」, each listed;
  - 「이미지 1,240장 업로드」;
  - 「한 상품씩 차례로 등록합니다. 결과가 불확실한 상품이 하나라도 생기면 그 자리에서 모두 멈춥니다.」;
  - 「4시간마다 멈추고, 「계속」을 눌러야 이어집니다.」;
  - the buttons 「취소」 (focused) and 「승인하고 등록 시작」.

**b3 — the batch progress panel.**
- **The seller does:** presses 「승인하고 등록 시작」.
- **The seller sees:**
  - 「12 / 304 · 진행 중: ‘상품명’ · 이미지 올리는 중」;
  - a per-product list that fills in: 등록성공 / 재확인필요 / 등록실패 / 보내지 않음;
  - 「중지」, which finishes the current product and starts no other;
  - 「실제 등록 잠그기」;
  - every product of the batch read-only in the editor.

**b4 — the same, when a product's result is uncertain.**
- **The seller does:** nothing.
- **The seller sees:** 「‘상품명’의 등록 결과가 불확실해 전체를 멈췄습니다. 남은 287개는 보내지 않았습니다.」 The remaining products are unlocked and need a new approval.

**b5 — the same, after 4 hours.**
- **The seller does:** nothing.
- **The seller sees:**
  - 「4시간이 지나 멈췄습니다. 남은 120개」, with 「계속」 and 「남은 상품 승인 취소」;
  - after 「계속」: a dialog with the remaining products' summary, recomputed now, and 「계속 등록」 (with 「취소」 focused);
  - if any remaining product changed: 「‘상품명’의 내용이 승인 후 바뀌어 멈췄습니다」, and the batch ends.

**b6 — the progress panel, at the end.**
- **The seller does:** filters by result.
- **The seller sees:**
  - 「등록성공 296 · 재확인필요 0 · 등록실패 4 · 보내지 않음 4」;
  - for each 등록실패, the store's reason; for each 보내지 않음, why.

### 0.4 Mobile width

The steps are the same. Only the layout differs:
- The approval buttons are full-width and fixed at the bottom.
- The dialogs are full-screen sheets. Their preview rows stack, and a margin warning is never truncated.
- These lists collapse into a summary, opened by a tap:
  - the missing preconditions of p2: 「실제 등록 전에 필요한 것 3개」;
  - the batch's name and warning lists of b2: 「상품 312개 보기」.

  Each is a `<details>` whose summary is visible, so the visual acceptance judges every line inside it (ADR-0018 §9 amendment, 2026-10-15).
- The batch progress list shows the current product and the counts first, and the full list below.

### 0.5 Once per release build (the developer, not the seller)

**r1 — the developer.**
- **Does:** builds the release, runs the populated visual harness against it, reviews the report, and records it into the build (§5.1).
- **Result:** the build ships `release/visual-acceptance.json` beside its manifest.

**r2 — the seller.**
- **Does:** installs or updates.
- **Result:** nothing visible. Without a valid record for the running build, p2 and b1 show the line 「이 버전은 화면 검수가 기록되지 않아…」, and nothing can be approved.

## 1. Terms

| term | meaning |
| --- | --- |
| **product** | One provider-listing unit of one store account (ADR-0014 §2): one preparation, one future Snapshot and one CREATE Intent. |
| **approval** | The durable, audited, server-owned record that the seller approved an ordered list of 1–500 products of one store account for real registration (§3). It is the protected action that carries the user's approval (rule §7.2). The grants it leads to are the authority.<br>A **single** approval has one product; a **batch** approval has more. |
| **product packet** | The deterministic, server-built preview of exactly what the approval covers for one product, and its SHA-256 (§3.2). |
| **batch packet** | The ordered list of the product packets' digests and the batch's summary, and its SHA-256 (§3.2). That SHA-256 is the **batch digest** the client echoes. |
| **segment** | One bounded stretch of an approval's processing, at most four hours. The approval opens the first; each 「계속」 opens another (§3.5). |
| **product run** | The server's sequence for one product inside a segment: it proves, uploads, freezes, issues the CREATE grant and sends (§3.6). |
| **release build** | One built distribution of the application, identified by its code digest, its schema head and a release identifier (§5.1). |
| **target net margin** | The seller's Settings value `t`. The price is set so that `expected_net_margin ≥ t` (§2). |

## 2. D1 — the seller's target margin and `pricing-rule/v2`

### 2.1 The setting

**Where.** The target margin is a field of the M4 pricing context, `target_net_margin`. It is held in the store's registration target policy (ADR-0015 §2, `app/stages/register/target_policy.py`). There is one value per `marketplace_key × marketplace_account_id`, like the fees it is computed against (A1).

**Audited.** A save appends one target-policy revision, as every Settings save does: a server-created identity, a fingerprint, an audit record, append-only.
- The content version becomes `registration-target-policy/v2`.
- The pricing context becomes `pricing-context/v2` and names `target_net_margin` in its canonical form, so the context fingerprint covers it.

**Value.** Exact decimal text of a percentage with at most two decimals, parsed to a `Fraction`, never a `float`.
- The accepted range is `−100% < t < 100%` (O2, A2).
- A value with `t ≥ 1 − fee_rate − other_cost_rate` leaves no reachable price. It is refused at save (`TARGET_POLICY_TARGET_MARGIN_UNREACHABLE`), and the refusal names the largest reachable value.
- **No minimum exists.** 0% and a negative value are saved when the seller confirms the warning of s1.

**The first save (O3).**
- A revision appended before slice L1 keeps `pricing-context/v1`. It is priced under `pricing-rule/v1` (35% target, 10% minimum), exactly as today.
- Nothing is backfilled or rewritten.
- The Settings field is prefilled with 35%, and the price row shows the prompt of s1 until the seller saves once.

**Not the FX margin.** ADR-0038's `fx_margin_percent` (「환전 마진」) is a landed-cost input and is unchanged. The two are labelled apart.

### 2.2 `pricing-rule/v2`

The canonical rule (ADR-0013 §7, rule §6.1) is unchanged:

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

What changes under v2:
- **The target price.** `target_margin_price` is the least whole-KRW price whose exact net margin is at least the context's `t`: the existing search, with `t` in place of the constant. Rounding stays `CEIL_KRW_1`. No `max(target, minimum)` is ever restored.
- **No minimum margin exists.** `MINIMUM_NET_MARGIN` does not apply to a v2 snapshot, and a v2 snapshot records none.
- **The guards are warnings** (A3). None blocks readiness. Each is shown wherever the price is shown: 통합DB, the editor's price row, and the product and batch packets (§3.2).

| guard (v2) | holds when | readiness reason |
| --- | --- | --- |
| `LOSS` → `PRICE_LOSS` | `expected_net_profit_krw < 0`: the margin is below 0%, counting every cost the policy defines (fee and other policy costs included) | `Reason(PRICE_LOSS, READY)`, the warning form ADR-0039 SP-04 already uses |
| `BELOW_TARGET_MARGIN` → `PRICE_BELOW_TARGET_MARGIN` | `expected_net_margin < t`. Only a `MINIMUM_SALE_PRICE` basis can produce it | `Reason(PRICE_BELOW_TARGET_MARGIN, READY)` |

- **The primary guard.** `price_guard` is `LOSS` when a loss applies, else `BELOW_TARGET_MARGIN` when that applies, else `OK`. Every applicable reason is listed, as in v1.
- **An unreachable target.** `TARGET_MARGIN_UNREACHABLE` stays `REVIEW_REQUIRED` for a context whose rates leave no price. §2.1 refuses such a save, so it arises only from a fee change.
- **Fingerprints.** The dependency fingerprint already names `pricing_rule_version` and the context fingerprint, so a v2 context reprices every Item under it. The old snapshots become historical (`STALE`), new ones are created, and nothing is updated in place (ADR-0013 §7).

### 2.3 Storage — migration `0071`

`0071_pricing_rule_v2` changes both snapshot tables, `pricing_snapshots` and `atomic_sku_pricing_snapshots`, and nothing else.

**By rule version.** Every CHECK that pins the v1 policy keeps pinning it, unchanged in meaning, for rows with `pricing_rule_version = 'pricing-rule/v1'`:
- `target_margin = 35/100` and `minimum_margin = 10/100`;
- the v1 `LOSS` definition, `purchase_cost + supplier_shipping + platform_fee ≥ final_sale_price`;
- `BELOW_MIN_MARGIN` and both guard-reason CHECKs.

**v2 rows.** `rule_known` admits `pricing-rule/v2`. A v2 row records:
- the context's exact target margin (`target_margin_denominator ≥ 1` and `−denominator < numerator < denominator`);
- **NULL** minimum-margin columns;
- `price_guard IN ('OK', 'LOSS', 'BELOW_TARGET_MARGIN')`;
- guard and guard-reason CHECKs that encode §2.2 exactly.

A v1 row may not have NULL minimum-margin columns.

**How.** SQLite cannot alter a CHECK in place, so both tables are rebuilt:
- every row is copied unchanged;
- the append-only and current-state triggers, the indexes and the foreign keys are recreated;
- `app.platform.db.schema_contract` is updated.

No row is rewritten, so the append-only triggers never fire on existing rows.

**Downgrade fails closed** while any v2 snapshot exists.

### 2.4 What does not change

- the canonical rule, the supplier minimum-sale-price policy (ADR-0039), the landed cost (ADR-0038), the exact arithmetic, the rounding and the dependency model;
- every v1 snapshot, and every RegistrationItemSnapshot that pins one;
- `documents/acceptance/milestones/M4.md`, which is historical v1 evidence;
- an estimated margin is never the settled margin (ADR-0013 §7, OPERATE's).

## 3. D2 — the in-app LIVE approval

### 3.1 What one approval covers

**Scope.** One store account, and 1 to 500 products in a fixed order (O5). A selection over 500 is refused; more products need more approvals.

**The canary.** The first real registration of an account is a single approval (O5).
- A batch approval is refused for an account until one ICBM registration on it is `ACTIVE`: created by ICBM and confirmed by read-back (ADR-0014 §11).
- A canary registered earlier through `icbm live` counts.
- An adopted or imported listing does not (ADR-0024, ADR-0042).

**Grants.** Grants are issued per product, when that product's run reaches the step that needs them, never ahead of it (A5). Each is exact, and nothing is shared between products.
- **ASSET: one grant per artifact that needs transmission, each with budget 1.**
  - Each binds the exact ASSET unit of ADR-0018 §3.2: the preparation revision, the candidate fingerprint, the exact selected artifact set and the asset profile.
  - Each also binds **its one artifact** (kind, SHA-256, derivation identity). An upload of any other artifact never matches it.
  - An artifact whose exact bytes are already `APPLIED_PROVEN` for the same account, endpoint and asset profile gets no grant. It is locally rebound (ADR-0018 §3.4), including when the bytes were uploaded earlier in the same batch.
  - A product that needs no upload gets no ASSET grant.
- **CREATE: one grant with budget 1.**
  - It binds the exact Snapshot, Intent, idempotency key and attempt number (ADR-0018 §3.2).
  - It is issued after that product's freeze, and only if the freeze reproduces what was approved (§3.6 step 6). No Intent exists before the freeze (ADR-0018 §3.1).
  - There is at most one CREATE grant per product per approval.
- **Expiry.** Every grant expires 30 minutes after its product's run started, and never after its segment ends (§3.5). That is D2's short expiry, per product.
- **Reference.** Every grant's authorization reference is the approval (§8: a typed in-app reference), not a GitHub comment.

These grants are narrower than any the CLI can issue: one artifact per ASSET grant, budget 1 everywhere, a 30-minute window per product. None of these exists: a wildcard, a unit-less grant, a grant of another stage, a grant that matches two products. A CLI-issued grant is unchanged (§3.10).

### 3.2 The packets

**The product packet.** The server builds it deterministically from the canonical owners, as ADR-0018 §5.1 builds the eligibility packet. It holds only sanitized, seller-visible material and the identities that bind it:
- the marketplace, the canonical account and the store's display name;
- the preparation identity and revision, the candidate fingerprint and the asset profile;
- the selected artifact set (kind, SHA-256, derivation identity), and which of those artifacts need transmission;
- the publication-facing listing content: name, category path, seller code, options summary and delivery summary;
- per Item: the pinned `PricingSnapshot` identity, the final sale price, the price basis, the expected net profit and margin, and every guard warning (§2.2);
- the residual-risk statement digest of §5.2;
- the packet version, `live-approval-packet/v1`.

A product may already be frozen, because the seller pressed 「등록 내용 확정」 earlier. Its packet is then built from the frozen Snapshot and its `PREPARED` Intent, and its run skips the ASSET steps (§3.6).

**The batch packet** (`live-approval-batch/v1`) holds:
- the account;
- the ordered list of `(preparation identity, product packet digest)`;
- the count;
- the summary the dialog shows: every name, the price range, the warnings by kind with their products, the total of artifacts to transmit, and the excluded selections with their reasons.

Its canonical JSON is digested with SHA-256 into the **batch digest**. A single approval is a batch packet of one product.

**The client never builds a packet.** It displays the server's packet and echoes back its batch digest.

### 3.3 Preconditions — a derived, read-only readiness

A `GET` of the approval readiness of one product or of a selection answers every precondition, and names every missing one by its code. It has no side effects.

**Account-level preconditions.** Each must hold at approval and at every 「계속」:
- the store account is bound, and CONNECT holds a proven current committed session (ROADMAP §14 item 4);
- the protected-write brake is `RELEASED` (§3.9);
- the residual-risk acceptance of §5.2 is recorded for this account;
- the visual acceptance of §5.1 is recorded for the running build;
- the image upload and CREATE endpoints, and the positive-only reconcile path, are adopted;
- the ADR-0014 §26 execution scope of the CREATE endpoint group is `ACTIVE`;
- no other approval of this account is open, `RUNNING` or `PAUSED` (A7);
- no LIVE window opened by another path is open;
- for a batch: the account has its verified first registration (§3.1).

**Product-level preconditions.** A selected product is **included** only when all of these hold:
- its candidate preflight is `READY`, or it is frozen with a `PREPARED` Intent;
- no unresolved `UNKNOWN` CREATE or conflict scope covers it;
- no started or `UPLOAD_UNKNOWN` attempt fences any of its artifacts' replay keys (ADR-0018 §3.4);
- it is not locked by another approval.

A product that fails any of them is **excluded**, with its reasons, before the batch digest is computed. The seller sees each exclusion in b2, and nothing about it is approved. A single approval refuses instead of excluding.

**This readiness is never permission.** Grants are issued only by the run, under the approval, and the send-time stack decides every mutation (§6).

### 3.4 Approving

The route is `POST /api/v1/register/live-approvals`, with the body `{request_id, account, preparation_ids, batch_digest}`. The server takes these steps.

1. **It accepts only a browser request of this app** (§4):
   - a loopback host;
   - the `X-ICBM-Client` header;
   - `Content-Type: application/json`;
   - an `Origin`, when present, equal to the app's own origin.

   An extension origin is refused.
2. **It is idempotent by `request_id`.** The client creates one `request_id`, a UUID, when the dialog opens.
   - The same `request_id` again returns the same approval and records nothing more.
   - A `request_id` is never reused for another selection or packet.
3. **It allows one open approval per account.** While an approval of this account is open, a new one is refused with `LIVE_APPROVAL_ALREADY_OPEN`. The refusal carries the open approval's identity, so the UI can show its progress.
4. **It rebuilds the batch packet** from the same selection, immediately before the write. It refuses when:
   - the digest differs from the submitted one: `LIVE_APPROVAL_PACKET_MOVED`, and only the audit event is recorded;
   - more than 500 products are included: `LIVE_APPROVAL_TOO_MANY`;
   - a batch is for an account with no verified first registration: `LIVE_APPROVAL_CANARY_REQUIRED`.
5. **It re-derives every account-level precondition of §3.3.** Any failure refuses, and the refusal names every failing precondition.
6. **It writes one unit of work**, which:
   - records the approval: its ordered products with their packet digests, the approving user's identity (`config.operator_actor`), the batch digest and the correlation identity;
   - opens the first segment (§3.5);
   - locks every included product against edits (A10);
   - writes the audit events;
   - enqueues the first product run.

**No grant is issued here.** Grants are issued by each product run (§3.1).

The confirmation the seller sees is an input to this protected action, never the authority (ADR-0018 §3.3). No confirmation prose exists to persist, and nothing the seller types is stored.

### 3.5 Segments, expiry and windows — the exact rule

**A segment** starts when the approval is recorded, or when a 「계속」 is accepted.
- It ends **four hours** after it started: `LIVE_WINDOW_MAX_S`, equal to the grant maximum `MAX_GRANT_WINDOW_S`.
- It ends sooner if the approval ends.

**Starting a product.**
- A product run starts only while at least 30 minutes of its segment remain.
- Its grants expire 30 minutes after it started, so no grant outlives its segment.

**Pausing.** When less than 30 minutes remain, no further product starts. The current product finishes inside its own 30 minutes. The approval then becomes **`PAUSED`**, with no grant active and no window open.

**A `PAUSED` approval** waits for 「계속」 or 「남은 상품 승인 취소」.
- It survives a restart, because nothing is in flight.
- If not continued within 24 hours of pausing, it ends `EXPIRED` by itself and unlocks its remaining products (A13).

**Windows.** Each product run opens at most two bounded LIVE windows, through the execution-mode owner, each inside a live grant of that product:
- one for its uploads, closed before its freeze;
- one for its CREATE, opened after its CREATE grant exists.

No window spans a freeze, outlives its product's grants, or outlasts its segment. Every window closes when its step ends, whatever happens, and a restart never restores one (ADR-0018 §2). While an approval of any account is open:
- the execution-mode route opens no other window;
- a `DRY_RUN` request closes the current one.

**「계속」** is one more explicit confirmation by the seller, on its own dialog (b5), with its own `request_id`. The server:
1. re-derives every account-level precondition (§3.3);
2. rebuilds each remaining product's packet and compares it with the digest approved for it. **Any difference ends the approval `STOPPED`** (`LIVE_APPROVAL_PRODUCT_MOVED`), naming the product;
3. opens a new segment and enqueues the next product run.

The remaining products' grants are then issued by their runs, inside the new segment, exactly as in the first one.

So nothing lasts longer than ADR-0018's four hours:
- a grant lasts at most 30 minutes;
- a window lasts at most the step it serves;
- a segment lasts at most four hours.

### 3.6 The product run

Each product run is its own durable job of the single worker (ADR-0002, ADR-0005), so other work interleaves between products, as in ADR-0014 §29's sequential bulk orchestration.
- **No retry.** The job is declared **not idempotent**, with **one attempt**. A restart while it is `RUNNING` makes it `DEAD` (`INTERRUPTED_OUTCOME_UNKNOWN`), and the approval ends **`INTERRUPTED`** (A9). It never resumes.
- **It decides nothing itself.** Each step calls the existing owner, and that owner's answer decides.
- **Before every step**, it re-reads the approval, the brake, the segment and the clock. The run ends before its next step if the approval is no longer `RUNNING` or the product's 30 minutes have passed.

**Step 1 — check the product.**
- **Does:** rebuilds this product's packet and re-derives its product-level preconditions.
- **Owner:** the packet builder (§3.2).
- **Stops the whole approval when:** the digest differs from the approved one, or a precondition no longer holds (`LIVE_APPROVAL_PRODUCT_MOVED`).

**Step 2 — prove evidence retention.**
- **Does:** proves retention, unless a proof taken in this segment is still current.
- **Owner:** `RetentionProofService.prove` (ADR-0018 §8).
- **Stops the whole approval when:** the proof is not `PASSED`.

**Step 3 — issue the ASSET grants.**
- **Does:** issues this product's ASSET grants (§3.1).
- **Owner:** `LiveAuthorityService`.
- **Stops the whole approval when:** it refuses.

**Step 4 — the ASSET restore drill.**
- **Does:** takes the ASSET restore drill of this product's artifacts, into a fresh root (A11).
- **Owner:** `RestoreDrillService.drill_asset` (ADR-0018 §7).
- **Stops the whole approval when:** the drill is not `PASSED`.

**Step 5 — upload.**
- **Does:** opens the upload window, uploads each artifact that needs it once, and closes the window.
- **Owners:** `ExecutionModeService`, and `AssetUploadService.upload` as `upload_run.py` composes it today, now in the server process.
- **Stops the whole approval when:** the send-time stack refuses, or any upload ends `UPLOAD_UNKNOWN` or `NOT_APPLIED_PROVEN`. The product's remaining artifacts are not uploaded, and nothing is frozen (A14). Each ASSET grant has budget 1, so nothing is retried.

**Step 6 — freeze.**
- **Does:** re-evaluates the final preflight and freezes.
- **Owner:** `RegistrationPreparationService.freeze_current` (ADR-0018 §3.1 amendment notes).
- **Stops the whole approval when** (`LIVE_APPROVAL_FREEZE_MOVED` for either mismatch):
  - the freeze refuses;
  - the frozen Snapshot does not derive from **exactly** the approved preparation revision, candidate fingerprint, artifact set and pricing snapshots;
  - a packet rebuilt from the Snapshot has a digest other than the approved one.

**Step 7 — issue the CREATE grant.**
- **Does:** issues the CREATE grant, with budget 1, the product's expiry and the approval's reference.
- **Owner:** `LiveAuthorityService`.
- **Stops the whole approval when:** it refuses.

**Step 8 — the CREATE restore drill.**
- **Does:** takes the CREATE restore drill, after the freeze.
- **Owner:** `RestoreDrillService.drill_create`.
- **Stops the whole approval when:** the drill is not `PASSED`.

**Step 9 — send.**
- **Does:** opens the CREATE window, sends the CREATE once, and closes the window.
- **Owners:** `ExecutionModeService`, and the REGISTER execution owner's existing `register.create` path, which the send-time stack admits (§6).
- **Stops the whole approval when:** the stack refuses, or the outcome is `UNKNOWN`. A definitive provider rejection (`NOT_APPLIED_PROVEN`, 등록실패) fails only this product (A16).

**Step 10 — finish.**
- **Does:**
  - revokes this product's unspent grants;
  - records its result;
  - enqueues the next product's run, unless the approval was stopped or paused, or the seller pressed 「중지」.
- **Owners:** `LiveAuthorityService` and the approval owner.

After an applied CREATE, read-back verification runs as today (ADR-0014 §11). The product's result is the registration read state (ADR-0014 §28.5).

A frozen product runs steps 1, 2 and 6–10, with no ASSET grant. Its step 6 only checks the existing Snapshot against its approved packet.

### 3.7 The batch

**Sequential.** Products run one at a time, in the approved order. No two product runs, of any approvals, ever overlap: the single worker runs one job at a time, and at most one window exists.

**Stop at the first doubt (O5).** The whole approval ends **`STOPPED`** at the first of these:
- an `UNKNOWN` CREATE;
- an `UPLOAD_UNKNOWN`;
- an engaged brake;
- a drift, packet, fingerprint or freeze mismatch;
- a failed retention proof or restore drill;
- any send-time stack refusal;
- a `NOT_APPLIED_PROVEN` upload;
- a grant that expired before its mutation.

The products after it are left untouched and unlocked. They need a new approval.

**A definitive rejection does not stop the batch (A16).** Naver's documented rejection of one CREATE proves that nothing was registered. Only that product fails (등록실패, with the store's reason), and the batch continues.

**「중지」.** It is available at any time, and ends the approval **`STOPPED_BY_SELLER`** between products. The current product finishes its run, and no request in flight is interrupted. No further product starts.

**「실제 등록 잠그기」.** It engages the brake, which stops every mutation not yet started (ADR-0018 §4.1). It therefore stops the batch before the current product's next mutation.

**「승인 취소」 (single approval only).** It ends the approval before the product's next step. It is available until the CREATE is handed to the transport.

**Results are per product:** 등록성공, 재확인필요, 등록실패, or 보내지 않음 with its reason. One product's result never changes another's.

### 3.8 How an approval ends

An approval is open while **`RUNNING`** or **`PAUSED`**. It ends exactly once:

| state | meaning |
| --- | --- |
| `COMPLETED` | Every included product ran. |
| `STOPPED` | A stop condition of §3.7, or a moved product at 「계속」. The code, the product and the step are kept. |
| `STOPPED_BY_SELLER` | 「중지」, 「승인 취소」 or 「남은 상품 승인 취소」. |
| `EXPIRED` | A `PAUSED` approval was not continued within 24 hours, or a single approval's product did not start within its segment. |
| `INTERRUPTED` | The process stopped while a product run was `RUNNING`. Its started attempts are judged by their owners' crash rules (ADR-0018 §3.4, G3-25; ADR-0014 §9). |

- **Every ending** revokes every unspent grant, closes any window, and unlocks every product of the approval.
- **No ending is reopened, and no approval is ever reused.** Another try is another approval, over freshly built packets.
- **An attempt in flight completes as itself.** An attempt already handed to the transport before the approval ended, or before the brake engaged, completes as the one attempt it is. Its outcome is recorded under ADR-0014 §9–§10 (ADR-0018 §4.2).

### 3.9 The brake on the screen

The protected-write brake keeps every rule of ADR-0018 §4.1 and §4.2:
- one global brake, durable and fail-closed, that survives a restart;
- engaging stops every mutation not yet started;
- releasing resurrects no grant and widens none.

What changes is only where it is operated (A8):
- **「실제 등록 잠금 해제」** is a separate, explicit in-app action, with its own confirmation dialog (s4).
  - It is the "new explicit authorization" of §4.1: the server records the release with the actor, a typed in-app authorization reference (§8) and the audit event.
  - It never happens as a side effect of anything else: an approval, a 「계속」, a Settings save, a connect or a restart.
- **「실제 등록 잠그기」** is one press, with no dialog. It is always visible on the register page and on every progress panel.
- **An approval never releases the brake**, and a released brake approves nothing.
- **The CLI stays:** `icbm live release-brake` and `engage-brake` keep working (§3.10).

### 3.10 The CLI stays

Every `icbm live` command keeps working exactly as ADR-0018 §4.1 describes, with its GitHub authorization reference. It serves the operator's recovery and development.

The in-app path is a second entry to the same owners, never a second owner. No duplicate grant, brake, drill, retention or upload model exists.

### 3.11 Relation to ADR-0042 S2c and ADR-0014 §29

**ADR-0042 S2c.** ADR-0042 §11 A4 adds a UI-issued batch approval of the UPDATE stage, with its own rule: at most 50 listings and 30 minutes. That rule is unchanged.
- Both ADRs need the same widening of `live_grants` and `protected_write_brakes` to an in-app authorization reference (§8). Whichever slice lands first makes it; the other reuses it.
- The approval records stay in their own stage owners: REGISTER approvals here, UPDATE batch approvals in StoreUpdate (ADR-0042 §12).

**ADR-0014 §29.** `POST /api/v1/register/bulk-creates` stays local orchestration: it issues no grant and opens no window.
- A batch approval does not use it. Its sequencing only follows §29's pattern: one job per product, and exactly one successor queued.
- The two keep apart:
  - a product reserved by a running bulk-create run is excluded from a selection;
  - a product locked by an approval is refused by bulk-create.

## 4. Threat model

**A stray click.**
- Two deliberate actions are needed: the button, then the confirm button of a dialog where 「취소」 is focused and Enter and Esc cancel.
- The button is disabled until every precondition holds and the packet has loaded.
- The brake is a third, separate action, never implied (§3.9).

**A double click or a retried request.**
- The `request_id` makes approving and 「계속」 idempotent: a repeat returns the same approval and records nothing (§3.4).
- Grants have budget 1, so a repeated send finds no budget.

**A replayed request, later.**
- A `request_id` belongs to one approval forever. A replay after that approval ended returns it and records nothing.
- A new approval needs a new dialog, freshly built packets, and every precondition met again.

**A request from another origin (CSRF).**
- `TrustedHostMiddleware` admits only `127.0.0.1` and `localhost` (`app/main.py`), which also defeats DNS rebinding.
- `ClientHeaderGuard` refuses a state-changing `/api/` request without `X-ICBM-Client` (`app/interface/api/middleware.py`). A browser sends a custom header cross-origin only after a CORS preflight, and no CORS is configured.
- The approval, 「계속」, brake and acceptance routes also require `Content-Type: application/json`, and refuse an `Origin` other than the app's own.
- `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop clickjacking.
- No `GET` has a side effect.

**The capture extension.**
- The extension also sends `X-ICBM-Client` (`ui/extension/lib/signing.js`).
- The approval, 「계속」, brake and acceptance routes refuse an extension origin, and any request carrying the extension's signature headers.

**Another local process.**
- This is out of scope, by trust boundary. A process that can call the loopback API with forged headers can also run `icbm live` with the data directory.
- This ADR gives such a process no capability that the CLI did not already give it.

**A second tab.**
- At most one approval per account is open (A7). The second tab gets `LIVE_APPROVAL_ALREADY_OPEN` and shows the running approval's progress.
- Every product of an open approval is locked against edits (A10).

**An expired approval or grant.**
- A grant expires 30 minutes after its product started, and never after its segment ends.
- The run re-reads the clock before every step.
- A paused approval issues nothing, and it ends by itself after 24 hours (§3.5).

**A batch running unattended for too long.**
- A segment ends after four hours.
- Continuing needs the seller's explicit 「계속」, which re-checks every account-level layer and every remaining product's digest (§3.5).

**A stale or tampered selection.**
- The batch digest binds the ordered product digests, the count and the summary the seller saw.
- The server rebuilds it before recording, and a mismatch refuses (§3.4).

**A product changed after approval.** Several checks stop it, and any mismatch stops the whole approval:
- the product packet binds the preparation revision, candidate fingerprint, artifact set and pricing snapshots;
- every product of the approval is locked;
- step 1 compares the product's digest before anything is issued;
- each ASSET grant stops matching on drift (ADR-0018 §3.2);
- the freeze must reproduce the approved packet, or no CREATE grant is issued;
- the CREATE grant binds the exact Intent;
- the send-time truth fence still runs at admission.

**A price or margin setting changed after approval.**
- The pinned pricing snapshots are in each packet.
- A Settings save that reprices makes them historical. Step 1 or the freeze then fails to reproduce the packet, and the approval stops.

**An `UNKNOWN` CREATE.**
- It is never resent (ADR-0014 §10, §28; G3-07). The CREATE grant had budget 1 and is spent.
- The whole approval stops (§3.7).
- The product's conflict scope stays closed. §3.3 excludes it from every later approval until positive-only reconcile, or proven non-application, ends it.
- The seller sees 재확인필요.

**An `UPLOAD_UNKNOWN`.**
- It is never retried (G3-22).
- The product is not frozen, and the whole approval stops.
- Its replay key stays fenced across every later approval (G3-29).

**A systemic fault repeated 500 times.**
- Every failure other than a documented, definitive provider rejection stops the whole approval at its first occurrence (§3.7).

**The first registration of a new store.**
- A batch is refused until one single-product registration on the account is created and confirmed by read-back (§3.1).

**A crash or restart mid-run.**
- The window is gone with the process memory.
- The product run job is `DEAD` and never retried, and the approval ends `INTERRUPTED`.
- Unspent grants are revoked at startup.
- A started attempt is judged by its owner's crash rule (ADR-0018 §3.4, G3-25; ADR-0014 §9).

**Starving other work.**
- There is one job per product, so collection, sync and other jobs interleave between products.

**The seller changes their mind.**
- 「중지」 stops between products, 「승인 취소」 cancels a single approval, and 「실제 등록 잠그기」 works at any time.

**An agent acting for the seller.**
- LAP-13: an agent never gives, clicks or simulates an approval, a 「계속」, a brake release or a residual-risk acceptance.
- A slice acceptance that reaches a LIVE step is run with the owner's own click, under the owner's go-ahead for those products (§9).

## 5. D3 — the two acceptances, where they belong

### 5.1 The visual acceptance, once per release build

ADR-0018 §9 is unchanged in **what** it accepts: the populated first-vertical scenario, at the accepted viewport set, with no server-owned blocker hidden. What changes is **what it is bound to**, and **where it lives** (A12).

**A release build's identity** is three values:
- the running **code digest** (`app.platform.core.code_identity`, unchanged): every file of `app` and `integrations`, the served UI, and the dependency pins;
- the **schema head** its migrations build;
- a **release identifier** from its manifest.

The git commit becomes provenance in the manifest when one exists, never a requirement.

**The release manifest**, `release/release-manifest.json`, names:
- the release identifier;
- the code-identity version and the code digest;
- the schema head;
- the commit, if any.

**The acceptance record**, `release/visual-acceptance.json`, is the reviewed harness report's sealed summary. It holds:
- the report digest, the harness version, the scenario and the viewport set;
- the target and check counts, and the sanitized evidence;
- the reviewer and the review reference;
- the release identity it accepted.

The `release/` directory is outside the code digest's file set, so recording the acceptance never changes the digest it binds.

**Recording** is the developer's, once per release build. It uses the existing harness and the `verify_report` contract, through the existing command family: `icbm live record-visual-acceptance --release`. A passing run alone still records nothing, and the reviewer and review reference are still required.

**Reading.** At startup, the server verifies the shipped record against the running build:
- the release identity equals the manifest's;
- the code digest equals the one it computes now;
- the schema head equals the database's head;
- the record's seal verifies.

Only then does `VISUAL_ACCEPTANCE_RECORDED` hold, and the send-time layer reads it for every mutation, as before. Any difference, one changed file included, makes the layer refuse.

**A build whose code identity cannot be computed records nothing.** A packaging format that does not ship the files the code digest covers is not a release build under this ADR. It fails closed, and is never trusted.

**The integrity binding is not a lock against the machine's owner.** Someone who can edit the shipped record can also edit the code it binds. The seal proves that the record belongs to exactly this code, which is what ADR-0018 §9 needs.

**The git-checkout path stays** for development: a record bound to the exact commit and code digest, as today (`G3-VISUAL.md`).

**The approval surfaces join the harness**, at both viewports, and the harness version becomes `g3-visual-acceptance/v4`. The new surfaces are:
- the readiness lines of p2 and b1;
- both approval dialogs, with their margin warnings and exclusions;
- both progress panels, including the paused state with 「계속」;
- the brake card.

### 5.2 The residual-risk acceptance, once per account, in the app

The risk is ADR-0018 §6.1's and ADR-0014 §28.7's, word for word. `RESIDUAL_RISK_STATEMENT` and its SHA-256 are unchanged:

> a product may be live in SmartStore after an ambiguous CREATE while ICBM has not yet recovered the provider identity, leaving that listing temporarily outside normal confirmed price/stock monitoring.

The seller reads it in Korean (s3): 「네이버 응답이 불확실하면, 상품이 실제로는 등록되었는데 ICBM이 아직 상품번호를 확인하지 못해 가격·재고 감시 밖에 잠시 놓일 수 있습니다. ICBM은 그 상품을 다시 보내지 않고, 확인될 때까지 ‘재확인필요’로 표시합니다.」

**Recorded in the app, by the owner of the account (O4)**, once per `marketplace_key × marketplace_account_id`.
- The route is `POST /api/v1/live/residual-risk-acceptances`. It carries the server-built statement packet's digest, which covers the canonical statement's digest, the Korean text's digest and the account.
- The record keeps the actor, the time, both digests, and the contract version `adr-0043-residual-risk-in-app/v1`.

**The architect's half is given at contract level (O4).**
- ADR-0018 §6.1 asked for "a separate, explicit user and architect acceptance".
- The owner, deciding as architect in Issue #219 `6200578768` and `6201001579`, accepted this risk for every account whose owner accepts it in the app.
- The record names that decision as its contract reference. No per-account GitHub comment is needed.

**Withdrawable.** 「수락 철회」 appends a withdrawal. The layer then refuses for that account until a new acceptance. Records are append-only, and nothing is deleted.

**A changed statement needs a new acceptance.** A different statement digest, or a different contract version, proves nothing.

**Existing proofs stay valid.** A GitHub-form proof recorded by `icbm live record-residual-risk-acceptance` still satisfies the layer for its account.

**It still authorizes nothing.** It is one layer (ADR-0018 §10), and every other layer still decides.

## 6. What stays exactly as it is

Every mutation an approval leads to is admitted only by **every** layer of ADR-0018 §4.3, at send time, in the one unit that starts its attempt. The approval satisfies none of them by itself. The layers:
- the execution mode is `LIVE`;
- the brake is `RELEASED`;
- an `ACTIVE` grant of the stage matches its exact unit, with budget left;
- the stage's readiness is `READY`: restore proof, retention, visual acceptance, residual risk and the upload-attempt owner;
- the endpoint is adopted;
- for CREATE, the ADR-0014 §26 scope is `ACTIVE`;
- the attempt starts together with its budget consumption;
- the stage's own gate passes;
- the truth fence holds.

Unchanged, and still binding as written:
- **ADR-0018 invariants.**
  - G3-01 to G3-17, and G3-19 to G3-29.
  - G3-30, except who gives the architect's half (§5.2).

  In particular:
  - G3-02: deny by default;
  - G3-03: the grant is the authority, and UI text never is;
  - G3-04: budget is consumed at start and never refunded;
  - G3-05: terminal states are terminal;
  - G3-07: no blind replay;
  - G3-08 to G3-10: the brake;
  - G3-11: the §26 scope brake is CREATE-only and not weakened;
  - G3-16: restore proofs are stage-bound and freshness-bound;
  - G3-17: retention;
  - G3-21: exact units, no wildcard;
  - G3-22 and G3-24 to G3-29: the upload-attempt owner and its replay fence;
  - G3-23: no raw confirmation prose.
- **ADR-0014.**
  - §9–§11, §15, §26 and §28 are unchanged.
  - §24 and §29 are amended only as §7 states.
  - §28.7 changes only in who gives its architect's half (§5.2).
- **Other ADRs:** ADR-0011, ADR-0016, ADR-0039, and ADR-0042 including S2c's rule of at most 50 listings and 30 minutes.
- **The execution-mode owner's rules:**
  - `DRY_RUN` at boot and after every restart;
  - one bounded window in process memory, at most four hours, never past its scope, and never widened.

## 7. Amendments, each effective when its slice lands

| document | amendment | slice |
| --- | --- | --- |
| **ADR-0013 §7** ("The registration guard") | Under `pricing-rule/v2`, the target margin is the pricing context's `target_net_margin`, which is the seller's Settings value.<br>No minimum margin applies.<br>`LOSS` (net profit below 0) and `BELOW_TARGET_MARGIN` are warnings, never "not registerable".<br>`pricing-rule/v1` is unchanged for every v1 snapshot. | L1 |
| **ADR-0013 §8** (pricing readiness) | "a guard of `LOSS` or `BELOW_MIN_MARGIN` gives `BLOCKED`" holds for v1 snapshots only.<br>A v2 guard gives a `READY`-status warning reason (§2.2). | L1 |
| **ROADMAP "Pricing contract"** | A note: the target net margin is the seller's per-store Settings value, with no floor (ADR-0043 §2).<br>The canonical rule is unchanged. | L1 |
| **ADR-0018 §4.1**, and its amendment notes | **Amended.** Beside the `icbm live` family:<br>· the in-app actions of §3.9 also release and engage the brake (L2);<br>· the in-app approval and its server-side product runs also issue grants, and also run the upload, the restore drills and the retention proofs (§3; L4a, L4b).<br>For these, "never a new screen or route" and "never a screen or route" no longer hold.<br>The upload may run in the server process, inside its product's window.<br>Every other sentence stands. | L2, L4a |
| **ADR-0018 §2**, amendment note | The note reads: "issuing the grant is the protected action that carries the user's approval … recording its GitHub evidence is the agent's bookkeeping".<br>For an in-app approval, the approval itself, and each 「계속」, is that protected action. Its durable record is the evidence, and no GitHub record exists for it. | L4a |
| **ADR-0018 §3.2** ("approval" row) | The approval reference is either the GitHub authorization reference (CLI) or the in-app approval (§8).<br>An approval-issued ASSET grant also binds its one artifact (§3.1). | L4a |
| **ADR-0018 §3.3** | A note: the in-app approval is a server-validated protected action, under which the server issues exact per-product grants.<br>"UI text and checkboxes are never authority" stands: the grants are the authority. | L4a |
| **ADR-0018 §6.1**, and its amendment notes; **ADR-0014 §28.7** | The user's acceptance is recorded in the app, once per account (§5.2).<br>The architect's acceptance is given at contract level, by Issue #219 `6200578768` and `6201001579`.<br>GitHub-form proofs stay valid. | L2 |
| **ADR-0018 §9**, **G3-18**, **G3-31** | A visual acceptance is bound to a release build's identity — code digest, schema head and release identifier — and shipped with it (§5.1).<br>The git-checkout binding stays, for development.<br>"a merged commit is a new accepted code SHA" reads "any change to the running code is a new release identity".<br>The approval surfaces join the required surfaces. | L3 |
| **ADR-0018 §10** table | The residual-risk row reads "recorded for the stage's account (§5.2)".<br>The visual row reads "`VISUAL_ACCEPTANCE_RECORDED` for the running build". | L2, L3 |
| **ADR-0018 G3-30** | "a recorded explicit user and architect acceptance" means the user's acceptance, in the app, per account, and the architect's, at contract level (§5.2). | L2 |
| **ADR-0014 §24** | "the user explicitly authorizes that write scope" is satisfied per approval, in the app (§3).<br>"A real M5 acceptance starts with a single canary product" is kept for every account: a batch needs the account's verified first registration (§3.1). | L4a, L4b |
| **ADR-0014 §29** | A note: the batch approval is not the bulk-create call, and does not widen it.<br>Bulk-create still issues no grant and opens no window.<br>Neither takes a product the other holds (§3.11). | L4b |
| **ROADMAP §14.2** | The residual-risk and visual bullets follow §5.<br>A note names the in-app approval as the user's scope approval: one product, or up to 500 under §3. | L2, L3, L4a |
| **ARCHITECTURE §13** | The visual acceptance is "bound to the running release build".<br>The scope's evidence is the in-app approval record, or, on the CLI path, the agent's GitHub bookkeeping. | L3, L4a |
| **`documents/acceptance/gates/G3-VISUAL.md`** | The release-build recording procedure, beside the git-checkout one. | L3 |
| **`documents/rules/07-execution-safety.md` §7.1–§7.2** | A note (O1, `6201001579`): a seller's in-app approval under ADR-0043 is the user's approval of one bounded LIVE scope, and needs no GitHub record. The scope is:<br>· one store account;<br>· ASSET and CREATE;<br>· at most 500 named products, with exact per-product grants;<br>· segments of at most four hours, each after the first confirmed by 「계속」;<br>· recorded durably in the app.<br>An agent never gives or simulates it.<br>An agent's own LIVE run still needs the GitHub scope and the user's approval. | L4a |
| **Repository rule tests** (`tests/contracts/test_repository_rules.py`) | These tests name the new in-app paths as the only additional callers:<br>· the one-recording-path tests of the visual and residual-risk records;<br>· any pin of CLI-only grant issuance. | L2, L3, L4a |

## 8. Owners and storage (migrations from `0071`, one per slice)

**L1 — M4 pricing and the target policy. Migration `0071_pricing_rule_v2`.**
- **Owners:** `app/stages/products/pricing.py`, `pricing_store.py`, `atomic_sku_economics_store.py`; and `app/stages/register/target_policy.py`.
- **Holds:**
  - `pricing-rule/v2`;
  - `pricing-context/v2`, with `target_net_margin`;
  - `registration-target-policy/v2`.

  Both snapshot tables are rebuilt with CHECKs by rule version (§2.3).

**L2 — live safety: residual risk and brake. Migration `0072_in_app_acceptance_and_brake`.**
- **Owners:** `residual_risk.py`, `store.py`.
- **Holds:**
  - `residual_risk_acceptances` gains the in-app form: an evidence-kind column (`GITHUB_COMMENTS` or `IN_APP`), and, for `IN_APP`, the actor, both digests and the withdrawal record.
  - `protected_write_brakes.release_is_authorized` admits a typed in-app reference beside a comment id.

**L3 — live safety: visual. Migration `0073_release_visual_acceptance`.**
- **Owners:** `visual.py`, `code_identity.py`.
- **Holds:** `visual_acceptances` gains the release binding:
  - `code_sha` becomes nullable;
  - new `release_id` and binding-kind (`GIT_CHECKOUT` or `RELEASE_BUILD`) columns;
  - a CHECK that each kind has exactly its own identity.

**L4a — live safety: approvals. Migration `0074_live_registration_approvals`.**
- **Owner:** new, `app/capabilities/live_safety/approvals.py`.
- **Holds:**
  - `live_registration_approvals`: identity, `request_id` (unique), account, kind (`SINGLE` or `BATCH`), product count (1–500, and 1 for `SINGLE`), batch digest, approver, state, end code, correlation identity.
  - `live_registration_approval_items`: the ordered products. Each has its preparation, revision, candidate fingerprint, packet digest, result and end code, and at most one CREATE grant (unique).
  - `live_registration_approval_segments`: each segment's start, end and confirming `request_id`.
  - `live_grants`, rebuilt as `0057` did:
    - `authorization_ref` is typed as a comment id or an approval-item identity (`authorization_kind`, with an FK for the latter);
    - `asset_artifact_sha256` is added, for an approval-issued ASSET grant.

  The whole schema serves single and batch approvals.

Rules that apply to every owner above:
- Tables are append-only, with delete guards, as in the canary scope (ADR-0018 §8).
- Approval, segment, item and grant transitions are audited in the same unit.
- L4b needs no migration, because §8's schema already holds a batch. If L4b's review finds a schema need, it takes `0075`.
- **The numbers are reservations.** ADR-0042's `0068`–`0070` may not have landed when a slice here lands. Its migration then still chains by `down_revision` to the head at its merge, and no slice renumbers another's reservation.

## 9. Slices

**Every slice is accepted by a seller walkthrough on the real UI** (owner principle 2026-10-15, 「실제 사용자 기준」). The agent runs the §0 steps the slice delivers, in the running application:
- at desktop and mobile width;
- with screenshots of what the seller sees at each step, recorded in the slice's acceptance record.

**A step that registers for real (p3–p5, b2–b6) runs only with the owner's go-ahead for those exact products.** The owner presses 「승인하고 등록」, 「승인하고 등록 시작」, 「계속」 and 「실제 등록 잠금 해제」 themselves (LAP-13).

Tests and CI are necessary, and never sufficient.

| # | slice | seller walkthrough at acceptance | tier |
| --- | --- | --- | --- |
| P0 | this ADR | — | docs |
| **L1** | §2: <br>· `pricing-rule/v2` and `pricing-context/v2`;<br>· the Settings field 「목표 순마진」, with its range and warnings;<br>· the readiness warnings;<br>· the price-row and 통합DB display;<br>· migration `0071`. | s1–s2:<br>· set 25%, and see prices recompute;<br>· set −5%, see the warning, and save;<br>· see a loss product shown as 「손실 판매」 and still 「등록 준비 완료」;<br>· see an old store keep 35% until saved. | `HIGH_RISK`: it removes a registration guard, and rebuilds two pricing tables |
| **L2** | §5.2 and §3.9:<br>· the in-app residual-risk acceptance and its withdrawal;<br>· 「실제 등록 잠금 해제」 and 「실제 등록 잠그기」;<br>· migration `0072`;<br>· the route guards of §4. | s3–s4:<br>· accept, withdraw, and accept again;<br>· release and engage the brake (the owner presses release);<br>· see a cross-origin request refused. | `HIGH_RISK`: a safety gate's authorization path |
| **L3** | §5.1:<br>· the release manifest and the shipped acceptance record;<br>· startup verification;<br>· `--release` recording;<br>· the approval surfaces in the harness (`g3-visual-acceptance/v4`);<br>· `G3-VISUAL.md`;<br>· migration `0073`. | r1–r2:<br>· record against a release build outside git;<br>· see that build's server report the layer as recorded;<br>· change one served file, and see the layer refuse with the p2 line. | `HIGH_RISK`: a safety gate's binding |
| **L4a** | §3 for a **single** approval:<br>· the approval owner and schema (migration `0074`);<br>· the product packet and the readiness;<br>· the approval route;<br>· the product run: retention, drills, in-server upload, freeze, CREATE grant, send;<br>· the segment and expiry rule;<br>· 「승인 취소」 and restart handling;<br>· the UI of p2–p6. | p1–p6, on a canary test product (ROADMAP canary plan):<br>· one real registration from the screen, ending 등록성공 on the store;<br>· then a second tab, an expired approval and a moved packet, each refusing;<br>· 「실제 등록 잠그기」 mid-run, stopping before the next mutation. | `HIGH_RISK`: LIVE |
| **L4b** | §3 for a **batch**:<br>· the selection and the batch packet, with exclusions;<br>· the canary precondition;<br>· sequencing, one job per product;<br>· the stop rules and 「중지」;<br>· `PAUSED` and 「계속」, and the 24-hour pause expiry;<br>· per-product results;<br>· the bulk-create exclusion;<br>· the UI of b1–b6. | b1–b6, on the owner's chosen products:<br>· a batch refused before the account's first verified registration;<br>· a small real batch, of an owner-chosen size, completing with per-product results.<br>On test fixtures, never on real products:<br>· an injected `UNKNOWN` stops the batch, leaving the rest untouched;<br>· a pause at the segment cap (a shortened clock in tests) and 「계속」 resume correctly;<br>· a product changed during a pause stops the batch at 「계속」. | `HIGH_RISK`: LIVE |

**Order:** L1 → L2 → L3 → L4a → L4b.
- L1 is independent of LIVE, and unblocks pricing at once.
- L2 and L3 make the two acceptances reachable without GitHub or git.
- L4a needs both: without them, §3.3 refuses every approval.
- L4b needs L4a, and an account's first verified registration.

Each slice follows the global merge lane.

## 10. Resolved decisions

| # | question | decision | source |
| --- | --- | --- | --- |
| D1 | Who sets the target margin, and is there a floor? | The seller, in Settings.<br>There is no minimum.<br>A loss is a warning, never a block. | owner `6200578768` |
| D2 | How is a real registration approved? | In the app:<br>· exact ASSET and CREATE grants, budget 1, short expiry;<br>· the server runs the upload, the drills and the retention proof;<br>· every §4.3 layer is checked per mutation;<br>· the brake stays separate. | owner `6200578768` |
| D3 | Where do the two acceptances live? | Visual: once per release build, by the developer, shipped with the build.<br>Residual risk: once per account, in the app, by the owner. | owner `6200578768` |
| O1 | Does an in-app approval need a GitHub record? | No. A note in rule §7.1–§7.2 says so.<br>An agent never gives or simulates the approval.<br>An agent's own LIVE run still needs the GitHub scope and the user's approval. | owner `6201001579` |
| O2 | May the target margin be below 0%? | Yes, with a warning. There is no floor. | owner `6201001579` |
| O3 | Do existing stores switch to v2 automatically? | No. A store keeps v1 (35% target, 10% minimum) until the seller saves the margin once.<br>No existing data is changed. | owner `6201001579` |
| O4 | Who gives the residual-risk acceptance? | The architect's half: at contract level, by `6200578768`.<br>The user's half: in the app, once per account. | owner `6201001579` |
| O5 | Do 600 products need 600 approvals? | No. One approval covers up to 500 products:<br>· exact per-product grants, budget 1;<br>· products run in order;<br>· the batch stops at an uncertain result;<br>· 「중지」 is always available;<br>· the batch pauses at the four-hour cap, and 「계속」 is one more press;<br>· an account's first registration is a single-product test. | owner `6201001579` |
| A1 | Where is the margin set? | Per store, in the target policy's pricing context, beside the fees it is computed against. | implementation agent (ADR-0022), 2026-10-16 |
| A2 | What is the margin's range? | `−100% < t < 100%`, with at most two decimals, and reachable with the store's rates. | implementation agent, 2026-10-16 (O2) |
| A3 | Which guards does v2 keep? | `LOSS` (net profit below 0, counting every policy cost) and `BELOW_TARGET_MARGIN`.<br>Both are `READY`-status warnings. | implementation agent, 2026-10-16 |
| A4 | What does the seller see before approving a batch? | The count, every name in order, the price range, the warnings by kind with their products, the total images to upload, and the exclusions with their reasons.<br>The per-product digests are rolled into one batch digest, which the client echoes. | implementation agent, 2026-10-16 (O5) |
| A5 | When are grants issued? | Per product, by its run, at the step that needs them; never ahead of the product.<br>ASSET: one grant per artifact, budget 1, bound to its one artifact.<br>CREATE: one grant, budget 1, after a freeze that reproduces the approved packet. | implementation agent, 2026-10-16 (O5) |
| A6 | What is the exact expiry rule? | A segment lasts four hours (`LIVE_WINDOW_MAX_S` = `MAX_GRANT_WINDOW_S`).<br>A product starts only while at least 30 minutes of its segment remain, and its grants expire 30 minutes after it starts.<br>Below 30 minutes left, the approval pauses.<br>At most two windows per product, and none spans a freeze. | implementation agent, 2026-10-16 (O5) |
| A7 | How many approvals can be open at once? | At most one open approval (`RUNNING` or `PAUSED`) per account.<br>Across accounts, products still run one at a time on the single worker. | implementation agent, 2026-10-16 (O5) |
| A8 | Does the brake get a screen action? | Yes. Release is in the app, with its own confirmation; engage is one press.<br>The semantics of ADR-0018 §4.1 are unchanged. | implementation agent, 2026-10-16 (D2 "brake separate") |
| A9 | What happens on a restart during a product run? | The run job is `DEAD`, never retried.<br>The approval is `INTERRUPTED`, and unspent grants are revoked.<br>A `PAUSED` approval survives a restart. | implementation agent, 2026-10-16 |
| A10 | Can products be edited during an approval? | No. Every product of an open approval is locked.<br>Drift is still detected at every step. | implementation agent, 2026-10-16 (O5) |
| A11 | Where do the restore drills restore? | Into a fresh root, under a configured directory outside the data directory.<br>The restored copy is removed when its product run ends; the sanitized proof is kept.<br>Two drills per product are the accepted cost to a batch's throughput. | implementation agent, 2026-10-16 |
| A12 | What identifies a release build? | Code digest + schema head + release identifier.<br>The commit is provenance only.<br>The shipped record is outside the digest's file set. | implementation agent, 2026-10-16 |
| A13 | How long can a paused batch wait? | 24 hours. It then ends `EXPIRED`, and its remaining products are unlocked. | implementation agent, 2026-10-16 |
| A14 | What does an `UPLOAD_UNKNOWN` or `NOT_APPLIED_PROVEN` upload do? | It stops that product before its freeze, and with it the whole approval.<br>There is no partial CREATE. | implementation agent, 2026-10-16 |
| A15 | How is a batch sequenced? | One durable job per product, with exactly one successor queued (the pattern of ADR-0014 §29), so other work interleaves. | implementation agent, 2026-10-16 |
| A16 | Which failures do not stop a batch? | Only a documented, definitive provider rejection of a CREATE: it proves nothing was registered, and the cause is that product's own.<br>Every other non-success stops the whole approval. | implementation agent, 2026-10-16 |

## Invariants

```text
LAP-01  one in-app approval covers 1 to 500 products of one store account in a fixed order; a selection over 500 is refused; the first real registration of an account is a single-product approval, and a batch needs one ICBM registration on that account created and confirmed by read-back
LAP-02  grants are issued per product by its run, never ahead of it and never shared: one ASSET grant of budget 1 per artifact needing transmission, bound to the exact ASSET unit and its one artifact, and at most one CREATE grant of budget 1, issued only after that product's freeze; each expires 30 minutes after its product started and never after its segment; no wildcard, unit-less, other-stage or two-product grant exists
LAP-03  an approval and each 「계속」 are server-validated protected actions over server-built packets whose batch digest the client echoes; a moved packet refuses; the client never builds a packet; the grants are the authority, UI text and confirmations never are; nothing the seller types is persisted
LAP-04  the CREATE grant is issued only when the product's frozen Snapshot derives from exactly the approved preparation revision, candidate fingerprint, artifact set and pricing snapshots and reproduces the approved packet
LAP-05  every layer of ADR-0018 §4.3 admits every upload and every CREATE at send time, in the unit that starts its attempt; an approval satisfies no layer by itself and the mode alone is never authority
LAP-06  an approval never releases the brake; releasing is a separate explicit in-app or CLI action with its own confirmation, audited with an authorization reference; engaging is always one action; every ADR-0018 §4.1–§4.2 rule of the brake is unchanged
LAP-07  at most one approval per account is open; a request_id is idempotent and belongs to one approval for ever; an ended approval is never reopened or reused
LAP-08  the approval, 「계속」, brake and acceptance routes accept only a loopback-host, same-origin JSON request carrying X-ICBM-Client and refuse an extension origin; no GET has a side effect
LAP-09  a segment lasts at most four hours; a product starts only while 30 minutes of its segment remain; below that the approval pauses with no grant active and no window open; continuing needs the seller's explicit 「계속」, which re-derives every account-level layer and every remaining product's digest and stops the approval on any difference; a paused approval ends by itself after 24 hours
LAP-10  each product opens at most two LIVE windows, each inside its own live grant: one for its uploads, closed before its freeze, and one for its CREATE; no window spans a freeze or outlasts its segment; a restart ends a running approval INTERRUPTED and never resumes it
LAP-11  products run one at a time in the approved order; the whole approval stops at the first UNKNOWN, UPLOAD_UNKNOWN, NOT_APPLIED_PROVEN upload, engaged brake, drift or mismatch, failed proof, stack refusal or expired grant, leaving later products untouched and unlocked; only a documented definitive provider rejection of a CREATE fails a single product and lets the batch continue; results are per product
LAP-12  an UNKNOWN CREATE is never resent and an UPLOAD_UNKNOWN never retried; a product with an unresolved UNKNOWN, conflict scope or fenced replay key is excluded from every approval
LAP-13  an agent never gives, clicks or simulates an in-app approval, a 「계속」, a brake release or a residual-risk acceptance; a slice acceptance that registers for real is clicked by the owner under the owner's go-ahead for those products
LAP-14  per product the server proves retention (once per segment while current), takes the ASSET restore drill before its first upload and the CREATE restore drill after its freeze, each into a fresh root outside the data directory; a proof that does not pass stops the approval before any transmission it gates
LAP-15  「중지」 ends an approval between products without interrupting a request in flight; 「승인 취소」 ends a single approval before its next step until the CREATE is handed to the transport; an engaged brake stops every mutation not yet started; an attempt already handed to the transport completes as itself and is recorded under ADR-0014 §9–§10
LAP-16  every product of an open approval is locked against edits and excluded from bulk-create, and drift is still detected at every step; every ending unlocks them and revokes every unspent grant
LAP-17  the residual-risk acceptance is recorded in the app once per account by its owner over the exact ADR-0018 §6.1 statement, append-only and withdrawable; the architect's half is given at contract level; a changed statement proves nothing; it authorizes nothing by itself
LAP-18  a visual acceptance is recorded by the developer once per release build, bound to its code digest, schema head and release identifier, and shipped with it; any change to the running code or schema makes it refuse; a build whose code identity cannot be computed has none
LAP-19  under pricing-rule/v2 the target margin is the store's audited Settings value, pinned in every snapshot; no minimum margin exists and a target below 0% is allowed with a warning; LOSS (net profit below 0) and BELOW_TARGET_MARGIN are warnings shown wherever the price is shown, including both approval dialogs, and never block
LAP-20  pricing-rule/v1 snapshots keep every v1 CHECK; a store keeps v1 until its seller saves the margin once; no snapshot or policy revision is rewritten; a margin change creates new snapshots; the canonical pricing rule is unchanged
LAP-21  a target margin no price can reach is refused at save; every margin is an exact fraction, never a float
LAP-22  every approval, segment, item, grant, brake and acceptance transition is audited in the same unit of work with actor, time and correlation identity
LAP-23  the icbm live command family keeps working unchanged beside the in-app path; no second grant, brake, drill, retention, upload or acceptance owner exists; ADR-0042 S2c's UPDATE batch rule is unchanged
LAP-24  every slice is accepted by a seller walkthrough on the real UI at desktop and mobile width, not by tests alone
```

## References

- **Issue #219 comments:**
  - `6200578768`: D1–D3;
  - `6201001579`: O1–O5;
  - `6109102895`: the seller-risk principle;
  - `6185598957`: the visual `<details>` amendment;
  - `6190978708`: ADR-0042 Q1–Q7.
- **ROADMAP:** "Pricing contract", §14 items 4–7, §14.1, §14.2.
- **ARCHITECTURE:** §13.
- **GLOSSARY:** §1.
- **ADRs:**
  - ADR-0002;
  - ADR-0005: the job restart rule;
  - ADR-0006;
  - ADR-0013: §7, §8;
  - ADR-0014: §2, §9–§11, §15, §24, §26, §28, §28.5, §28.7, §29;
  - ADR-0015: §2;
  - ADR-0018: §2, §3.1–§3.5, §4.1–§4.3, §5.1, §6.1, §7–§10, G3-01–G3-31;
  - ADR-0020: §3;
  - ADR-0022: §3;
  - ADR-0024;
  - ADR-0038: §4–§5;
  - ADR-0039: §3, SP-04;
  - ADR-0042: §11, §12, §15 A4.
- **Rules:**
  - `documents/rules/06-immutable-domain-rules.md`: §6.1;
  - `documents/rules/07-execution-safety.md`: §7.1–§7.3;
  - `documents/rules/14-operating-authority.md`: §14.2.
- **Acceptance records:**
  - `documents/acceptance/gates/G3-VISUAL.md`;
  - `documents/acceptance/milestones/M4.md`.
- **Pricing code:**
  - `app/stages/products/pricing.py`: `TARGET_NET_MARGIN`, `MINIMUM_NET_MARGIN`, `calculate`, `PricingDependencies.fingerprint`;
  - `app/stages/products/readiness.py`, `models.py`, `atomic_sku_economics_models.py`;
  - `app/stages/register/target_policy.py`.
- **Migrations:**
  - `0013`: pricing snapshots;
  - `0026`: live authority;
  - `0029`, `0030`, `0036`;
  - `0047`: AtomicSKU pricing snapshots;
  - `0057`: the DISPATCH grant rebuild;
  - `0066`: landed cost.
- **Interface and safety code:**
  - `app/interface/cli.py`: `icbm live`;
  - `app/interface/api/middleware.py`: `ClientHeaderGuard`;
  - `app/main.py`: `ALLOWED_HOSTS`, `TrustedHostMiddleware`;
  - `app/interface/api/routes/system.py`: the execution mode;
  - `app/interface/api/routes/register.py`: bulk-creates;
  - `ui/extension/lib/signing.js`;
  - `app/platform/system/execution_mode.py`;
  - `app/platform/core/code_identity.py`;
  - `app/capabilities/live_safety/`: `authority.py`, `stack.py`, `proofs.py`, `drill.py`, `retention.py`, `visual.py`, `residual_risk.py`, `upload_run.py`, `model.py`, `models.py`.
