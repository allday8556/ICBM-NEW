// 상품 편집기 (2026-10-08 owner UX, phase 1): one provider-listing unit in its own tab.
//
// 등록관리 → a unit's row → 상품등록 / 상품수정 → this page in a new tab, in six steps: 기본정보,
// 대표이미지, 옵션·가격, 상품정보고시, 상세페이지, 기타 등록정보. Phase 1 places the REGISTER owner's
// existing pieces by step and adds no contract: the unit read (GET /api/v1/register/units/{draft}),
// the authoring form and its save, the category requirements, the image editor, the repin, the
// preflight with its area-classified reasons, the snapshot preview and the unit's own actions. A v29
// slot no owner fills yet keeps its place and reads 데이터 없음 or 준비 중 (owner decision 가); a
// control without a contract is markInert. The bottom 등록 준비 strip colours each step from the
// server's preflight reasons only: nothing here judges readiness. An AI control (background
// removal, image translation) keeps its approved place as an inert 준비 중 placeholder that sends
// nothing until its server owner exists (owner decision 2026-10-08, Issue #219 comment 6054956408;
// ROADMAP, Issue #127). 상품수정 is read-only in phase 1: no contract edits a registered listing yet,
// and a unit with a Snapshot or an Intent is never authored here.
//
// ADR-0033 G4: the 상세페이지 step's 상단 공지 and 하단 공지 show the notices the server resolves for
// the unit (GET /api/v1/register/detail-guidance), in order, and offer 기본값 / 끄기 / 직접 작성. 직접
// 작성 is the settings card's own editor (components/guidance-editor.js): the same five-template
// live preview and presets. The choice is saved with the preparation through the same save; the
// server validates and draws a product's own notice, and states PUBLICATION_GUIDANCE_UNPLACED while
// no composition can place a notice (G5).

import { ApiError, getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { markInert } from '../core/inert.js';
import { closeModal, openModal } from '../core/modal.js';
import { platformTag } from '../core/platform.js';
import { toast } from '../core/toast.js';
import { NAME_RESULT, NAME_TASK, aiNamePanel } from '../components/ai-name.js';
import { TAG_RESULT, TAG_TASK, aiTagsPanel } from '../components/ai-tags.js';
import { CATEGORY_RESULT, CATEGORY_TASK, aiCategoryPanel } from '../components/ai-category.js';
import { itemImagesEditor } from '../components/item-images.js';
import { emptyState } from '../components/states.js';
import {
  GUIDANCE_BASE,
  OPENING,
  PLACEMENTS,
  composer,
  errorText,
  exampleNote,
  labelOf,
  presetBar,
} from '../components/guidance-editor.js';
import {
  PREPARATION_LABEL,
  REASON_COPY,
  actionCell,
  attemptRow,
  authoringForm,
  categoryBlock,
  chip,
  itemRow,
  kv,
  preflightBlock,
  previewBlock,
  readStateChip,
  reason,
  repinBlock,
  scopeBlock,
  table,
  unitItemsKey,
} from './register.js';

const NO_DATA = '데이터 없음';
const READINESS = '/api/v1/register/readiness';
// Each step and the reason areas (app/stages/register/reason_areas.py) it answers for. A reason
// belongs to the step of the first area the server gave it; every area has exactly one step.
const STEPS = [
  ['basic', '기본정보', '메인이미지 · 상품명 · 태그 · 카테고리', ['CATEGORY', 'LISTING']],
  ['images', '대표이미지', '추가 · 제거 · 순서 · 편집', ['IMAGES']],
  ['price', '옵션 · 가격', '가격 · 옵션 · 옵션값', ['PRICE', 'OPTIONS']],
  ['notice', '상품정보고시', '고시 유형 · 항목', ['NOTICE']],
  ['detail', '상세페이지', '상·하단 공지 · 본문', ['DETAIL']],
  ['etc', '기타 등록정보', '배송 · A/S · 계정 · 안전', ['ACCOUNT', 'POLICY', 'DUPLICATE', 'SAFETY', 'SOURCE', 'UNIT', 'UNCLASSIFIED']],
];
const BAD_STATUSES = new Set(['BLOCKED', 'DUPLICATE']);
const STATE_LABEL = { good: '통과', warn: '보완 필요', bad: '불통', none: '평가 전' };

function noData(label) {
  return h('div', { class: 'kv', 'data-no-data': label }, h('span', {}, label), h('b', { class: 'no-data' }, NO_DATA));
}

function pending(label, copy) {
  return markInert(h('button', { type: 'button', class: 'btn' }, label), copy ?? label);
}

// An AI control ahead of its server owner: its place only, inert, sending nothing.
function aiPending(label) {
  return markInert(h('button', { type: 'button', class: 'btn ai', 'data-ai-placeholder': label }, `✨ ${label}`), `${label} · 준비 중`);
}

// The server's preflight reasons, grouped by step. Without a preflight the step is not judged.
function stepStates(unit) {
  const reasons = unit.preflight?.reasons ?? [];
  return STEPS.map(([, , , areas]) => {
    if (!unit.preflight) return { state: 'none', reasons: [] };
    const mine = reasons.filter((item) => areas.includes((item.areas ?? [])[0] ?? 'UNCLASSIFIED'));
    if (!mine.length) return { state: 'good', reasons: [] };
    return { state: mine.some((item) => BAD_STATUSES.has(item.status)) ? 'bad' : 'warn', reasons: mine };
  });
}

function readyStrip(unit, states, go) {
  return h(
    'div',
    { class: 'ready-strip', 'data-role': 'ready-strip' },
    ...STEPS.map(([key, label], index) => {
      const { state, reasons } = states[index];
      const lines =
        state === 'none'
          ? [h('li', {}, REASON_COPY[unit.preflight_unavailable_reason] ?? 'Preflight 평가 전입니다. 기타 등록정보의 Preflight 평가로 판정합니다.')]
          : state === 'good'
            ? [h('li', {}, '이 단계의 사전 점검 사유가 없습니다.')]
            : reasons.map((item) =>
                h('li', { 'data-reason': item.code }, REASON_COPY[item.code] ?? item.code, item.subject ? h('span', { class: 'tip-sub' }, ` · ${item.subject}`) : null),
              );
      return h(
        'button',
        { type: 'button', class: `ready-item ${state}`, 'data-step': key, 'data-ready': state, onclick: () => go(index + 1) },
        h('span', { class: `dot ${state}`, 'aria-hidden': 'true' }),
        label,
        h('span', { class: 'tip', role: 'tooltip' }, h('b', {}, `${label} · ${STATE_LABEL[state]}`), h('ul', {}, ...lines), h('span', { class: 'tip-sub' }, '누르면 이 단계로 이동합니다.')),
      );
    }),
  );
}

// The representative image of the first Item, through the image owner's own read.
function mainImage(unit) {
  for (const item of unit.items) {
    const asset = (item.publication_assets ?? []).find((candidate) => candidate.role === 'REPRESENTATIVE');
    if (!asset) continue;
    const holder = h('div', { class: 'bigthumb', 'data-role': 'editor-main-image' }, h('span', { class: 'chip good' }, '메인'));
    const image = h('img', { alt: '메인 이미지', src: `/api/v1/products/items/${encodeURIComponent(item.item_id)}/images/${encodeURIComponent(asset.sha256)}` });
    image.addEventListener('error', () => image.replaceWith(h('span', { class: 'no-data' }, '이미지를 불러오지 못했습니다')));
    holder.append(image);
    return holder;
  }
  return h('div', { class: 'bigthumb', 'data-role': 'editor-main-image', 'data-thumb': 'none' }, h('span', { class: 'no-data' }, NO_DATA));
}

function platformTabs(unit) {
  return h(
    'div',
    { class: 'pf-tabs', 'data-role': 'platform-tabs' },
    h('button', { type: 'button', class: 'on', 'aria-pressed': 'true' }, platformTag(unit.marketplace_key, { name: true })),
    ...['coupang', 'st11']
      .filter((key) => key !== unit.marketplace_key)
      .map((key) => markInert(h('button', { type: 'button', 'aria-pressed': 'false' }, platformTag(key, { name: true })), '플랫폼별 값은 준비 중')),
  );
}

function readOnlyInputs(unit) {
  const inputs = unit.authored?.inputs ?? {};
  return [
    kv('카테고리', inputs.category?.category_id ?? '—'),
    kv('상품명', inputs.name?.value ?? '—'),
  ];
}

const OPERATOR = 'operator';
const APPLY_COPY = {
  AI_APPLY_FIELD_LOCKED: '확인된 상품명은 AI 값으로 덮어쓰지 않습니다.',
  AI_APPLY_REVISION_CHANGED: '다른 곳에서 먼저 저장했습니다. 다시 불러온 뒤 적용하세요.',
  AI_APPLY_RESULT_UNUSABLE: '최신이고 성공한 추천만 적용할 수 있습니다. 다시 추천받으세요.',
  AI_APPLY_RESULT_CHANGED: '더 새로운 추천이 있습니다. 새 추천을 확인한 뒤 적용하세요.',
  AI_APPLY_VALUE_INVALID: '추천에 상품명이 없습니다.',
};
// A confirmed or source value is never overwritten by an AI value (ADR-0026 §7).
const LOCKED_PROVENANCE = new Set(['OPERATOR_CONFIRMED', 'SOURCE_FACT']);

// ADR-0027 AIS-2: the product's current name recommendation beside the name field, and its apply
// as AI_SUGGESTION naming the exact result revision shown (ADR-0026 §7). The operator's own save
// of the name confirms it (OPERATOR_CONFIRMED); until then the preflight keeps it unconfirmed.
function aiNameBlock(unit, reload) {
  const authored = unit.authored;
  const productGroupId = unit.items[0]?.product_group_id;
  if (!productGroupId) return null;
  const name = authored?.inputs?.name ?? null;
  const applied = name?.provenance === 'AI_SUGGESTION';
  const locked = Boolean(name?.value) && LOCKED_PROVENANCE.has(name.provenance ?? 'OPERATOR_CONFIRMED');
  const apply = h('button', { type: 'button', class: 'btn blue', 'data-action': 'ai-name-apply', disabled: true }, 'AI 추천 적용');
  const why = h('span', { class: 'mini', 'data-role': 'ai-name-apply-why' });
  let shown = null;
  const panel = aiNamePanel(productGroupId, {
    onResult: (result) => {
      shown = result;
      const usable = Boolean(authored && !locked && result?.status === 'OK' && !result.stale && result.value?.recommended);
      apply.disabled = !usable;
      why.textContent = !authored
        ? '먼저 준비 내용을 저장하면 적용할 수 있습니다.'
        : locked
          ? '확인된 상품명이 있어 AI 값으로 덮어쓰지 않습니다. 상품명을 비우고 저장하면 적용할 수 있습니다.'
          : result?.stale
          ? '추천 이후 입력이 바뀌었습니다. 다시 추천받으세요.'
          : usable
            ? '적용하면 저장한 내용 기준으로 다시 불러옵니다. 저장하지 않은 입력은 사라집니다.'
            : '';
    },
  });
  apply.addEventListener('click', async () => {
    if (apply.disabled || !shown) return;
    apply.disabled = true;
    try {
      await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}/apply-enrichment`, {
        actor: OPERATOR,
        expected_revision_no: authored.revision_no,
        field: 'name',
        product_group_id: productGroupId,
        task_key: NAME_TASK,
        result_key: NAME_RESULT,
        result_targeted: false,
        result_sequence: shown.sequence,
        value_field: 'recommended',
      });
      toast('AI 추천 상품명을 적용했습니다', '저장하면 확인된 상품명이 됩니다.');
      reload();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('적용하지 못했습니다', APPLY_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
      apply.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'field ai-name-field', 'data-role': 'editor-ai-name' },
    h('label', {}, 'AI 상품명 추천'),
    applied ? h('span', { class: 'chip warn', 'data-role': 'ai-name-applied' }, 'AI 추천 적용됨 · 저장하면 확인됩니다') : null,
    panel,
    h('div', { class: 'row' }, apply, why),
  );
}

const CATEGORY_APPLY_COPY = {
  ...APPLY_COPY,
  AI_APPLY_FIELD_LOCKED: '확정된 카테고리는 AI 값으로 덮어쓰지 않습니다.',
  AI_APPLY_RESULT_UNUSABLE: '최신이고 성공한, 지금 카테고리 목록 기준의 추천만 적용할 수 있습니다.',
  AI_APPLY_VALUE_INVALID: '추천에 카테고리가 없습니다.',
};

// ADR-0029 §5 (C3): for a SmartStore unit, the account's category recommendation and
// `AI 카테고리 적용` — the exact result revision shown, as an AI_SUGGESTION selection under the
// target's current mapping revision. It never confirms a category (AIC-04): the operator's own
// save of the category does; an operator-confirmed selection is never overwritten (AIC-05).
function aiCategoryBlock(unit, reload) {
  const authored = unit.authored;
  const productGroupId = unit.items[0]?.product_group_id;
  if (unit.marketplace_key !== 'smartstore' || !productGroupId) return null;
  const category = authored?.inputs?.category ?? null;
  const applied = category?.confirmation === 'AI_SUGGESTION';
  const locked = category?.confirmation === 'OPERATOR_CONFIRMED';
  const apply = h('button', { type: 'button', class: 'btn blue', 'data-action': 'ai-category-apply', disabled: true }, 'AI 카테고리 적용');
  const why = h('span', { class: 'mini', 'data-role': 'ai-category-apply-why' });
  let shown = null;
  const target = { marketplace_key: unit.marketplace_key, marketplace_account_id: unit.marketplace_account_id };
  const panel = aiCategoryPanel(productGroupId, target, {
    onResult: (result) => {
      shown = result;
      const usable = Boolean(authored && !locked && result?.status === 'OK' && !result.stale && result.value?.category_id);
      apply.disabled = !usable;
      why.textContent = !authored
        ? '먼저 준비 내용을 저장하면 적용할 수 있습니다.'
        : locked
          ? '확정된 카테고리가 있어 AI 값으로 덮어쓰지 않습니다.'
          : result?.stale
            ? '추천 이후 입력이 바뀌었습니다. 다시 추천받으세요.'
            : usable
              ? '적용하면 저장한 내용 기준으로 다시 불러옵니다. 카테고리는 저장해야 확정됩니다.'
              : '';
    },
  });
  apply.addEventListener('click', async () => {
    if (apply.disabled || !shown) return;
    apply.disabled = true;
    try {
      await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}/apply-enrichment`, {
        actor: OPERATOR,
        expected_revision_no: authored.revision_no,
        field: 'category',
        product_group_id: productGroupId,
        task_key: CATEGORY_TASK,
        result_key: CATEGORY_RESULT,
        result_targeted: true,
        result_sequence: shown.sequence,
        value_field: 'category_id',
      });
      toast('AI 추천 카테고리를 적용했습니다', '저장해야 확정된 카테고리가 됩니다.');
      reload();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('적용하지 못했습니다', CATEGORY_APPLY_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
      apply.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'field ai-category-field', 'data-role': 'editor-ai-category' },
    h('label', {}, 'AI 카테고리 추천'),
    applied ? h('span', { class: 'chip warn', 'data-role': 'ai-category-applied' }, 'AI 추천 적용됨 · 저장해야 확정됩니다') : null,
    panel,
    h('div', { class: 'row' }, apply, why),
  );
}

const TAGS_APPLY_COPY = {
  ...APPLY_COPY,
  AI_APPLY_FIELD_LOCKED: '확인된 태그는 AI 값으로 덮어쓰지 않습니다.',
  AI_APPLY_VALUE_INVALID: '추천에 쓸 수 있는 태그가 없습니다.',
};

// ADR-0028 §6–§7 (T4): the unit's tags, and for a SmartStore unit the account's tag recommendation
// with `AI 태그 적용` — the exact result revision shown, as one AI_SUGGESTION set. The operator's
// own save confirms the set; a confirmed, non-empty set is never overwritten. No tag is sent to
// the marketplace (AIT-01). Tag editing itself stays inert.
function tagsBlock(unit, reload) {
  const authored = unit.authored;
  const tags = authored?.inputs?.tags ?? [];
  const applied = authored?.inputs?.tags_provenance === 'AI_SUGGESTION';
  const current = h(
    'div',
    { class: 'tags' },
    ...(tags.length ? tags.map((tag) => h('span', { class: 'tag' }, `#${tag}`)) : [h('span', { class: 'no-data' }, NO_DATA)]),
    pending('태그 편집', '태그 편집은 준비 중'),
  );
  const productGroupId = unit.items[0]?.product_group_id;
  if (unit.marketplace_key !== 'smartstore' || !productGroupId) {
    return h('div', { class: 'field', 'data-role': 'editor-tags' }, h('label', {}, '태그 / 검색어'), current);
  }
  const locked = tags.length > 0 && !applied;
  const apply = h('button', { type: 'button', class: 'btn blue', 'data-action': 'ai-tags-apply', disabled: true }, 'AI 태그 적용');
  const why = h('span', { class: 'mini', 'data-role': 'ai-tags-apply-why' });
  let shown = null;
  const target = { marketplace_key: unit.marketplace_key, marketplace_account_id: unit.marketplace_account_id };
  const panel = aiTagsPanel(productGroupId, target, {
    onResult: (result) => {
      shown = result;
      const usable = Boolean(authored && !locked && result?.status === 'OK' && !result.stale && result.value?.recommended?.length);
      apply.disabled = !usable;
      why.textContent = !authored
        ? '먼저 준비 내용을 저장하면 적용할 수 있습니다.'
        : locked
          ? '확인된 태그가 있어 AI 값으로 덮어쓰지 않습니다.'
          : result?.stale
            ? '추천 이후 입력이 바뀌었습니다. 다시 추천받으세요.'
            : usable
              ? '적용하면 저장한 내용 기준으로 다시 불러옵니다. 저장하지 않은 입력은 사라집니다.'
              : '';
    },
  });
  apply.addEventListener('click', async () => {
    if (apply.disabled || !shown) return;
    apply.disabled = true;
    try {
      await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}/apply-enrichment`, {
        actor: OPERATOR,
        expected_revision_no: authored.revision_no,
        field: 'tags',
        product_group_id: productGroupId,
        task_key: TAG_TASK,
        result_key: TAG_RESULT,
        result_targeted: true,
        result_sequence: shown.sequence,
        value_field: 'recommended',
      });
      toast('AI 추천 태그를 적용했습니다', '저장하면 확인된 태그가 됩니다. 마켓으로 보내지는 않습니다.');
      reload();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('적용하지 못했습니다', TAGS_APPLY_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
      apply.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'field', 'data-role': 'editor-tags' },
    h('label', {}, '태그 / 검색어'),
    current,
    applied ? h('span', { class: 'chip warn', 'data-role': 'ai-tags-applied' }, 'AI 태그 적용됨 · 저장하면 확인됩니다') : null,
    h('div', { class: 'ai-tags-field', 'data-role': 'editor-ai-tags' }, panel, h('div', { class: 'row' }, apply, why)),
  );
}

const GUIDANCE_READ = '/api/v1/register/detail-guidance';
const GUIDANCE_MODES = [
  ['DEFAULT', '기본값', '설정의 상세페이지 공지를 그대로 씁니다'],
  ['OFF', '끄기', '이 상품에는 이 자리 공지를 넣지 않습니다'],
  ['CUSTOM', '직접 작성', '이 상품만의 상시 공지를 씁니다 · 기간 공지는 그대로 들어갑니다'],
];
const GUIDANCE_UNPLACED = 'PUBLICATION_GUIDANCE_UNPLACED';

function guidanceTime(iso) {
  return new Date(iso).toLocaleString('ko-KR', { dateStyle: 'medium', timeStyle: 'short' });
}

// One resolved notice, as the server resolved it: its image, what it is and its design.
function resolvedEntry(entry, settings, label) {
  const what =
    entry.source === 'PRODUCT' ? '이 상품 공지' : entry.kind === 'PERIOD' ? '기간 공지' : '상시 공지';
  return h(
    'figure',
    { 'data-role': 'dg-entry', 'data-source': entry.source, 'data-kind': entry.kind, 'data-sha256': entry.sha256 },
    h('img', { src: entry.image_url, alt: `${label} · ${what}` }),
    h(
      'figcaption',
      {},
      h('span', { class: entry.source === 'PRODUCT' ? 'chip info' : 'chip' }, what),
      h('span', { class: 'chip' }, settings ? labelOf(settings, entry.template) : entry.template),
      entry.kind === 'PERIOD' && entry.ends_at ? h('span', { class: 'mini' }, `${guidanceTime(entry.ends_at)}까지`) : null,
    ),
  );
}

// ADR-0033 §5: one placement's group. The resolved list is the server's for the saved choice; a
// changed choice applies once the preparation is saved.
function guidanceGroup(unit, placement, read, settings, editable) {
  const saved = read.placements.find((item) => item.placement === placement.key);
  const savedChoice = saved?.choice ?? { mode: 'DEFAULT' };
  let mode = savedChoice.mode;
  let editor = null;
  const customHost = h('div', { class: 'dg-unit-custom', 'data-role': 'dg-custom' });
  const changeNote = h('div', { class: 'mini', 'data-role': 'dg-mode-note' });
  const buttons = GUIDANCE_MODES.map(([key, label, copy]) => {
    const button = h('button', { type: 'button', class: 'btn dg-small', role: 'radio', 'data-mode': key, title: copy }, label);
    button.disabled = !editable;
    button.addEventListener('click', () => {
      mode = key;
      draw();
    });
    return button;
  });
  const reasons = (unit.preflight?.reasons ?? []).filter(
    (item) => item.code === GUIDANCE_UNPLACED && item.subject === `guidance:${placement.key.toLowerCase()}`,
  );
  const unplaced = reasons.length || (!unit.preflight && saved?.unplaced);
  const entries = saved?.entries ?? [];

  function draw() {
    buttons.forEach((button) => button.setAttribute('aria-checked', String(button.dataset.mode === mode)));
    changeNote.textContent =
      mode === savedChoice.mode
        ? GUIDANCE_MODES.find(([key]) => key === mode)?.[2] ?? ''
        : `${GUIDANCE_MODES.find(([key]) => key === mode)?.[1]}: 준비 내용을 저장하면 적용됩니다.`;
    if (mode !== 'CUSTOM' || !editable || !settings) {
      customHost.replaceChildren();
      return;
    }
    if (!editor) {
      const own = savedChoice.mode === 'CUSTOM';
      editor = composer({
        blocks: own ? savedChoice.content?.blocks : OPENING[placement.key],
        template: own ? savedChoice.template : null,
        templates: settings.templates,
        saved: own,
      });
    }
    customHost.replaceChildren(
      presetBar(settings, placement.key, editor),
      exampleNote('준비 내용을 저장하기 전에는 이 상품에 들어가지 않습니다.'),
      editor.el,
    );
  }
  draw();

  const element = h(
    'div',
    { class: 'detail-group dg-unit', 'data-role': `editor-guidance-${placement.key.toLowerCase()}`, 'data-placement': placement.key, 'data-saved-mode': savedChoice.mode },
    h(
      'div',
      { class: 'dg-unit-head' },
      h('h4', {}, placement.label),
      h('span', { class: 'mini' }, placement.where),
      h('div', { class: 'dg-modes', role: 'radiogroup', 'aria-label': `${placement.label} 선택`, 'data-role': 'dg-modes' }, ...buttons),
    ),
    changeNote,
    h(
      'div',
      { class: 'dg-resolved', 'data-role': 'dg-resolved', 'data-count': String(entries.length) },
      entries.length
        ? entries.map((entry) => resolvedEntry(entry, settings, placement.label))
        : h('span', { class: 'mini', 'data-role': 'dg-none' }, '이 상품에 들어가는 공지가 없습니다.'),
    ),
    unplaced
      ? h('div', { class: 'note', 'data-role': 'dg-unplaced', 'data-reason': GUIDANCE_UNPLACED }, REASON_COPY[GUIDANCE_UNPLACED] ?? GUIDANCE_UNPLACED)
      : null,
    customHost,
  );
  return {
    element,
    // What the save sends for this placement. A product's own notice needs its design chosen; the
    // server validates its text and draws it.
    value() {
      if (mode !== 'CUSTOM') return { mode };
      if (!editor) return savedChoice.mode === 'CUSTOM' ? savedChoice : { mode: 'CUSTOM' };
      if (editor.empty()) throw new Error(`${placement.label}: 직접 작성할 공지 문구를 입력하세요.`);
      if (!editor.template()) throw new Error(`${placement.label}: 직접 작성한 공지의 템플릿을 고르세요.`);
      return { mode: 'CUSTOM', template: editor.template(), content: editor.content() };
    },
  };
}

// ADR-0033 §5 (G4): the 상단 공지 and 하단 공지 groups of the 상세페이지 step. Each reads the server's
// resolution for the unit; the authoring form saves the choice with the preparation.
function guidanceGroups(unit, form, editable) {
  const hosts = Object.fromEntries(
    PLACEMENTS.map((placement) => [
      placement.key,
      h(
        'div',
        { class: 'detail-group dg-unit', 'data-role': `editor-guidance-${placement.key.toLowerCase()}`, 'data-state': 'loading' },
        h('h4', {}, placement.label),
        h('span', { class: 'chip' }, '불러오는 중'),
      ),
    ]),
  );
  const preparationId = unit.authored?.preparation_id;
  const read = getJson(preparationId ? `${GUIDANCE_READ}?preparation_id=${encodeURIComponent(preparationId)}` : GUIDANCE_READ);
  Promise.all([read, getJson(GUIDANCE_BASE).catch(() => null)])
    .then(([resolved, settings]) => {
      const groups = PLACEMENTS.map((placement) => {
        const group = guidanceGroup(unit, placement, resolved, settings, editable && Boolean(form));
        group.element.dataset.state = 'ready';
        hosts[placement.key].replaceWith(group.element);
        hosts[placement.key] = group.element;
        return [placement.key, group];
      });
      if (form) {
        const byKey = Object.fromEntries(groups);
        form.guidanceValue = () => ({ top: byKey.TOP.value(), bottom: byKey.BOTTOM.value() });
      }
    })
    .catch((error) => {
      for (const placement of PLACEMENTS) {
        const host = hosts[placement.key];
        host.dataset.state = 'error';
        host.replaceChildren(h('h4', {}, placement.label), h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
      }
    });
  return hosts;
}

function pane(index, title, copy, ...children) {
  return h(
    'section',
    { class: 'editor-pane', 'data-pane': String(index) },
    h('div', { class: 'panel' }, h('h3', { class: 'panel-title' }, title, copy ? h('span', { class: 'mini' }, copy) : null), ...children),
  );
}

function previewModal(unit) {
  const block = previewBlock(unit);
  if (!block) {
    openModal({ eyebrow: '미리보기', title: unit.unit_ref, body: h('div', { class: 'note' }, '스냅샷을 고정한 뒤 마켓 미리보기를 볼 수 있습니다.') });
    return;
  }
  openModal({ eyebrow: '미리보기', title: unit.unit_ref, help: '고정한 스냅샷이 마켓에 보낼 내용을 서버가 그대로 보여 줍니다.', body: block });
  block.querySelector('[data-action="PREVIEW_SNAPSHOT"]')?.click();
}

function editor(unit, ctx, labels, mode, step) {
  const reload = () => ctx.navigate('register-editor', { draft: unit.draft_id, unit: unit.unit_ref, items: unitItemsKey(unit), mode, step: String(current) });
  let current = Math.min(Math.max(Number(step) || 1, 1), STEPS.length);
  const states = stepStates(unit);
  const editable = !unit.snapshot && !unit.intent;
  // One authoring form for the whole unit: its parts sit in their steps and stay bound to it. The
  // save reads the name and the body through the form's FormData, and the category, the category
  // fields and the options through the form's own references to those controls, wherever they are
  // placed. Every control of every part is still associated with the form (the `form` attribute),
  // including the fields the category's requirements add later, so the form owns all of them.
  const form = editable ? authoringForm(unit, reload) : null;
  if (form) {
    form.id = `authoring-${unit.unit_ref}`;
    form.classList.add('editor-form');
    const associate = (root) => {
      for (const control of root.querySelectorAll('input, textarea, select, button[type="submit"]')) {
        control.setAttribute('form', form.id);
      }
    };
    for (const part of [form.parts.category, form.parts.name, form.parts.fields, form.parts.options, form.parts.detail]) {
      associate(part);
      new MutationObserver(() => associate(part)).observe(part, { childList: true, subtree: true });
    }
    form.parts.submit.setAttribute('form', form.id);
  }
  const parts = form?.parts;
  const guidance = guidanceGroups(unit, form, editable);
  const stepper = h('nav', { class: 'stepper', 'aria-label': '편집 단계' });
  const panes = [
    pane(
      1,
      '① 상품 기본정보',
      '플랫폼마다 다른 값은 준비 중 · 지금은 대상 플랫폼 하나',
      h('div', { class: 'hero' }, mainImage(unit), h('div', { class: 'stack' }, h('div', { class: 'row' }, pending('수집 이미지에서 선택'), pending('업로드'), pending('이미지 편집')), h('span', { class: 'mini' }, '메인 이미지는 대표이미지 단계의 대표 이미지입니다.'))),
      platformTabs(unit),
      ...(parts ? [parts.head, parts.category, aiCategoryBlock(unit, reload), parts.name, aiNameBlock(unit, reload)] : readOnlyInputs(unit)),
      tagsBlock(unit, reload),
    ),
    pane(
      2,
      '② 상품 대표이미지',
      '대표 · 추가 · 상세 · 사용 안 함을 이미지마다 정합니다',
      h(
        'div',
        { class: 'bulk', 'data-role': 'image-bulk' },
        h('span', { class: 'count' }, '선택한 이미지에'),
        aiPending('배경 이미지 제거'),
        pending('리사이즈', '리사이즈는 준비 중'),
        pending('이미지 편집', '이미지 편집은 준비 중'),
        aiPending('이미지 자동번역'),
      ),
      editable ? null : h('div', { class: 'note' }, '등록 요청이나 스냅샷이 있어 이미지는 보기만 합니다.'),
      ...unit.items.map((item) =>
        h('div', { class: 'register-item-images', 'data-item': item.item_id }, h('b', {}, `품목 ${item.ordinal}`), itemImagesEditor(item.item_id, { editable, onChanged: reload })),
      ),
    ),
    pane(
      3,
      '③ 옵션 · 가격',
      '판매가는 M4가 매긴 고정 가격입니다',
      h('div', { class: 'calc' }, ...['도매가', '공급처 배송비', '환율', '원가 합계', '최저판매가', '가격 기준', '상품에 포함할 배송비'].map((label) => h('div', { class: 'box', 'data-no-data': label }, h('span', { class: 'mini' }, label), h('b', { class: 'no-data' }, NO_DATA)))),
      repinBlock(unit, reload),
      unit.item_facts_unavailable_reason ? reason(unit.item_facts_unavailable_reason) : null,
      table(['품목', '고정 판매가', '가격 근거', '현재 M4 판매가', '기본 준비', '가격 준비', '등록 품목 키', '이미지'], unit.items.map(itemRow)),
      parts ? parts.options : null,
      h('div', { class: 'row' }, pending('＋ 옵션 축 추가', '옵션 편집은 준비 중'), pending('옵션별 이미지', '옵션별 이미지는 준비 중'), pending('일괄 가격 변경', '일괄 가격 변경은 준비 중')),
    ),
    pane(
      4,
      '④ 상품정보고시',
      '카테고리가 정한 고시 항목과 속성',
      unit.category ? categoryBlock(unit.category) : noData('카테고리 요구사항'),
      parts ? parts.fields : null,
    ),
    pane(
      5,
      '⑤ 상세페이지',
      '상단 공지 → 상세 이미지 → 본문 → 하단 공지',
      h('div', { class: 'mode-switch' }, h('button', { type: 'button', class: 'mode-card on' }, h('b', {}, '이미지 에디터 방식'), h('span', { class: 'mini' }, '상세 이미지는 대표이미지 단계에서 「상세」로 고릅니다')), markInert(h('button', { type: 'button', class: 'mode-card' }, h('b', {}, 'HTML 방식'), h('span', { class: 'mini' }, '준비 중')), 'HTML 방식은 준비 중')),
      guidance.TOP,
      parts ? parts.detail : h('div', { class: 'kv' }, h('span', {}, '상세 본문'), h('b', {}, unit.authored?.inputs?.detail_body ?? '—')),
      h('div', { class: 'row' }, pending('이미지 편집', '상세 이미지 편집은 준비 중'), aiPending('이미지 자동번역'), h('button', { type: 'button', class: 'btn', 'data-action': 'open-preview', onclick: () => previewModal(unit) }, '미리보기')),
      guidance.BOTTOM,
    ),
    pane(
      6,
      '⑥ 기타 등록정보',
      '배송·반품·A/S는 등록 정책에서 정합니다',
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', onclick: () => ctx.navigate('settings') }, '설정 › 등록 정책 열기')),
      preflightBlock(unit, labels),
      unit.intent && unit.intent.attempts.length ? table(['시도', '결과', '오류 분류', '오류 코드', '시작'], unit.intent.attempts.map(attemptRow)) : null,
      scopeBlock(unit.scope),
      h('div', { class: 'supplier-actions', 'data-role': 'editor-actions' }, ...unit.actions.map((action) => actionCell(unit, action, reload))),
    ),
  ];
  const main = h('main', { class: 'editor-main' }, form, ...panes, h('div', { class: 'row editor-nav' }, h('button', { type: 'button', class: 'btn', onclick: () => go(current - 1) }, '← 이전'), h('span', { class: 'spacer' }), h('button', { type: 'button', class: 'btn blue', onclick: () => go(current + 1) }, '다음 →')));

  function renderStepper() {
    stepper.replaceChildren(
      ...STEPS.map(([key, label, copy], index) =>
        h(
          'button',
          { type: 'button', class: index + 1 === current ? 'step on' : 'step', 'data-step': key, 'aria-current': index + 1 === current ? 'step' : null, onclick: () => go(index + 1) },
          h('span', { class: 'no' }, String(index + 1)),
          h('span', {}, h('b', {}, label), h('small', {}, copy)),
          h('span', { class: `dot ${states[index].state}`, 'aria-label': STATE_LABEL[states[index].state] }),
        ),
      ),
    );
  }

  function go(n) {
    current = Math.min(Math.max(n, 1), STEPS.length);
    // Only the current step is in the document's flow; the others are not shown.
    panes.forEach((node, index) => node.toggleAttribute('hidden', index + 1 !== current));
    renderStepper();
    window.scrollTo(0, 0);
  }

  const enabled = unit.actions.filter((action) => action.enabled);
  const top = h(
    'header',
    { class: 'editor-top' },
    h('span', { class: 'title' }, '상품 편집기'),
    h('span', { class: `chip ${mode === 'edit' ? 'good' : 'info'}`, 'data-role': 'editor-mode' }, mode === 'edit' ? '상품수정' : '상품등록'),
    h('b', { class: 'editor-unit' }, unit.unit_ref),
    platformTag(unit.marketplace_key),
    chip(PREPARATION_LABEL[unit.preparation] ?? unit.preparation),
    unit.intent ? readStateChip(unit.intent.read_state, unit.intent.read_state_problem) : null,
    h('span', { class: 'spacer' }),
    h('button', { type: 'button', class: 'btn', 'data-action': 'open-preview', onclick: () => previewModal(unit) }, '미리보기'),
    h('button', { type: 'button', class: 'btn', onclick: () => ctx.navigate('register') }, '등록관리'),
  );
  const foot = h(
    'footer',
    { class: 'editor-foot' },
    h('span', { class: 'ready-label' }, '등록 준비'),
    platformTag(unit.marketplace_key),
    readyStrip(unit, states, go),
    h('span', { class: 'spacer' }),
    parts ? parts.submit : null,
    ...enabled.map((action) => actionCell(unit, action, reload)),
  );
  const notice = mode === 'edit' ? h('div', { class: 'note editor-banner', 'data-role': 'edit-banner' }, '등록된 상품입니다. 마켓에 수정 반영은 준비 중이라 지금은 내용 확인만 합니다.') : null;
  const root = h(
    'div',
    { class: 'register-editor', 'data-role': 'register-editor', 'data-unit': unit.unit_ref, 'data-draft': unit.draft_id, 'data-mode': mode },
    top,
    notice,
    h('div', { class: 'editor-body' }, stepper, main),
    foot,
  );
  go(current);
  return root;
}

export default {
  key: 'register-editor',
  title: '상품 편집기',
  navLabel: '상품 편집기',
  icon: '✎',
  // Opened in its own tab: the application chrome is not drawn around it.
  bare: true,
  async render(ctx) {
    const draft = ctx.params.get('draft');
    const unitRef = ctx.params.get('unit');
    const mode = ctx.params.get('mode') === 'edit' ? 'edit' : 'register';
    if (!draft || !unitRef) {
      return emptyState({ title: '편집할 상품이 없습니다', copy: '등록관리에서 상품을 눌러 편집기를 여세요.', action: { label: '등록관리', onSelect: () => ctx.navigate('register') } });
    }
    const [units, readiness] = await Promise.all([
      getJson(`/api/v1/register/units/${encodeURIComponent(draft)}`),
      getJson(READINESS).catch(() => null),
    ]);
    // The ref moves when the unit is authored or frozen; then the one unit holding the same Items
    // is the same unit. Two units holding them (separate frozen listings) are never guessed between.
    let unit = units.find((candidate) => candidate.unit_ref === unitRef);
    const items = ctx.params.get('items');
    if (!unit && items) {
      const same = units.filter((candidate) => unitItemsKey(candidate) === items);
      if (same.length === 1) {
        [unit] = same;
        const params = new URLSearchParams(ctx.params);
        params.set('unit', unit.unit_ref);
        window.history.replaceState(null, '', `#/register-editor?${params}`);
      }
    }
    if (!unit) {
      return emptyState({ title: '이 등록 단위를 찾을 수 없습니다', copy: '등록관리에서 다시 열어 주세요.', action: { label: '등록관리', onSelect: () => ctx.navigate('register') } });
    }
    const labels = Object.fromEntries((readiness?.areas ?? []).map((area) => [area.area, area.label]));
    document.title = `${mode === 'edit' ? '상품수정' : '상품등록'} · ${unit.unit_ref} · ICBM`;
    closeModal();
    return fragment(editor(unit, ctx, labels, mode, ctx.params.get('step')));
  },
};

