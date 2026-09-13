# v29 Prototype Changes — review request

Status: **PROPOSAL — awaiting architect approval**
Author: Claude
Base: `icbm_redesign_test_v28_icbm_new_gaps.html` (`3689b86c…`)
Result: `icbm_redesign_test_v29_final.html` (`896ad870…`, 323,751 bytes)
Place at: `docs/review/V29-CHANGES-BY-CLAUDE.md`

---

## What this is

A defect and consistency pass over v28, plus a single-definition platform
identity layer. **No screen, flow, field or information-architecture change.**
Every v28 surface listed in `docs/UI_BUILD_GAPS.md` is present and unchanged.

The architect decides whether this becomes the approved visual source. Until
then v28 remains approved and this file records exactly what differs.

---

## 1. Defects fixed

### 1.1 Duplicate help icon on every page title

Three generations of the same feature were live simultaneously:

| Generation | Entry point | Icon class |
| ---------- | ----------- | ---------- |
| v18 | `attachHelpTooltip()` | (trigger only) |
| v22 | `wire()` | `.ui-help-icon` |
| v27 | `addHelp()` | `.icbm-help-icon` |

All three target the same `h1`. v27 was written to supersede v22 and even
contains migration code, but v22's duplicate guard checks only for its own
class, so it appended a second icon after v27 had already added one.

Fixed by widening that guard. v18/v22 were **not** deleted: v18 uniquely
provides tooltips for `.extension-card > p` and `.shopping-agent-head span`,
which v27 does not cover. Verified: 10 page titles, 0 with more than one icon,
total help icons 104 → 52.

**This is a patch chain, which `ROADMAP.md` §1.1 forbids inheriting.** The fix
keeps the prototype usable; the M0 rebuild should implement one help system, not
port these three.

### 1.2 Channel price colour carried no meaning

```css
.db-table tbody td:nth-child(7){color:#14875a}  /* 네이버 always green */
.db-table tbody td:nth-child(8){color:#d85854}  /* 쿠팡 always red */
```

Colour was fixed **per column**, unrelated to the values. Coupang's price
rendered red whatever it was. In an operations console red should mean "you must
act"; here it competed with `확인필요`, `실패` and `품절`.

Note: the row template also carried the same colours as inline styles, which
overrode the stylesheet. Both were removed. All channel price columns are now
neutral; status chips carry the semantics.

### 1.3 Lists scrolled vertically with an empty page below

`syncAdaptiveListHeights()` computed `available = viewport − listTop − reserve`
and applied it as `max-height` **without ever measuring content**. A 7-row table
scrolled while several hundred pixels sat empty underneath.

Two corrections:

- `reserveBelow` was 175px in landscape to keep the detail area visible — but in
  landscape the detail sits *beside* the list, not below. Reduced to 48px
  (portrait 245 → 160).
- the list no longer applies `max-height` below its own content height, and the
  horizontal scrollbar's own height is added back (`scrollHeight` excludes it,
  which clipped lists by ~16px on the first attempt).

Recalculation now also runs on `load`, `hashchange` and via `ResizeObserver`,
because rows are script-rendered after first paint and the original code only
listened for `resize` and navigation clicks.

### 1.4 Horizontal scrollbar where the table already fitted

`.adaptive-list > .table` used `width:max-content`, so any overflow — including
the 15px reserved by `scrollbar-gutter:stable` — produced a scrollbar. Above
1100px the table now fits the panel and long text columns yield via ellipsis;
below 1100px the previous behaviour is retained, since there the width genuinely
is insufficient.

`.panel`-hosted tables (dashboard 최근 수집 현황, settings 사용자 권한) were a
separate case: the global `.table{min-width:760px}` applied to them directly.
Released above 1100px.

### 1.5 Dashboard panel grid left an empty column

A `#dashboard .panels` override pinned a three-track ratio at a specificity that
also defeated the responsive rules, so two-column widths kept three tracks and
rendered the third empty. Redefined per width band.

### 1.6 AI Shopping Insight KPI row

Wrapped to 3+2 while the same `.page-kpis` fitted 5 on every other page. Forced
to 5 above 1100px.

---

## 2. Accessibility and consistency

| Change | Before | After |
| ------ | ------ | ----- |
| `--text-faint` | `#909ba5` (2.8:1) | `#6b7682` (4.6:1) |
| `--text-sub` | `#6f7d89` (4.1:1) | `#5c6874` (6.0:1) |
| `--muted` | `#6e7b89` | `#5c6874` |
| Keyboard focus | 5 `:focus` rules / 220 buttons | global `:focus-visible` ring |
| Reduced motion | not honoured | `prefers-reduced-motion` respected |
| Help icon | 14px circle / 8px glyph | 16px / 10.5px |
| AI confidence label | `추천 신뢰도` | `카테고리 추천 신뢰도` |
| Dashboard slogan | brand statement appeared 3× on one screen | 2× |

Contrast was the highest-value fix: the faint tone failed even the large-text
threshold, and it is the tone used for supporting text throughout.

---

## 3. Platform identity

### Problem

Platform names were plain text in at least six places (dashboard table, donut
legend, integrated-DB column headers, orders, registration chips, settings), and
the donut legend hard-coded its own colours inline, which matched nothing else.
Adding a logo meant editing six places; changing one meant editing six again.

### Change

One registry, one renderer:

```js
const PLATFORM = {
  smartstore: { label:'스마트스토어', color:'#03C75A', aliases:[…], logo:'data:…' },
  coupang: { … }, gmarket: { … }, st11: { … }, auction: { … },
};
platformTag(key)              // logo only (default)
platformTag(key,{name:true})  // logo + label
```

Existing plain text is upgraded by a scan restricted to leaf elements whose text
matches a platform exactly, or the `플랫폼 · 나머지` form (`네이버 · 일반`,
`11번가 · ICBM0001245`). 59 occurrences now render from this definition.

Decisions worth recording:

- **Labels hidden by default.** The wordmarks already spell the brand; the text
  was duplication. `alt` retains the name for assistive technology.
- **Table headers excluded.** Wordmarks are wider than the text they replace and
  would widen the integrated-DB channel columns enough to reinstate §1.4.
- **Fixed 72×18 box, `object-fit:contain`.** Aspect ratios range from 3.1
  (11번가) to 6.4 (AUCTION.); sizing by height alone would make column widths
  jump per row.
- **Chips lose their tint** (`pf-chip`) so a logo never sits on a coloured field.
- **`logo: null` falls back** to a brand-coloured monogram, so a platform can be
  added before its mark is available.

### Logos

User-supplied. Extracted from a composite sheet by detecting non-white regions
programmatically rather than cropping by hand, then background-keyed to
transparency and palette-quantised: 5 marks, 11.4KB total, embedded as data URIs
so the prototype remains a single file.

These are **placeholders**. The user intends to replace them with square symbol
marks. That replacement touches `PLATFORM.<key>.logo` and nothing else — which
is the reason for the registry.

Trademark note: these are third-party registered marks used indicatively to
identify a connected marketplace. Before public release, each platform's brand
guideline should be checked (clear space, minimum size, no modification, no
implied endorsement).

---

## 4. Deliberately NOT changed

These are the architect's calls, not an implementer's. Listing them so the
omission is visible rather than silent.

### 4.1 Registration automation toggles — needs a ruling

`등록관리 > 플랫폼별 등록 설정` offers three toggles, **all defaulting to ON**:

| Toggle | Conflicts with |
| ------ | -------------- |
| 등록 실패 자동 재시도 | `CLAUDE.md` §7.3 / `ARCHITECTURE.md` §7 — an unknown CREATE result must be reconciled, never blindly retried |
| 카테고리 자동매칭 | B3 — AI proposes, the persisted mapping decides |
| 자동 가격조정 | §8.2b — a source price change produces a *proposal*, not a silent application |

The prototype predates the architecture review, so this is expected. But the UI
currently offers automation authority the architecture forbids, and it will
surface at M5.

Suggested resolution: scope auto-retry to `TRANSIENT` / `RATE_LIMITED` only —
never `UNKNOWN`, `VALIDATION` or `POLICY_BLOCKED` — and default the other two to
OFF. Alternatives are removal, or keeping them with explicit scope labels.

### 4.2 BLOCKED is missing from the registration KPI row

Counters are `등록 대기 / 완료 / 실패 / 재시도 필요 / 카테고리 확인`. The
compliance state that can legally stop a listing is not among them. It exists
inside the preflight modal, but the count that matters most is not visible at a
glance. Adding it implies a data contract, so it is left for the architect.

### 4.3 Token system

~20 font sizes, 15 padding values, 9 radii, and a `--r` token used four times.
Touching this means rewriting 6,600 lines with no way to verify the result
visually. It belongs in M0 as `tokens.css`, not in a prototype patch. See the
open question in `docs/UI_SOURCE_OF_TRUTH.md`.

### 4.4 Legacy help systems

Kept for the coverage reason in §1.1. The M0 rebuild should implement one.

---

## 5. Verification performed

Structural checks were run against a real DOM (jsdom), comparing v28 and v29:

```
page titles                 10 → 10
titles with >1 help icon    10 → 0
help icons total           104 → 52
buttons                    212 → 212     (unchanged)
tables                      18 → 18      (unchanged)
page sections               10 → 10      (unchanged)
toggles                     30 → 30      (unchanged — see §4.1)
platform tags                0 → 59
logo <img> decoding to PNG       59 / 59
logos missing alt                 0
platform names left as text       0
```

**Not verified: visual layout.** §1.3–§1.6 change how widths and heights are
computed, and no rendering check was possible in the authoring environment. The
user confirmed the dashboard, integrated DB, orders, sold-out and AI insight
screens by screenshot during iteration. Registration, inquiries and analytics
have not been visually reviewed since the platform logos landed, and column
widths there may need a look before approval.

One process note: an intermediate build shipped with every logo broken. The
check in use counted `<img>` elements rather than decoding their `src`, so it
reported success. The check now decodes each `src` and verifies the PNG
signature. Counting that something exists is not evidence that it works.

---

## 6. Requested decision

1. Accept v29 as the approved visual source, or return it with changes.
2. Rule on §4.1 (automation toggles) — blocking for M5, not for M0.
3. Rule on §4.2 (BLOCKED counter).
4. Rule on literal vs structural reproduction (`UI_SOURCE_OF_TRUTH.md`).

Items 2–4 do not block M0 starting.
