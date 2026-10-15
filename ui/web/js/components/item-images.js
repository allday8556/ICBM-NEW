// B-EDITOR: one Item's image choices inside the registration workspace.
//
// The image owner (ProductImageService) decides; this component only shows the CONFIRMED source
// images of the Item's current bound revision and hands the operator's complete decision to the
// owner's own routes. Every image is decided explicitly (대표 / 추가 / 상세 / 사용 안 함), nothing is
// pre-selected for an operator, and a selection the rule made is shown as the rule's. The
// auto-selection rule never moves an operator's selection; the server says so in its answer.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { toast } from '../core/toast.js';

const OPERATOR = 'operator';
const ROLE_CHOICES = [
  ['', '선택하세요'],
  ['REPRESENTATIVE', '대표 이미지'],
  ['ADDITIONAL', '추가 이미지'],
  ['DETAIL', '상세 이미지'],
  ['EXCLUDE', '사용 안 함'],
];
const SOURCE_ROLE_LABEL = { REPRESENTATIVE: '원본 대표', DETAIL: '원본 상세' };
// The automatic image check's verdict, in a seller's words (the verdict stays `data-qa`).
const QA_LABEL = {
  PASS: ['이미지 검사 통과', 'good'],
  REVIEW_REQUIRED: ['이미지 확인 필요', 'warn'],
  FAIL: ['이미지 검사 실패', 'bad'],
};
const AUTO_STATUS_COPY = {
  SELECTED: '자동 선택 규칙으로 새로 골랐습니다.',
  UNCHANGED: '자동 선택 결과가 이미 현재 선택과 같습니다.',
  OPERATOR_HELD: '운영자가 직접 고른 선택이 있어 자동 선택은 바꾸지 않았습니다.',
  BLOCKED: '자동 선택 규칙이 고르지 못했습니다.',
};
// Why the rule could not choose (app/stages/products/auto_images.py AutoSelectionBlocked).
const AUTO_BLOCKED_COPY = {
  NO_BOUND_REVISION: '수집한 원본 정보가 아직 확정되지 않았습니다.',
  ROLE_RULES_UNAVAILABLE: '이 공급처의 이미지 구분 규칙이 아직 없습니다.',
  ROLE_RULE_UNKNOWN: '이 공급처의 이미지 구분 규칙을 알 수 없습니다.',
  REPRESENTATIVE_MISSING: '대표로 쓸 원본 이미지가 없습니다.',
};
// The image auto-selection rule records its selections under its own version name.
const AUTO_ACTOR = 'image-auto-selection/';
// The order a saved selection places its outputs: the representative first, then the gallery,
// then the detail body, each in the source page order.
const OUTPUT_ORDER = ['REPRESENTATIVE', 'ADDITIONAL', 'DETAIL'];

// A refusal is never shown as the server's English message or its code (2026-10-15).
function errorText(error) {
  return /[가-힣]/.test(error?.message ?? '') && !(error instanceof ApiError) ? error.message : '요청을 처리하지 못했습니다. 잠시 뒤 다시 시도하고, 계속되면 화면을 새로 고쳐 주세요.';
}

function key(role, ordinal) {
  return `${role}:${ordinal}`;
}

// What the current selection says for each source image: its output role, or EXCLUDE.
function currentChoices(selection) {
  const chosen = new Map();
  if (!selection) return chosen;
  for (const decision of selection.decisions) {
    if (decision.decision === 'EXCLUDE') chosen.set(key(decision.role, decision.ordinal), 'EXCLUDE');
  }
  for (const output of selection.outputs) {
    chosen.set(key(output.source_role, output.source_ordinal), output.role);
  }
  return chosen;
}

function imageCard(itemId, image, choice, editable) {
  const select = h(
    'select',
    { 'data-image-role': image.sha256, 'aria-label': `${SOURCE_ROLE_LABEL[image.role] ?? image.role} ${image.ordinal}` },
    ROLE_CHOICES.map(([value, label]) => h('option', { value }, label)),
  );
  select.value = choice ?? '';
  select.disabled = !editable;
  const [qaLabel, qaTone] = QA_LABEL[image.qa_verdict] ?? ['검사 전', ''];
  return {
    image,
    select,
    node: h(
      'div',
      { class: 'item-image', 'data-image': image.sha256 },
      h('img', {
        src: `/api/v1/products/items/${encodeURIComponent(itemId)}/images/${image.sha256}`,
        alt: `${SOURCE_ROLE_LABEL[image.role] ?? image.role} ${image.ordinal}`,
        loading: 'lazy',
        width: '96',
        height: '96',
      }),
      h('span', { class: 'mini' }, `${SOURCE_ROLE_LABEL[image.role] ?? image.role} ${image.ordinal}`),
      h('span', { class: `chip ${qaTone}`.trim(), 'data-qa': image.qa_verdict ?? 'NONE' }, qaLabel),
      select,
    ),
  };
}

function selectionBody(found, cards) {
  const decisions = [];
  const placed = { REPRESENTATIVE: [], ADDITIONAL: [], DETAIL: [] };
  for (const { image, select } of cards) {
    const choice = select.value;
    decisions.push({
      role: image.role,
      ordinal: image.ordinal,
      sha256: image.sha256,
      decision: choice === 'EXCLUDE' ? 'EXCLUDE' : 'USE_SOURCE',
      derivation_id: null,
    });
    if (choice && choice !== 'EXCLUDE') {
      placed[choice].push({ role: choice, source_role: image.role, source_ordinal: image.ordinal });
    }
  }
  return {
    source_revision_id: found.source_revision_id,
    decisions,
    outputs: OUTPUT_ORDER.flatMap((role) => placed[role]),
    actor: OPERATOR,
    reason: 'B-EDITOR image selection',
  };
}

function editorBody(itemId, found, editable, reload, onChanged) {
  const selection = found.current_selection;
  // A replacement (a derived image) has no editor here yet: such a selection is shown, never
  // rewritten from this screen.
  const derived = (selection?.decisions ?? []).some((d) => d.decision === 'USE_DERIVED');
  const canEdit = editable && !derived;
  const chosen = currentChoices(selection);
  const cards = found.images.map((image) => imageCard(itemId, image, chosen.get(key(image.role, image.ordinal)), canEdit));
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-image-selection' }, '이미지 선택 저장');
  save.disabled = !canEdit;
  save.addEventListener('click', async () => {
    if (cards.some(({ select }) => !select.value)) {
      toast('모든 이미지의 쓰임을 골라야 저장할 수 있습니다.');
      return;
    }
    save.disabled = true;
    try {
      await sendJson('POST', `/api/v1/products/items/${encodeURIComponent(itemId)}/image-selection`, selectionBody(found, cards));
      toast('이미지 선택을 저장했습니다.');
      await reload();
      onChanged?.();
    } catch (error) {
      toast(`이미지 선택을 저장하지 못했습니다: ${errorText(error)}`);
      save.disabled = !canEdit;
    }
  });
  const auto = h('button', { type: 'button', class: 'btn', 'data-action': 'auto-select-images' }, '자동 선택 다시 실행');
  auto.disabled = !editable;
  auto.addEventListener('click', async () => {
    auto.disabled = true;
    try {
      const result = await sendJson('POST', `/api/v1/products/items/${encodeURIComponent(itemId)}/image-auto-selection`, {});
      toast(AUTO_STATUS_COPY[result.status] ?? '자동 선택을 마쳤습니다.', AUTO_BLOCKED_COPY[result.blocked] ?? undefined);
      await reload();
      onChanged?.();
    } catch (error) {
      toast(`자동 선택을 실행하지 못했습니다: ${errorText(error)}`);
      auto.disabled = !editable;
    }
  });
  const origin = selection
    ? h(
        'span',
        { class: 'mini', 'data-selection-by': selection.decided_by, 'data-selection-revision': String(selection.revision_no) },
        String(selection.decided_by).startsWith(AUTO_ACTOR) ? '자동으로 선택됨' : '직접 선택함',
      )
    : h('span', { class: 'chip warn', 'data-selection-by': 'NONE' }, '아직 선택 없음');
  return [
    h('div', { class: 'supplier-head-row' }, origin, derived ? h('span', { class: 'chip warn' }, '대체 이미지가 있어 이 화면에서는 보기만 합니다') : null),
    h('div', { class: 'item-image-grid' }, ...cards.map((card) => card.node)),
    h('div', { class: 'api-action-row' }, auto, save),
  ];
}

export function itemImagesEditor(itemId, { editable, onChanged } = {}) {
  const host = h('div', { class: 'item-images', 'data-item-images': itemId, 'data-images-state': 'LOADING' }, h('span', { class: 'mini' }, '이미지를 불러오는 중…'));
  const reload = async () => {
    try {
      const found = await getJson(`/api/v1/products/items/${encodeURIComponent(itemId)}/image-candidates`);
      host.setAttribute('data-images-state', found.images.length ? 'READY' : 'EMPTY');
      host.replaceChildren(
        ...(found.images.length
          ? editorBody(itemId, found, editable, reload, onChanged)
          : [h('span', { class: 'mini' }, '확정된 원본 이미지가 없습니다.')]),
      );
    } catch (error) {
      host.setAttribute('data-images-state', 'UNAVAILABLE');
      host.replaceChildren(h('span', { class: 'chip warn' }, `이미지를 불러오지 못했습니다 · ${errorText(error)}`));
    }
  };
  reload();
  return host;
}
