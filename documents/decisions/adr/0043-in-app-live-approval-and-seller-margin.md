# ADR-0043 — The seller's target margin, the in-app LIVE approval per product, and release-time acceptances

Status: **PROPOSED** 2026-10-16. The owner's decisions are recorded (Issue #219 `6200578768`). It becomes **ACCEPTED** when it merges after the GPT + Claude cross-audit.

This ADR is the contract for three things:
- **D1:** the seller sets the target net margin in Settings, and ICBM enforces no minimum margin;
- **D2:** a seller registers one product for real from the screen, with one approval of that product, instead of a protected CLI command run by an operator;
- **D3:** the two acceptances a real registration still needs — the visual acceptance and the residual-risk acceptance — are recorded where they belong: the visual acceptance once per release build by the developer, shipped with the build; the residual-risk acceptance once per store account, in the app, by the owner.

It lands before its code. Each slice of §9 cites it as canon. It **amends** ADR-0018 §2, §3.2, §3.3, §4.1, §6.1, §9, §10 and invariants G3-18, G3-30 and G3-31; ADR-0013 §7 and §8; ADR-0014 §28.7; ROADMAP "Pricing contract" and §14.2; ARCHITECTURE §13; `documents/acceptance/gates/G3-VISUAL.md`; and adds one note to `documents/rules/07-execution-safety.md` §7.1–§7.2. §7 states each amendment and the slice that makes it effective. It supersedes none of them.

Decision owners:
- **Product direction:** the owner, in Issue #219 `6200578768` (2026-10-16, "첫 실등록까지 남은 판단 3가지"):
  - **D1** — the target margin is the seller's Settings value, with no minimum floor (원문: "마진 최소 하한 없애 사용자결정에 맡겨"). A loss — a margin below 0% — is a warning the seller sees, never a block.
  - **D2** — real registration is approved on the screen. ADR-0018 §4.1 (CLI-only grants) is amended on the model of ADR-0042 §11 A4. One 「이 상품 실제 등록 승인」 per product issues exactly that product's ASSET and CREATE grants (budget 1, short expiry) and opens the bounded LIVE window inside them. The server runs the image upload, the restore drill and the retention proof per approval. Every §4.3 layer is still checked per mutation. The brake stays a separate explicit action.
  - **D3** — the visual acceptance is recorded by the developer once per release build and shipped with it; the residual-risk acceptance is confirmed by the owner once per account, in the app.
  - `6109102895`: the seller-risk principle. Where the seller bears the risk, ICBM shows the facts and the user decides. ICBM's own safety invariants stay hard.
  - The principle "실제 사용자 기준" (2026-10-15): each slice is accepted by a seller walkthrough on the real UI (§0, §9).
- **Design decisions A1–A14** (§10): made by the implementation agent under ADR-0022, recorded here as decisions. The points of §11 are defaults this ADR chose where the owner's words leave room; each is open to the owner at audit.
- **Already decided by canon, and unchanged here:**
  - ADR-0018: the grant, the brake, the send-time stack, the restore drill, retention, the ASSET upload-attempt owner and its replay fence.
  - ADR-0014 §9–§11, §15, §26 and §28: attempts, `UNKNOWN`, read-back, the sanitizer, the execution-scope brake and positive-only reconcile.
  - ADR-0013 §7: the canonical pricing rule (`documents/rules/06-immutable-domain-rules.md` §6.1).
  - ADR-0039: the supplier minimum-sale-price policy is the user's choice.
  - ADR-0042 §11 A4: the UI-issued batch approval of the UPDATE stage — the model D2 follows.
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

**Context.** A seller cannot finish one real registration from the screen today. After #368, #369, #370, S1b and S3b, three things still stop it:
- **The margin is a constant.** `app/stages/products/pricing.py` fixes `TARGET_NET_MARGIN = Fraction(35, 100)` and `MINIMUM_NET_MARGIN = Fraction(10, 100)` under `pricing-rule/v1`. Both snapshot tables pin them by CHECK: `pricing_snapshots` (migration `0013`, `app/stages/products/models.py` `rule_known`, `policy_margins`, `guard_exact`, `guard_reasons_exact`) and `atomic_sku_pricing_snapshots` (migration `0047` lines 188–251, `atomic_sku_economics_models.py`). A guard of `LOSS` or `BELOW_MIN_MARGIN` makes pricing readiness `BLOCKED` (`app/stages/products/readiness.py`, ADR-0013 §8). The seller cannot choose either value.
- **Every grant is issued by an operator command.** `issue-asset-grant`, `issue-create-grant`, `upload-assets`, `restore-drill-asset`, `restore-drill-create`, `prove-retention` and `release-brake` exist only in `app/interface/cli.py` (ADR-0018 §4.1 amendment notes: "never a new screen or route"). `upload-assets` runs only with the server stopped. Grants and a brake release accept only a GitHub comment id as their authorization reference (`live_grants.authorization_ref_is_comment_id`, `protected_write_brakes.release_is_authorized`).
- **Both acceptances are developer-side records.** The visual acceptance is current only at the exact git commit the process runs at (`app/capabilities/live_safety/visual.py` lines 333–340; `app/container.py` lines 1128–1133; `app/platform/core/code_identity.py`), so an installed build with no git checkout can never have one. The residual-risk proof needs two GitHub comment ids, the user's and the architect's (`residual_risk.py` lines 143–160), recorded only by `icbm live record-residual-risk-acceptance`.

What it authorizes:
- this contract;
- the slices of §9, each its own PR;
- the amendments of §7, each effective when the slice it names lands.

What it does not authorize:
- **No mutation outside ADR-0018.** Every upload and every CREATE is still admitted, at send time, by every layer of ADR-0018 §4.3, in the one unit that starts its attempt (§6).
- **No widening.** No grant is wider, longer or larger than ADR-0018 already allows; the approval issues grants that are *narrower* than the CLI's (§3.1). No layer is removed.
- **No implied brake release.** An approval never releases the brake (§3.7).
- **No blind resend.** An `UNKNOWN` CREATE and an `UPLOAD_UNKNOWN` stay governed by ADR-0014 §10, §17.1 and §28 and ADR-0018 §3.3–§3.4 (§4).
- **No batch LIVE registration.** One approval covers one product (one provider-listing unit). A batch approval for REGISTER is not decided here; ADR-0042 §11's batch approval governs only the UPDATE stage.
- **No agent approval.** An agent never gives, clicks or simulates an in-app approval, a brake release or a residual-risk acceptance (LAP-13).
- **No change to the canonical pricing rule.** `documents/rules/06-immutable-domain-rules.md` §6.1 is unchanged; only the target margin's source and the guards' effect change (§2).
- **No Coupang change.** Track C's bounded Coupang mutation commands (`icbm coupang …`) are outside this ADR.

Recorded by: Claude Code (Track B). The number 0043 was confirmed free on `main` and in every remote branch immediately before writing. Migrations start at `0071`: ADR-0042 reserved `0067`–`0070`, and main's head is `0066_landed_cost`.

Date: 2026-10-16

---

## 0. 사용자 관점 / Seller walkthrough

This is what a seller does and sees. The screens are the existing ones: 설정, 통합DB and 등록관리 with its editor. New elements are marked **new**. Korean strings are the intended copy; the slices may refine the wording, not the meaning.

These walkthroughs are the acceptance script of every slice (§9). Each slice runs the steps it delivers on the real UI, at desktop and mobile width, before it is accepted.

### 0.1 Once per store

| # | screen | the seller does | the seller sees |
| --- | --- | --- | --- |
| s1 | 설정 → 스마트스토어 → 가격 정책 | **new:** types 「목표 순마진」, for example 25 (%), and saves | 「저장됨 · 상품 가격을 다시 계산합니다」. Under the field: 「최소 마진 제한은 없습니다. 0% 미만이면 손실 판매입니다.」 A value below 0 shows 「손실 판매 목표입니다」 before saving and asks 「그래도 저장」. A value that no price can reach with this store's fees is refused: 「수수료를 빼면 이 마진을 만들 수 없습니다 (최대 …%)」 |
| s2 | 통합DB → product | nothing | prices recomputed under the new margin: 「판매가 12,900원 · 예상 순마진 25.0%」. A product priced by the supplier's minimum sale price below the target shows 「목표 마진보다 낮음 (18.2%)」; a loss shows 「손실 판매: 예상 순이익 −1,200원」. Neither blocks registration |
| s3 | 설정 → 스마트스토어 → **new** 「실제 등록 위험 안내」 | reads it and presses 「이해했고 수락합니다」 | the statement of §5.2 in Korean, the store name, then 「수락함 · 10/16 14:02」 and 「수락 철회」. Asked once per store, never again unless the statement changes |
| s4 | 등록관리 → 「실제 등록 잠금」 card (also in 설정 → 스마트스토어) | **new:** presses 「실제 등록 잠금 해제」, then 「잠금 해제」 in the dialog | the dialog: 「잠금을 풀어도 상품마다 승인하기 전에는 아무것도 등록되지 않습니다. 언제든 다시 잠글 수 있습니다.」 Then 「잠금 해제됨」 with **new** 「실제 등록 잠그기」, always visible and one press, no dialog |

### 0.2 Per product, desktop

| # | screen | the seller does | the seller sees |
| --- | --- | --- | --- |
| p1 | 등록관리 editor | prepares the listing and presses 「등록 준비 확인」 (as today) | 「등록 준비 완료」 (the candidate preflight is `READY`), with the price row and its margin warning, if any |
| p2 | 등록관리 editor | nothing | **new** 「이 상품 실제 등록 승인」, enabled only when every precondition of §3.3 holds. Otherwise it is disabled with the missing items as plain lines and links: 「실제 등록 잠금 해제 필요」, 「위험 안내 수락 필요」, 「스토어 연결 확인 필요」, 「전송 일시정지됨 · 전송 재개」, 「이전 등록 결과 확인 중」, 「다른 상품 등록 진행 중」, 「이 버전은 화면 검수가 기록되지 않아 실제 등록을 할 수 없습니다. 업데이트를 받아 주세요.」 |
| p3 | the approval dialog | presses the button | **new:** 스토어, 상품명, 판매가, 예상 순마진 (with its warning), 카테고리, 「이미지 n장 업로드」, the seller code, 「승인은 30분 동안 유효합니다」 and 「네이버 스마트스토어에 실제로 등록됩니다. 결과가 불확실하면 다시 보내지 않고 등록 여부를 확인합니다.」 Buttons 「취소」 (focused) and 「승인하고 등록」. Enter and Esc cancel |
| p4 | the progress panel | presses 「승인하고 등록」 | **new** steps, each with its state: 「보존 확인」 → 「백업 확인」 → 「이미지 올리는 중 2/5」 → 「등록 내용 확정」 → 「백업 확인」 → 「상품 등록 전송」 → 「등록 결과 확인」. 「승인 취소」 until the CREATE is handed to the transport; 「실제 등록 잠그기」 always. The editor's fields are read-only: 「승인 진행 중에는 수정할 수 없습니다」 |
| p5 | the progress panel and the registration status card (ADR-0014 §28.5) | nothing | one of: 「등록성공」 with the product number and 「스토어에서 보기」; 「재확인필요」 — 「결과를 확인하지 못했습니다. 다시 보내지 않고 등록 여부를 확인합니다.」; 「등록실패」 — 「네이버가 거절했습니다 (등록되지 않음)」; 「중단됨」 with its reason and what was sent: 「이미지 2장만 올라갔고 상품은 보내지 않았습니다」 |
| p6 | the same, in a second tab or after a delay | presses 「승인하고 등록」 again | 「이미 진행 중인 승인이 있습니다」 with the running approval's progress; or 「승인 내용이 바뀌었습니다. 다시 확인해 주세요」 with the new preview; or 「승인 시간이 지났습니다. 다시 승인해 주세요」 |

### 0.3 Per product, mobile width

The same steps p1–p6. Differences only of layout:
- 「이 상품 실제 등록 승인」 is a full-width button fixed at the bottom of the editor.
- The dialog is a full-screen sheet. Its preview rows stack; the margin warning is never truncated.
- The missing preconditions of p2 collapse into 「실제 등록 전에 필요한 것 3개」, opened by a tap. It is a `<details>` whose summary is visible, so the visual acceptance judges every line inside it (ADR-0018 §9 amendment, 2026-10-15).

### 0.4 Once per release build (the developer, not the seller)

| # | who | does | result |
| --- | --- | --- | --- |
| r1 | developer | builds the release, runs the populated visual harness against it, reviews the report, and records it into the build (§5.1) | the build ships `release/visual-acceptance.json` beside its manifest |
| r2 | seller | installs or updates | nothing visible. Without a valid record for the running build, p2 shows the 「이 버전은 화면 검수가 기록되지 않아…」 line and nothing can be approved |

## 1. Terms

| term | meaning |
| --- | --- |
| **product** (in "per product") | one provider-listing unit of one store account (ADR-0014 §2): one preparation, one future Snapshot and one CREATE Intent |
| **approval** | the durable, audited, server-owned record that the seller approved one product for real registration (§3). It is the protected action that carries the user's approval (rule §7.2); the grants it issues are the authority |
| **approval packet** | the deterministic, server-built preview of exactly what the approval covers, and its SHA-256 (§3.2) |
| **approval run** | the server's sequence that proves, uploads, freezes, issues the CREATE grant and sends, inside one approval (§3.5) |
| **release build** | one built distribution of the application, identified by its code digest, its schema head and a release identifier (§5.1) |
| **target net margin** | the seller's Settings value `t`: the price is set so that `expected_net_margin ≥ t` (§2) |

## 2. D1 — the seller's target margin and `pricing-rule/v2`

### 2.1 The setting

- **Where.** The target margin is a field of the M4 pricing context, `target_net_margin`, held in the store's registration target policy (ADR-0015 §2, `app/stages/register/target_policy.py`). One value per `marketplace_key × marketplace_account_id`, like the fees it is computed against (A1).
- **Audited.** A save appends one target-policy revision, as every Settings save does: server-created identity, fingerprint, audit record, append-only. The content version becomes `registration-target-policy/v2`; the pricing context becomes `pricing-context/v2` and names `target_net_margin` in its canonical form, so the context fingerprint covers it.
- **Value.** Exact decimal text of a percentage with at most two decimals, parsed to a `Fraction`; never a `float`. The accepted range is `−100% < t < 100%` (A4). A value with `t ≥ 1 − fee_rate − other_cost_rate` leaves no reachable price and is refused at save (`TARGET_POLICY_TARGET_MARGIN_UNREACHABLE`), naming the largest reachable value. **No minimum exists**: 0% and a negative value are saved when the seller confirms the warning of s1.
- **The first save.** A revision appended before slice L1 keeps `pricing-context/v1` and is priced under `pricing-rule/v1` (35% target, 10% minimum) exactly as today. Nothing is backfilled or rewritten (A3). The Settings field is prefilled with 35% and the price row shows 「목표 마진을 설정에서 확인해 주세요」 until the seller saves once.
- **Not the FX margin.** ADR-0038's `fx_margin_percent` (「환전 마진」) is a landed-cost input and is unchanged. The two are labelled apart.

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
- `target_margin_price` is the least whole-KRW price whose exact net margin is at least the context's `t` (the existing search of `target_margin_price`, with `t` in place of the constant). Rounding stays `CEIL_KRW_1`. No `max(target, minimum)` is ever restored.
- **No minimum margin exists.** `MINIMUM_NET_MARGIN` does not apply to a v2 snapshot, and a v2 snapshot records none.
- **The guards are warnings** (A2). Each is shown wherever the price is shown — 통합DB, the editor's price row, the approval packet (§3.2) — and none blocks readiness:

| guard (v2) | holds when | readiness reason |
| --- | --- | --- |
| `LOSS` → `PRICE_LOSS` | `expected_net_profit_krw < 0`: the margin is below 0% with every cost the policy defines (fee and other policy costs included) | `Reason(PRICE_LOSS, READY)` — the warning form ADR-0039 SP-04 already uses |
| `BELOW_TARGET_MARGIN` → `PRICE_BELOW_TARGET_MARGIN` | `expected_net_margin < t`. Only a `MINIMUM_SALE_PRICE` basis can produce it | `Reason(PRICE_BELOW_TARGET_MARGIN, READY)` |

  The primary `price_guard` is `LOSS` when a loss applies, else `BELOW_TARGET_MARGIN` when that applies, else `OK`. Every applicable reason is listed, as in v1.
- `TARGET_MARGIN_UNREACHABLE` stays `REVIEW_REQUIRED` for a context whose rates leave no price (§2.1 refuses such a save, so it arises only from a fee change).
- **Fingerprints.** The dependency fingerprint already names `pricing_rule_version` and the context fingerprint. A v2 context therefore reprices every Item under it: the old snapshots become historical (`STALE`), new ones are created, and nothing is updated in place (ADR-0013 §7).

### 2.3 Storage — migration `0071`

`0071_pricing_rule_v2` changes both snapshot tables, `pricing_snapshots` and `atomic_sku_pricing_snapshots`, and nothing else:
- **By rule version.** Every CHECK that pins the v1 policy keeps pinning it for `pricing_rule_version = 'pricing-rule/v1'` rows, unchanged in meaning: `target_margin = 35/100`, `minimum_margin = 10/100`, the v1 `LOSS` definition (`purchase_cost + supplier_shipping + platform_fee ≥ final_sale_price`), `BELOW_MIN_MARGIN` and both guard-reason CHECKs.
- **v2 rows.** `rule_known` admits `pricing-rule/v2`. A v2 row records the context's exact target margin (`target_margin_denominator ≥ 1`, `−denominator < numerator < denominator`), **NULL** minimum-margin columns, `price_guard IN ('OK', 'LOSS', 'BELOW_TARGET_MARGIN')`, and guard and guard-reason CHECKs that encode §2.2 exactly. A v1 row may not have NULL minimum-margin columns.
- **How.** SQLite cannot alter a CHECK in place, so both tables are rebuilt: every row copied unchanged, the append-only and current-state triggers, indexes and foreign keys recreated, `app.platform.db.schema_contract` updated. No row is rewritten, so the append-only triggers never fire on existing rows.
- **Downgrade fails closed** while any v2 snapshot exists.

### 2.4 What does not change

- The canonical rule, the supplier minimum-sale-price policy (ADR-0039), the landed cost (ADR-0038), the exact arithmetic, the rounding and the dependency model.
- Every v1 snapshot, and every RegistrationItemSnapshot that pins one.
- `documents/acceptance/milestones/M4.md`: historical v1 evidence.
- An estimated margin is never the settled margin (ADR-0013 §7, OPERATE's).

## 3. D2 — the in-app LIVE approval per product

### 3.1 What one approval covers

One approval covers **one product of one store account**, and issues exactly:
- **ASSET grants: one per artifact that needs transmission, each with budget 1** (A5). Each binds the exact ASSET unit of ADR-0018 §3.2 — the preparation revision, the candidate fingerprint, the exact selected artifact set and the asset profile — and, in addition, **its one artifact** (kind, SHA-256, derivation identity). An upload of any other artifact never matches it. An artifact whose exact bytes are already `APPLIED_PROVEN` for the same account, endpoint and asset profile is locally rebound (ADR-0018 §3.4) and gets no grant. A product that needs no upload gets no ASSET grant.
- **One CREATE grant with budget 1**, binding the exact Snapshot, Intent, idempotency key and attempt number (ADR-0018 §3.2). It is issued by the server **after the freeze** of §3.5, never before, because no Intent exists before it (ADR-0018 §3.1).
- **Every grant's window is the approval's**: `not_before` is the approval time and `expires_at` its expiry, **30 minutes** after approval (A6; ADR-0042 A4). The maximum window of ADR-0018 (four hours) still bounds it.
- **Its authorization reference is the approval** (§8: a typed in-app reference), not a GitHub comment.

These grants are narrower than any the CLI can issue: one artifact per ASSET grant, budget 1 everywhere, a 30-minute window. No wildcard, no unit-less grant and no grant of another stage exists. A CLI-issued grant is unchanged (§3.8).

### 3.2 The approval packet

The server builds the packet deterministically from the canonical owners, as ADR-0018 §5.1 builds the eligibility packet. It holds only sanitized, seller-visible material and the identities that bind it:
- the marketplace, the canonical account and the store's display name;
- the preparation identity and revision, the candidate fingerprint and the asset profile;
- the selected artifact set (kind, SHA-256, derivation identity) and which of them need transmission;
- the publication-facing listing content: name, category path, seller code, options summary, delivery summary;
- per Item, the pinned `PricingSnapshot` identity, the final sale price, the price basis, the expected net profit and margin, and every guard warning (§2.2);
- the residual-risk statement digest of §5.2, and the packet version `live-approval-packet/v1`.

Its canonical JSON is digested with SHA-256. **The client never builds a packet**: it displays the server's packet and echoes back its digest.

When the preparation is already frozen (the seller pressed 「등록 내용 확정」 earlier), the packet is built from the frozen Snapshot and its `PREPARED` Intent instead, and the run of §3.5 skips the ASSET steps 2–5.

### 3.3 Preconditions — a derived, read-only readiness

`GET` of the approval readiness of one preparation answers, without side effects, every precondition and every missing one by code. The approval refuses unless each holds at the moment of approval:
- the candidate preflight is `READY` (「등록 준비 완료」), or the unit is frozen with a `PREPARED` Intent;
- the store account is bound and CONNECT holds a proven current committed session (ROADMAP §14 item 4);
- the protected-write brake is `RELEASED` (§3.7);
- the residual-risk acceptance of §5.2 is recorded for this account;
- the visual acceptance of §5.1 is recorded for the running build;
- the image upload and CREATE endpoints and the positive-only reconcile path are adopted;
- the ADR-0014 §26 execution scope of this CREATE endpoint group is `ACTIVE` (「전송 일시정지됨 · 전송 재개」 otherwise; the existing `scopes/resume` action);
- no unresolved `UNKNOWN` CREATE or conflict scope covers the unit, and no started or `UPLOAD_UNKNOWN` attempt fences any selected artifact's replay key (ADR-0018 §3.4);
- no other approval is `OPEN` (A7), and no LIVE window opened by another path is open.

This readiness is **never permission**. The approval issues grants; the send-time stack decides every mutation (§6).

### 3.4 Approving

`POST /api/v1/register/preparations/{preparation_id}/live-approvals` with `{request_id, packet_digest}`:
1. **The request is a browser request of this app** (§4): loopback host, `X-ICBM-Client`, `Content-Type: application/json`, and an `Origin`, when present, equal to the app's own origin. An extension origin is refused.
2. **Idempotent by `request_id`.** The client creates one `request_id` (a UUID) when the dialog opens. The same `request_id` again returns the same approval and issues nothing more; a `request_id` is never reused for another preparation or packet.
3. **Single flight.** While any approval is `OPEN`, a new one is refused: `LIVE_APPROVAL_ALREADY_OPEN`, with the open approval's identity so the UI can show its progress.
4. **The packet is rebuilt** immediately before the write. A digest other than the submitted one refuses: `LIVE_APPROVAL_PACKET_MOVED`, with nothing recorded but the audit event.
5. **Every precondition of §3.3 is re-derived.** Any failure refuses, and names every failing precondition.
6. **One unit of work** records the approval (`OPEN`, the approving user's identity — `config.operator_actor` —, the packet digest, `expires_at`, the correlation identity), issues the ASSET grants of §3.1 with the approval as their authorization reference, locks the preparation against edits (A10), writes the audit events and enqueues the approval run.

The confirmation the seller sees is an input to this protected action, never the authority (ADR-0018 §3.3). No confirmation prose exists to persist; nothing the seller types is stored.

### 3.5 The approval run

The run is a durable job of the single worker (ADR-0002, ADR-0005). It is declared **not idempotent** with **one attempt**: a restart while it is `RUNNING` makes it `DEAD` (`INTERRUPTED_OUTCOME_UNKNOWN`), and the approval ends `INTERRUPTED` (A9). It never resumes. It decides nothing itself: each step calls the existing owner, and each owner's refusal ends the run.

Before every step it re-reads the approval, the brake and the clock. An approval that is no longer `OPEN` or has expired ends the run before the next step.

| # | step | owner | ends the approval when |
| --- | --- | --- | --- |
| 1 | prove evidence retention as it is now | `RetentionProofService.prove` (ADR-0018 §8) | the proof is not `PASSED` |
| 2 | the ASSET restore drill of this approval's batch, into a fresh root (A11) | `RestoreDrillService.drill_asset` (ADR-0018 §7) | the drill is not `PASSED`. One drill covers the batch: its target digest names the preparation revision, candidate fingerprint, artifact set, profile, endpoint, account and replay keys, never a grant |
| 3 | open the first bounded LIVE window, inside the approval's ASSET grants | `ExecutionModeService` (ADR-0018 §2 amendment note) | it refuses. The window never outlasts the approval; while an approval is `OPEN`, the execution-mode route opens no other window, and a `DRY_RUN` request closes this one |
| 4 | upload each artifact that needs it, once; then close the window | `AssetUploadService.upload`, as `upload_run.py` composes it today, now in the server process | a safety-stack refusal, or any `UPLOAD_UNKNOWN`: the remaining independent artifacts are not uploaded and nothing is frozen (A14). A `NOT_APPLIED_PROVEN` artifact is not retried inside the approval: each ASSET grant has budget 1. Once every ASSET grant is spent the window lapses by itself (no live grant remains); the run closes it in any case |
| 5 | re-evaluate the final preflight and freeze | `RegistrationPreparationService.freeze_current` (ADR-0018 §3.1 amendment notes) | the freeze refuses, or the frozen Snapshot does not derive from **exactly** the approved preparation revision, candidate fingerprint, artifact set and pricing snapshots, or a packet rebuilt from the Snapshot has another digest than the approved one (`LIVE_APPROVAL_FREEZE_MOVED`) |
| 6 | issue the CREATE grant: budget 1, the approval's window and reference | `LiveAuthorityService` | it refuses. **At most one CREATE grant per approval** (a unique key in the approval owner) |
| 7 | the CREATE restore drill, taken after the freeze | `RestoreDrillService.drill_create` | the drill is not `PASSED` |
| 8 | prove retention again if step 1's proof is no longer current | `RetentionProofService.prove` | not `PASSED` |
| 9 | open the second bounded LIVE window, inside the CREATE grant; send the CREATE, one attempt | `ExecutionModeService`; the REGISTER execution owner's existing `register.create` path; the send-time stack admits it (§6) | always ends: by `APPLIED` → read-back verification as today, `NOT_APPLIED_PROVEN`, or `UNKNOWN` |
| 10 | close the window; revoke every unspent grant; end the approval | `ExecutionModeService`, `LiveAuthorityService` | — |

Each window opens only while a grant of this approval exists (the execution-mode owner's scope rule, unchanged), so the CREATE gets its own window after its grant exists; no window spans the freeze. A window is closed, and unspent grants are revoked, **whatever happens**: on success, on any refusal, on expiry, on cancellation and on process exit (a restart never restores a window, ADR-0018 §2).

For a frozen unit (§3.2), the approval issues no ASSET grant, and the run is steps 1 and 6–10.

### 3.6 How an approval ends

`OPEN` ends exactly once, in one of:

| state | meaning | the seller sees |
| --- | --- | --- |
| `COMPLETED` | step 9 ran; the outcome is the Intent's, shown by the registration read state (ADR-0014 §28.5) | 등록성공 / 재확인필요 / 등록실패 |
| `STOPPED` | an owner refused at a step; the code and step are kept | 「중단됨」 with the reason and what was sent |
| `EXPIRED` | the 30 minutes passed before step 9 started | 「승인 시간이 지났습니다」 |
| `CANCELLED` | the seller pressed 「승인 취소」 before step 9 handed the CREATE to the transport | 「승인을 취소했습니다」 and what was already uploaded |
| `INTERRUPTED` | the process stopped while the run was `RUNNING` | 「앱이 다시 시작되어 승인이 중단되었습니다」. A started attempt is judged by its own owner (ADR-0018 §3.4 crash rule, ADR-0014 §9–§10) |

Every terminal state revokes unspent grants and unlocks the preparation. **No terminal state is reopened, and no approval is ever reused.** Another try is another approval over a freshly built packet.

An attempt already handed to the transport when the approval is cancelled, expires or the brake engages completes as the one attempt it is, and its outcome is recorded under ADR-0014 §9–§10 (ADR-0018 §4.2).

### 3.7 The brake on the screen

The protected-write brake keeps every rule of ADR-0018 §4.1 and §4.2: one global brake, durable, fail closed, surviving restart; engaging stops every mutation not yet started; releasing resurrects and widens no grant.

What changes is only where it is operated (A8):
- **「실제 등록 잠금 해제」** is a separate, explicit in-app action with its own confirmation dialog (s4). It is the "new explicit authorization" of §4.1: the server records the release with the actor, a typed in-app authorization reference (§8) and the audit event. It never happens as a side effect of anything else — never of an approval, a Settings save, a connect or a restart.
- **「실제 등록 잠그기」** is one press, always visible on the register page and on the approval progress panel, with no dialog. It ends the running approval's remaining steps before their next mutation.
- The approval **never releases the brake**. A released brake approves nothing.
- `icbm live release-brake` and `engage-brake` stay (§3.8).

### 3.8 The CLI stays

Every `icbm live` command keeps working exactly as ADR-0018 §4.1 describes, for the operator's recovery and for development, with its GitHub authorization reference. The in-app path is a second entry to the same owners, never a second owner: no duplicate grant, brake, drill, retention or upload model exists.

### 3.9 Relation to ADR-0042 S2c

ADR-0042 §11 A4 adds a UI-issued batch approval of the UPDATE stage. Both need the same widening of `live_grants` and `protected_write_brakes` to an in-app authorization reference (§8). Whichever slice lands first makes it; the other reuses it. The approval records stay in their own stage owners: REGISTER approvals here, UPDATE batch approvals in StoreUpdate (ADR-0042 §12).

## 4. Threat model

| threat | what stops it |
| --- | --- |
| **a stray click** | Two deliberate actions: the button, then 「승인하고 등록」 in a dialog whose focused button is 「취소」 and where Enter and Esc cancel. The button is disabled until every precondition holds and the packet has loaded. The brake is a third, separate action, never implied (§3.7) |
| **a double click or a retried request** | The `request_id` makes the approval idempotent: a repeat returns the same approval and issues nothing (§3.4 step 2). Grants have budget 1, so a repeated send finds no budget |
| **a replayed request, later** | A `request_id` belongs to one approval for ever. A replay after it ended returns that ended approval and issues nothing. A new approval needs a new dialog, a freshly built packet and every precondition again |
| **a request from another origin (CSRF)** | `TrustedHostMiddleware` admits only `127.0.0.1` and `localhost` (`app/main.py`), which also defeats DNS rebinding. `ClientHeaderGuard` refuses a state-changing `/api/` request without `X-ICBM-Client` (`app/interface/api/middleware.py`); a browser sends a custom header cross-origin only after a CORS preflight, and no CORS is configured. The approval and brake routes also require `Content-Type: application/json` and refuse an `Origin` other than the app's own. `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop clickjacking. No `GET` has a side effect |
| **the capture extension** | The extension also sends `X-ICBM-Client` (`ui/extension/lib/signing.js`). The approval, brake and acceptance routes refuse an extension origin and any request carrying the extension's signature headers |
| **another local process** | Out of scope by trust boundary: a process that can call the loopback API with forged headers can also run `icbm live` with the data directory. This ADR adds no new capability to such a process that the CLI did not already give it |
| **a second tab** | One `OPEN` approval at a time (A7): the second tab gets `LIVE_APPROVAL_ALREADY_OPEN` and shows the running one's progress. The preparation is locked while an approval is `OPEN` (A10) |
| **an expired approval** | Every grant expires with the approval; the run re-reads the clock before every step; neither LIVE window outlasts it (§3.5). An expired approval issues, opens and sends nothing more |
| **a changed product after approval** | The packet digest binds the preparation revision, candidate fingerprint, artifact set and pricing snapshots; the preparation is locked; each ASSET grant stops matching on drift (ADR-0018 §3.2); the freeze must reproduce the approved lineage and packet, or no CREATE grant is issued (§3.5 step 5); the CREATE grant binds the exact Intent; the send-time truth fence still runs at admission |
| **a changed price or margin setting after approval** | The pinned pricing snapshots are in the packet. A Settings save that reprices makes them historical, so the freeze no longer reproduces the packet: the approval stops with `LIVE_APPROVAL_FREEZE_MOVED` |
| **an `UNKNOWN` CREATE** | Never resent (ADR-0014 §10, §28; G3-07). The CREATE grant had budget 1 and is spent. The unit's conflict scope stays closed, so §3.3 refuses a new approval for it until positive-only reconcile or proven non-application ends it. The seller sees 재확인필요 |
| **an `UPLOAD_UNKNOWN`** | Never retried (G3-22); the run stops before the freeze (A14); its replay key stays fenced across every later approval (G3-29) |
| **a crash or restart mid-run** | The window is gone (process memory); the run job is `DEAD`, never retried; the approval is `INTERRUPTED`; unspent grants are revoked at startup; a started attempt is judged by its owner's crash rule (ADR-0018 §3.4, G3-25; ADR-0014 §9) |
| **the seller changes their mind** | 「승인 취소」 until the CREATE is handed to the transport; 「실제 등록 잠그기」 at any time |
| **an agent acting for the seller** | LAP-13: an agent never gives, clicks or simulates an approval, a brake release or a residual-risk acceptance. A slice acceptance that reaches a LIVE step is run by the owner's own click, under the owner's go-ahead for that product (§9) |

## 5. D3 — the two acceptances, where they belong

### 5.1 The visual acceptance, once per release build

ADR-0018 §9 is unchanged in **what** it accepts: the populated first-vertical scenario, at the accepted viewport set, with no server-owned blocker hidden. What changes is **what it is bound to** and **where it lives**:
- **A release build's identity** is the triple: the running **code digest** (`app.platform.core.code_identity`, unchanged: every file of `app`, `integrations`, the served UI and the dependency pins), the **schema head** its migrations build, and a **release identifier** from its manifest. The git commit becomes provenance in the manifest when one exists, never a requirement (A12).
- **The release manifest** — `release/release-manifest.json` — names the release identifier, the code-identity version, the code digest, the schema head and, if any, the commit. **The acceptance record** — `release/visual-acceptance.json` — is the reviewed harness report's sealed summary: its report digest, harness version, scenario, viewport set, target and check counts, sanitized evidence, the reviewer and the review reference, and the release identity it accepted. The `release/` directory is outside the code digest's file set, so recording the acceptance never changes the digest it binds.
- **Recording** is the developer's, once per release build, with the existing harness and `verify_report` contract, through the existing command family (`icbm live record-visual-acceptance --release`). A passing run alone still records nothing; the reviewer and review reference are still required.
- **Reading.** At startup the server verifies the shipped record against the running build: the release identity equals the manifest's, the code digest equals the one it computes now, the schema head equals the database's head, and the record's seal verifies. Only then does `VISUAL_ACCEPTANCE_RECORDED` hold, and the send-time layer reads it for every mutation as before. Any difference — one changed file included — and the layer refuses.
- **A build whose code identity cannot be computed records nothing.** A packaging format that does not ship the files the code digest covers is not a release build under this ADR; it is fail closed, never trusted.
- **The integrity binding is not a lock against the machine's owner.** Someone who can edit the shipped record can also edit the code it binds. The seal proves the record belongs to exactly this code, which is what ADR-0018 §9 needs.
- **The git-checkout path stays** for development: a record bound to the exact commit and code digest, as today (`G3-VISUAL.md`).
- **The approval surfaces join the harness**: the 「이 상품 실제 등록 승인」 readiness lines, the dialog with its margin warning, the progress panel and the brake card, at both viewports. The harness version becomes `g3-visual-acceptance/v4`.

### 5.2 The residual-risk acceptance, once per account, in the app

The risk is ADR-0018 §6.1's and ADR-0014 §28.7's, word for word (`RESIDUAL_RISK_STATEMENT`, its SHA-256 unchanged):

> a product may be live in SmartStore after an ambiguous CREATE while ICBM has not yet recovered the provider identity, leaving that listing temporarily outside normal confirmed price/stock monitoring.

The seller reads it in Korean (s3): 「네이버 응답이 불확실하면, 상품이 실제로는 등록되었는데 ICBM이 아직 상품번호를 확인하지 못해 가격·재고 감시 밖에 잠시 놓일 수 있습니다. ICBM은 그 상품을 다시 보내지 않고, 확인될 때까지 ‘재확인필요’로 표시합니다.」

- **Recorded in the app by the owner of the account**, once per `marketplace_key × marketplace_account_id`, through `POST /api/v1/live/residual-risk-acceptances` with the server-built statement packet's digest (the canonical statement's digest, the Korean text's digest, the account). The record keeps the actor, the time, both digests and the contract version `adr-0043-residual-risk-in-app/v1`.
- **The architect half is given at contract level.** ADR-0018 §6.1 asked for "a separate, explicit user and architect acceptance". The owner, deciding as architect in Issue #219 `6200578768`, accepted this risk for every account whose owner accepts it in the app. The record names that decision as its contract reference. No per-account GitHub comment is needed (§11 item 4).
- **Withdrawable.** 「수락 철회」 appends a withdrawal; the layer then refuses for that account until a new acceptance. Append-only, nothing deleted.
- **A changed statement needs a new acceptance.** A different statement digest or contract version proves nothing.
- **Existing proofs stay valid.** A GitHub-form proof recorded by `icbm live record-residual-risk-acceptance` still satisfies the layer for its account.
- It still authorizes nothing: it is one layer (ADR-0018 §10), and every other layer still decides.

## 6. What stays exactly as it is

Every mutation an approval leads to is admitted only by **every** layer of ADR-0018 §4.3, at send time, in the one unit that starts its attempt: the execution mode is `LIVE`; the brake is `RELEASED`; an `ACTIVE` grant of the stage matches its exact unit with budget left; the stage's readiness is `READY` (restore proof, retention, visual acceptance, residual risk, the upload-attempt owner); the endpoint is adopted; for CREATE the ADR-0014 §26 scope is `ACTIVE`; the attempt starts with its budget consumption; the stage's own gate passes; the truth fence holds. The approval satisfies none of them by itself.

Unchanged, and still binding as written:
- **ADR-0018 invariants G3-01 to G3-17, G3-19 to G3-29**, and G3-30 except where §5.2 amends who gives the architect half. In particular: G3-02 deny by default; G3-03 the grant is the authority, UI text never is; G3-04 budget consumed at start, never refunded; G3-05 terminal states are terminal; G3-07 no blind replay; G3-08–G3-10 the brake; G3-11 the §26 scope brake is CREATE-only and unweakened; G3-16 stage-bound, freshness-bound restore proofs; G3-17 retention; G3-21 exact units, no wildcard; G3-22 and G3-24–G3-29 the upload-attempt owner and its replay fence; G3-23 no raw confirmation prose.
- **ADR-0014** §9–§11, §15, §24 (except that the "explicit user authorization of the write scope" is given in the app, per product), §26 and §28 (except §28.7's architect half, §5.2).
- **ADR-0011, ADR-0016, ADR-0039, ADR-0042.**
- The execution-mode owner's rules: `DRY_RUN` at boot and after every restart; one bounded window in process memory, at most four hours, never past its scope, never widened.

## 7. Amendments, each effective when its slice lands

| document | amendment | slice |
| --- | --- | --- |
| **ADR-0013 §7** ("The registration guard") | Under `pricing-rule/v2` the target margin is the pricing context's `target_net_margin` (the seller's Settings value), no minimum margin applies, and `LOSS` (net profit below 0) and `BELOW_TARGET_MARGIN` are warnings, never "not registerable". `pricing-rule/v1` is unchanged for every v1 snapshot | L1 |
| **ADR-0013 §8** (pricing readiness) | "a guard of `LOSS` or `BELOW_MIN_MARGIN` gives `BLOCKED`" holds for v1 snapshots only; a v2 guard gives a `READY`-status warning reason (§2.2) | L1 |
| **ROADMAP "Pricing contract"** | A note: the target net margin is the seller's per-store Settings value with no floor (ADR-0043 §2). The canonical rule is unchanged | L1 |
| **ADR-0018 §4.1** and its amendment notes | **Amended.** Beside the `icbm live` family, the brake is also released and engaged by the in-app actions of §3.7 (L2), and grants are also issued, and the upload, restore drills and retention proofs also run, through the in-app approval and its server-side run (§3, L4). "never a new screen or route" and "never a screen or route" no longer hold for these. The upload may run in the server process, inside the approval's window. Every other sentence stands | L2, L4 |
| **ADR-0018 §2** amendment note | "issuing the grant is the protected action that carries the user's approval … recording its GitHub evidence is the agent's bookkeeping" — for an in-app approval, the approval itself is that protected action and its durable record is the evidence; no GitHub record exists for it | L4 |
| **ADR-0018 §3.2** ("approval" row) | The approval reference is either the GitHub authorization reference (CLI) or the in-app approval (§8). An approval-issued ASSET grant also binds its one artifact (§3.1) | L4 |
| **ADR-0018 §3.3** | A note: the in-app approval is a server-validated protected action that creates grants; "UI text and checkboxes are never authority" stands — the grants are | L4 |
| **ADR-0018 §6.1** and its amendment notes, **ADR-0014 §28.7** | The user's acceptance is recorded in the app once per account (§5.2); the architect's is given at contract level by Issue #219 `6200578768`. GitHub-form proofs stay valid | L2 |
| **ADR-0018 §9**, **G3-18**, **G3-31** | A visual acceptance is bound to a release build's identity (code digest, schema head, release identifier) and shipped with it (§5.1); the git-checkout binding stays for development. "a merged commit is a new accepted code SHA" reads "any change to the running code is a new release identity". The approval surfaces join the required surfaces | L3 |
| **ADR-0018 §10** table | The residual-risk row reads "recorded for the stage's account (§5.2)"; the visual row reads "`VISUAL_ACCEPTANCE_RECORDED` for the running build" | L2, L3 |
| **ADR-0018 G3-30** | "a recorded explicit user and architect acceptance" — the user's in the app per account, the architect's at contract level (§5.2) | L2 |
| **ROADMAP §14.2** | The residual-risk and visual bullets follow §5; a note names the in-app approval as the user's scope approval for one product | L2, L3, L4 |
| **ARCHITECTURE §13** | The visual acceptance is "bound to the running release build"; the scope's evidence is the in-app approval record or, on the CLI path, the agent's GitHub bookkeeping | L3, L4 |
| **`documents/acceptance/gates/G3-VISUAL.md`** | The release-build recording procedure beside the git-checkout one | L3 |
| **`documents/rules/07-execution-safety.md` §7.1–§7.2** | A note: a seller's in-app approval under ADR-0043 is the user's approval of one bounded LIVE scope — one product, one account, ASSET and CREATE, at most 30 minutes — recorded durably in the app; it needs no GitHub record. An agent never gives or simulates it. An agent's own LIVE run still needs the GitHub scope and the user's approval (§11 item 1) | L4 |
| **Repository rule tests** (`tests/contracts/test_repository_rules.py`) | The one-recording-path tests of the visual and residual-risk records, and any pin of CLI-only grant issuance, name the new in-app paths as the only additional callers | L2, L3, L4 |

## 8. Owners and storage (migrations from `0071`, one per slice)

| owner | holds | slice | migration |
| --- | --- | --- | --- |
| **M4 pricing** (`app/stages/products/pricing.py`, `pricing_store.py`, `atomic_sku_economics_store.py`) and the **target policy** (`app/stages/register/target_policy.py`) | `pricing-rule/v2`, `pricing-context/v2` with `target_net_margin`, `registration-target-policy/v2`; both snapshot tables rebuilt with CHECKs by rule version (§2.3) | L1 | `0071_pricing_rule_v2` |
| **live safety** — residual risk and brake (`residual_risk.py`, `store.py`) | `residual_risk_acceptances` gains the in-app form (an evidence kind column: `GITHUB_COMMENTS` or `IN_APP`; for `IN_APP` the actor and both digests, and the withdrawal record); `protected_write_brakes.release_is_authorized` admits a typed in-app reference beside a comment id | L2 | `0072_in_app_acceptance_and_brake` |
| **live safety** — visual (`visual.py`, `code_identity.py`) | `visual_acceptances` gains the release binding: `code_sha` nullable, `release_id` and a binding kind (`GIT_CHECKOUT` or `RELEASE_BUILD`), a CHECK that each kind has exactly its identity | L3 | `0073_release_visual_acceptance` |
| **live safety** — approvals (new, `app/capabilities/live_safety/approvals.py`) | `live_registration_approvals`: identity, `request_id` (unique), account, preparation and revision, candidate fingerprint, packet version and digest, approver, times, state (`OPEN` → one terminal state), end code and step, correlation identity; a unique CREATE grant per approval. `live_grants` rebuilt (as `0057` did): `authorization_ref` typed as a comment id or an approval identity (`authorization_kind`, an FK to the approval for the latter), and `asset_artifact_sha256` for an approval-issued ASSET grant | L4 | `0074_live_registration_approvals` |

Tables are append-only with delete guards, as in the canary scope (ADR-0018 §8). Approval transitions and grant transitions are audited in the same unit.

The numbers are reservations. If ADR-0042's `0068`–`0070` have not landed when a slice here lands, its migration still chains by `down_revision` to the head at its merge; no slice renumbers another's reservation.

## 9. Slices

**Every slice is accepted by a seller walkthrough on the real UI (owner principle 2026-10-15, 「실제 사용자 기준」).** The agent runs the §0 steps the slice delivers, in the running application:
- at desktop and mobile width;
- with screenshots of what the seller sees at each step, recorded in the slice's acceptance record.

**A step that registers for real (p3–p5) is run only under the owner's go-ahead for that exact product, and the owner presses 「승인하고 등록」 and 「실제 등록 잠금 해제」 themselves** (LAP-13). Tests and CI are necessary and never sufficient.

| # | slice | seller walkthrough at acceptance | tier |
| --- | --- | --- | --- |
| P0 | this ADR | — | docs |
| **L1** | §2: `pricing-rule/v2`, `pricing-context/v2`, the Settings field 「목표 순마진」 with its range and warnings, the readiness warnings, the price-row and 통합DB display, migration `0071` | s1–s2: set 25%, see prices recompute; set −5%, see the warning and save; see a loss product shown 「손실 판매」 and still 「등록 준비 완료」; see an old store keep 35% until saved | `HIGH_RISK` (removes a registration guard; migration rebuild of two pricing tables) |
| **L2** | §5.2 and §3.7: the in-app residual-risk acceptance and withdrawal; 「실제 등록 잠금 해제」 / 「실제 등록 잠그기」; migration `0072`; the route guards of §4 | s3–s4: accept, withdraw, accept again; release and engage the brake (the owner presses release); a cross-origin request is refused | `HIGH_RISK` (a safety gate's authorization path) |
| **L3** | §5.1: the release manifest, the shipped acceptance record, startup verification, `--release` recording, the approval surfaces in the harness (`g3-visual-acceptance/v4`), `G3-VISUAL.md`, migration `0073` | r1–r2: record against a release build outside git; the build's server reports the layer recorded; change one served file and see it refuse with the p2 line | `HIGH_RISK` (a safety gate's binding) |
| **L4** | §3: the approval owner, the packet, the readiness, the approval route, the server approval run (retention, drills, in-server upload, freeze, CREATE grant, send), cancel, expiry, restart handling, the UI of p2–p6, migration `0074` | p1–p6 on a canary test product (ROADMAP canary plan): one real registration from the screen, 등록성공 on the store; then a second tab, an expired approval and a moved packet each refusing; 「실제 등록 잠그기」 mid-run stopping before the next mutation | `HIGH_RISK` (LIVE) |

Order: L1 → L2 → L3 → L4. L1 is independent of LIVE and unblocks pricing at once. L2 and L3 make the two acceptances reachable without GitHub or git. L4 needs both: without them §3.3 refuses every approval.

Each slice follows the global merge lane.

## 10. Resolved decisions

| # | question | decision | source |
| --- | --- | --- | --- |
| D1 | who sets the target margin, and is there a floor? | the seller, in Settings; no minimum; a loss is a warning, never a block | owner `6200578768` |
| D2 | how is a real registration approved? | in the app, once per product: exact ASSET and CREATE grants, budget 1, short expiry; the server runs the upload, drills and retention proof; every §4.3 layer per mutation; the brake separate | owner `6200578768` |
| D3 | where do the two acceptances live? | visual: once per release build, by the developer, shipped with it; residual risk: once per account, in the app, by the owner | owner `6200578768` |
| A1 | where is the margin set? | per store, in the target policy's pricing context, beside the fees it is computed against | implementation agent (ADR-0022), 2026-10-16 |
| A2 | which guards does v2 keep? | `LOSS` = net profit below 0 with every policy cost; `BELOW_TARGET_MARGIN`; both `READY`-status warnings | implementation agent, 2026-10-16 |
| A3 | do existing stores switch to v2 automatically? | no: a revision is never rewritten; the store keeps v1 until the seller saves once, with a visible prompt | implementation agent, 2026-10-16 |
| A4 | the margin's range | `−100% < t < 100%`, at most two decimals, and reachable with the store's rates | implementation agent, 2026-10-16 |
| A5 | how many ASSET grants? | one per artifact that needs transmission, budget 1 each, bound to its one artifact; none for a rebound or frozen unit | implementation agent, 2026-10-16 (owner's "budget 1") |
| A6 | the approval's expiry | 30 minutes, the grants' and the LIVE window's bound (as ADR-0042 A4) | implementation agent, 2026-10-16 |
| A7 | concurrent approvals | one `OPEN` approval at a time | implementation agent, 2026-10-16 |
| A8 | does the brake get a screen action? | yes: release in-app with its own confirmation, engage in one press; semantics of ADR-0018 §4.1 unchanged | implementation agent, 2026-10-16 (owner's "brake separate") |
| A9 | a restart during a run | the run job is `DEAD`, never retried; the approval `INTERRUPTED`; unspent grants revoked | implementation agent, 2026-10-16 |
| A10 | edits during an approval | the preparation is locked while `OPEN`; drift is still detected at every step | implementation agent, 2026-10-16 |
| A11 | where do drills restore? | a fresh root under a configured directory outside the data directory; the restored copy is removed when the approval ends, the sanitized proof is kept | implementation agent, 2026-10-16 |
| A12 | what identifies a release build? | code digest + schema head + release identifier; the commit is provenance only; the shipped record is outside the digest's file set | implementation agent, 2026-10-16 |
| A13 | the residual-risk record's life | append-only, withdrawable, per account; GitHub-form proofs stay valid | implementation agent, 2026-10-16 |
| A14 | an `UPLOAD_UNKNOWN` mid-run | the run stops before the freeze; no partial CREATE | implementation agent, 2026-10-16 |

## 11. Points for the owner's confirmation at audit

Each has a default above. The ADR is complete with the defaults; the owner may change any before it merges.

1. **Rule §7.1–§7.2 note (§7).** The rules say LIVE needs its scope "explicit in GitHub". The default reads the seller's in-app approval as the user's scope approval for one product, with no GitHub record. This changes a rule file.
2. **Negative target margins (A4).** The default allows a target below 0% with a warning, taking "no floor" literally. The alternative is a 0% floor on the setting only, with losses still possible through a supplier minimum.
3. **Existing stores (A3).** The default keeps 35% / 10% until the seller saves once. The alternative appends a server-created v2 revision at 35% for every store at L1, so the floor disappears at once.
4. **The architect half of the residual-risk acceptance (§5.2).** The default reads `6200578768` as the architect's contract-level acceptance for every account whose owner accepts in the app.
5. **One product at a time (A7), and 30 minutes (A6).** No batch REGISTER approval exists until a later decision.

## Invariants

```text
LAP-01  one in-app approval covers exactly one provider-listing unit of one store account; it issues one ASSET grant of budget 1 per artifact needing transmission, each bound to the exact ASSET unit and its one artifact, and at most one CREATE grant of budget 1, issued only after the freeze; every grant's window ends with the approval, at most 30 minutes after it; no wildcard, unit-less or other-stage grant exists
LAP-02  an approval is a server-validated protected action over a server-built packet whose SHA-256 the client echoes; a moved packet refuses; the client never builds a packet; the grants are the authority, UI text and confirmations never are; nothing the seller types is persisted
LAP-03  the CREATE grant is issued only when the frozen Snapshot derives from exactly the approved preparation revision, candidate fingerprint, artifact set and pricing snapshots and reproduces the approved packet; otherwise the approval stops and nothing is sent
LAP-04  every layer of ADR-0018 §4.3 admits every upload and every CREATE at send time, in the unit that starts its attempt; an approval satisfies no layer by itself and the mode alone is never authority
LAP-05  an approval never releases the brake; releasing is a separate explicit in-app or CLI action with its own confirmation, audited with an authorization reference; engaging is always one action; every ADR-0018 §4.1–§4.2 rule of the brake is unchanged
LAP-06  at most one approval is OPEN at a time; a request_id is idempotent and belongs to one approval for ever; an ended approval is never reopened or reused
LAP-07  the approval, brake and acceptance routes accept only a loopback-host, same-origin JSON request carrying X-ICBM-Client and refuse an extension origin; no GET has a side effect
LAP-08  nothing is issued, opened or sent after an approval's expiry; the LIVE window never outlasts the approval and is closed, and unspent grants revoked, whenever the approval ends; a restart ends a running approval INTERRUPTED and never resumes it
LAP-09  an UNKNOWN CREATE is never resent and an UPLOAD_UNKNOWN never retried; an UPLOAD_UNKNOWN stops the run before the freeze; no approval is admitted for a unit with an unresolved UNKNOWN, conflict scope or fenced replay key
LAP-10  per approval the server proves retention, takes the ASSET restore drill before the first upload and the CREATE restore drill after the freeze, each into a fresh root outside the data directory; a proof that does not pass stops the approval before any transmission it gates
LAP-11  cancelling, expiry or an engaged brake stops every mutation not yet started; an attempt already handed to the transport completes as itself and is recorded under ADR-0014 §9–§10
LAP-12  a preparation is locked against edits while its approval is OPEN, and drift is still detected at every step
LAP-13  an agent never gives, clicks or simulates an in-app approval, a brake release or a residual-risk acceptance; a slice acceptance that registers for real is clicked by the owner under the owner's go-ahead for that product
LAP-14  the residual-risk acceptance is recorded in the app once per account by its owner over the exact ADR-0018 §6.1 statement, append-only and withdrawable; a changed statement proves nothing; it authorizes nothing by itself
LAP-15  a visual acceptance is recorded by the developer once per release build, bound to its code digest, schema head and release identifier, and shipped with it; any change to the running code or schema makes it refuse; a build whose code identity cannot be computed has none
LAP-16  under pricing-rule/v2 the target margin is the store's audited Settings value, pinned in every snapshot; no minimum margin exists; LOSS (net profit below 0) and BELOW_TARGET_MARGIN are warnings shown wherever the price is shown, including the approval packet, and never block
LAP-17  pricing-rule/v1 snapshots keep every v1 CHECK; no snapshot or policy revision is rewritten; a margin change creates new snapshots; the canonical pricing rule is unchanged
LAP-18  a target margin no price can reach is refused at save; every margin is an exact fraction, never a float
LAP-19  every approval, grant, brake and acceptance transition is audited in the same unit of work with actor, time and correlation identity
LAP-20  the icbm live command family keeps working unchanged beside the in-app path; no second grant, brake, drill, retention, upload or acceptance owner exists
LAP-21  every slice is accepted by a seller walkthrough on the real UI at desktop and mobile width, not by tests alone
```

## References

- Issue #219 `6200578768` (D1–D3), `6109102895` (seller-risk principle), `6185598957` (visual `<details>` amendment), `6190978708` (ADR-0042 Q1–Q7)
- ROADMAP "Pricing contract", §14 items 4–7, §14.1, §14.2; ARCHITECTURE §13; GLOSSARY §1
- ADR-0002, ADR-0005 (job restart rule), ADR-0006; ADR-0013 §7, §8; ADR-0014 §2, §9–§11, §15, §24, §26, §28, §28.5, §28.7, §29; ADR-0015 §2; ADR-0018 §2, §3.1–§3.5, §4.1–§4.3, §5.1, §6.1, §7–§10, G3-01–G3-31; ADR-0020 §3; ADR-0022 §3; ADR-0038 §4–§5; ADR-0039 §3, SP-04; ADR-0042 §11, §12, §15 A4
- `documents/rules/06-immutable-domain-rules.md` §6.1; `documents/rules/07-execution-safety.md` §7.1–§7.3; `documents/rules/14-operating-authority.md` §14.2
- `documents/acceptance/gates/G3-VISUAL.md`; `documents/acceptance/milestones/M4.md`
- `app/stages/products/pricing.py` (`TARGET_NET_MARGIN`, `MINIMUM_NET_MARGIN`, `calculate`, `PricingDependencies.fingerprint`), `readiness.py`, `models.py`, `atomic_sku_economics_models.py`; `app/stages/register/target_policy.py`
- migrations `0013` (pricing snapshots), `0026` (live authority), `0029`, `0030`, `0036`, `0047` (AtomicSKU pricing snapshots), `0057` (DISPATCH grant rebuild), `0066` (landed cost)
- `app/interface/cli.py` (`icbm live`), `app/interface/api/middleware.py` (`ClientHeaderGuard`), `app/main.py` (`ALLOWED_HOSTS`, `TrustedHostMiddleware`), `app/interface/api/routes/system.py` (execution mode), `routes/register.py`
- `app/platform/system/execution_mode.py`; `app/platform/core/code_identity.py`; `app/capabilities/live_safety/` (`authority.py`, `stack.py`, `proofs.py`, `drill.py`, `retention.py`, `visual.py`, `residual_risk.py`, `upload_run.py`, `model.py`, `models.py`)
