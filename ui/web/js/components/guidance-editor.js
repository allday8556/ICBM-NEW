// 상세페이지 공지 입력 (ADR-0033 §4, §5, §11): the shared notice editor of the settings card
// (G3, 설정 › 공통 › 상세페이지 공지) and of the product editor's 상세페이지 step (G4, 직접 작성).
//
// It validates, renders and judges nothing: the server validates the plain text (1–3 blocks, a
// heading of at most 20 characters, 1–6 lines of at most 40, no URL or markup) and renders the
// five-template preview with its one renderer. The input limits below only mirror those rules as
// `maxlength` and add/remove bounds. A preset only fills the inputs with the server's example
// text, asking first when they were edited; choosing a template only picks a design and never
// changes the text.

import { ApiError, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';

export const GUIDANCE_BASE = '/api/v1/settings/detail-guidance';
const PREVIEW_DELAY_MS = 400;
// ADR-0033 §1, mirrored as input bounds only; the server validates every save and preview.
const BLOCKS_MAX = 3;
const LINES_MAX = 6;
const HEADING_MAX = 20;
const LINE_MAX = 40;
export const PLACEHOLDER = '○○';

export const PLACEMENTS = [
  { key: 'TOP', role: 'dg-top', label: '상단 공지', where: '상세 이미지 위' },
  { key: 'BOTTOM', role: 'dg-bottom', label: '하단 공지', where: '본문 아래' },
];
// What a placement with no notice opens with (ADR-0033 §4): editable defaults, saved only when
// the operator saves them.
export const OPENING = {
  TOP: [{ heading: '', lines: [''] }],
  BOTTOM: [
    { heading: '배송 안내', lines: [''] },
    { heading: 'C/S 안내', lines: [''] },
  ],
};

const ERROR_COPY = {
  GUIDANCE_CURRENT_MOVED: '다른 곳에서 이 공지가 먼저 바뀌었습니다. 다시 불러온 뒤 저장하세요.',
  GUIDANCE_UNCHANGED: '현재 저장된 공지와 같아 바뀐 것이 없습니다. 저장하지 않았습니다.',
  GUIDANCE_PERIOD_INVALID: '기간을 확인하세요. 시작과 끝을 모두 입력하고, 시작이 끝보다 앞서야 합니다.',
  GUIDANCE_TEXT_TOO_WIDE: '템플릿의 글상자보다 넓은 줄이 있습니다. 줄을 줄이거나 두 줄로 나누세요.',
  GUIDANCE_TEXT_INVALID: '쓸 수 없는 공지 문구입니다.',
  GUIDANCE_UNKNOWN: '이 공지를 찾을 수 없습니다. 다시 불러오세요.',
  GUIDANCE_TEMPLATE_UNKNOWN: '알 수 없는 템플릿입니다. 템플릿을 다시 고르세요.',
  REGISTER_GUIDANCE_CHOICE_INVALID: '직접 작성한 공지는 템플릿과 문구가 모두 있어야 합니다.',
};

export function clone(blocks) {
  return (blocks ?? []).map((block) => ({ heading: block.heading ?? '', lines: [...(block.lines ?? [''])] }));
}

// `blocks[0].lines[1]` → `블록 1 · 2번째 줄`: the server's location, shown in words.
function whereText(where) {
  const match = /^blocks\[(\d+)\](?:\.(heading)|\.lines(?:\[(\d+)\])?)?$/.exec(where ?? '');
  if (!match) return null;
  const block = `블록 ${Number(match[1]) + 1}`;
  if (match[2]) return `${block} · 제목`;
  if (match[3] !== undefined) return `${block} · ${Number(match[3]) + 1}번째 줄`;
  return block;
}

// Why the server refused the text, in words; the server's own message and code are shown too.
function reasonText(error) {
  const body = error?.error;
  const details = body?.details ?? {};
  const message = body?.message ?? '';
  if (body?.code !== 'GUIDANCE_TEXT_INVALID') return ERROR_COPY[body?.code] ?? null;
  if (details.limit !== undefined) return `글자 수가 너무 많습니다 (${details.length}자 · 최대 ${details.limit}자).`;
  if (details.codepoint) return `그릴 수 없는 문자가 있습니다 (${details.codepoint}).`;
  if (message.includes('URL or markup')) return 'URL·웹 주소나 태그 같은 마크업은 공지에 쓸 수 없습니다.';
  if (message.includes('non-empty lines')) return '빈 줄이 있습니다. 채우거나 지우세요 (블록마다 1~6줄).';
  return ERROR_COPY.GUIDANCE_TEXT_INVALID;
}

export function serverLine(error) {
  const body = error?.error;
  if (body?.message) return `${body.message} (${body.code})`;
  return String(error?.message ?? error);
}

export function errorText(error) {
  const reason = error instanceof ApiError ? reasonText(error) : null;
  const where = whereText(error?.error?.details?.where);
  const text = reason ?? serverLine(error);
  return where ? `${where}: ${text}` : text;
}

export function labelOf(settings, template) {
  return settings.templates.find((item) => item.name === template)?.label ?? template;
}

// The text inputs of one notice, the five-template live preview and the template choice. The
// preview is the server's: every change asks it again (debounced), and only an answer for the
// text as it is now makes the notice ready to save.
export function composer({ blocks, template, templates, saved = false }) {
  const state = {
    blocks: clone(blocks),
    template: template ?? null,
    dirty: false,
    generation: 0,
    validFor: -1,
    images: new Map(),
    timer: null,
  };
  const listeners = [];
  const notify = () => listeners.forEach((listener) => listener());

  const editor = h('div', { class: 'dg-editor', 'data-role': 'dg-editor' });
  const error = h('div', { class: 'dg-error', 'data-role': 'dg-error', role: 'alert' });
  error.hidden = true;
  const placeholderNote = h(
    'div',
    { class: 'note dg-placeholder-note', 'data-role': 'dg-placeholder' },
    `${PLACEHOLDER} 자리는 실제 값으로 바꾼 뒤 저장하세요.`,
  );
  placeholderNote.hidden = true;
  const previewState = h('span', { class: 'mini', 'data-role': 'dg-preview-state' });
  const tiles = h('div', { class: 'dg-preview', 'data-role': 'dg-preview', role: 'radiogroup', 'aria-label': '템플릿 선택' });
  const choice = h('div', { class: 'mini', 'data-role': 'dg-template-choice' });
  const large = h('div', { class: 'dg-large', 'data-role': 'dg-preview-large' });

  const contentOf = () => ({
    blocks: state.blocks.map((block) => ({ heading: block.heading, lines: [...block.lines] })),
  });
  const empty = () => state.blocks.every((block) => !block.heading.trim() && block.lines.every((line) => !line.trim()));

  const showError = (failure) => {
    editor.querySelectorAll('[aria-invalid]').forEach((input) => input.removeAttribute('aria-invalid'));
    if (!failure) {
      error.hidden = true;
      error.replaceChildren();
      return;
    }
    error.hidden = false;
    error.replaceChildren(h('b', {}, errorText(failure)), h('span', { class: 'mini' }, `서버: ${serverLine(failure)}`));
    const match = /^blocks\[(\d+)\](?:\.(heading)|\.lines\[(\d+)\])?/.exec(failure?.error?.details?.where ?? '');
    if (!match) return;
    const selector = match[2]
      ? `[data-role='dg-heading'][data-block='${match[1]}']`
      : match[3] !== undefined
        ? `[data-role='dg-line'][data-block='${match[1]}'][data-line='${match[3]}']`
        : null;
    if (selector) editor.querySelector(selector)?.setAttribute('aria-invalid', 'true');
  };

  const drawTemplates = () => {
    tiles.replaceChildren(
      ...templates.map((item) => {
        const image = state.images.get(item.name);
        const tile = h(
          'button',
          {
            type: 'button',
            class: 'dg-template',
            role: 'radio',
            'aria-checked': String(state.template === item.name),
            'data-role': 'dg-template',
            'data-template': item.name,
          },
          image
            ? h('img', { src: `data:image/png;base64,${image.png_base64}`, alt: `${item.label} 템플릿 미리보기`, 'data-sha256': image.sha256 })
            : h('span', { class: 'dg-empty-image' }, '미리보기 없음'),
          h('b', {}, item.label),
        );
        // Choosing a template picks a design only; the text stays exactly as typed (ADR-0033 §11).
        tile.addEventListener('click', () => {
          state.template = item.name;
          drawTemplates();
          notify();
        });
        return tile;
      }),
    );
    const chosen = templates.find((item) => item.name === state.template);
    choice.textContent = chosen ? `선택한 템플릿: ${chosen.label}` : '저장할 템플릿을 고르세요';
    const image = chosen ? state.images.get(chosen.name) : null;
    large.replaceChildren(
      ...(image
        ? [h('img', { src: `data:image/png;base64,${image.png_base64}`, alt: `${chosen.label} 템플릿으로 그린 공지` })]
        : []),
    );
  };

  const runPreview = async () => {
    const generation = state.generation;
    previewState.textContent = '미리보기를 만드는 중…';
    try {
      const images = await sendJson('POST', `${GUIDANCE_BASE}/preview`, { content: contentOf() });
      if (generation !== state.generation) return;
      state.images = new Map(images.map((image) => [image.template, image]));
      state.validFor = generation;
      showError(null);
      previewState.textContent = `템플릿 ${images.length}개 미리보기`;
    } catch (failure) {
      if (generation !== state.generation) return;
      state.images = new Map();
      state.validFor = -1;
      showError(failure);
      previewState.textContent = '미리보기를 만들 수 없습니다';
    }
    drawTemplates();
    notify();
  };

  const changed = ({ edited, delay = PREVIEW_DELAY_MS, preview = true }) => {
    if (edited) state.dirty = true;
    state.generation += 1;
    placeholderNote.hidden = !state.blocks.some(
      (block) => block.heading.includes(PLACEHOLDER) || block.lines.some((line) => line.includes(PLACEHOLDER)),
    );
    window.clearTimeout(state.timer);
    if (empty() || !preview) {
      state.images = new Map();
      showError(null);
      previewState.textContent = '문구를 입력하면 다섯 템플릿 미리보기가 나타납니다';
      drawTemplates();
    } else {
      state.timer = window.setTimeout(runPreview, delay);
    }
    notify();
  };

  const input = (attrs, value, onInput) => {
    const el = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs, value });
    el.addEventListener('input', () => {
      onInput(el.value);
      changed({ edited: true });
    });
    return el;
  };

  const action = (label, attrs, enabled, onClick) => {
    const el = h('button', { type: 'button', class: 'btn dg-small', ...attrs }, label);
    el.disabled = !enabled;
    el.addEventListener('click', () => {
      onClick();
      drawEditor();
      changed({ edited: true });
    });
    return el;
  };

  const drawEditor = () => {
    editor.replaceChildren(
      ...state.blocks.map((block, b) =>
        h(
          'fieldset',
          { class: 'dg-block', 'data-role': 'dg-block', 'data-block': String(b) },
          h('legend', {}, `블록 ${b + 1}`),
          input(
            {
              class: 'dg-heading',
              maxlength: String(HEADING_MAX),
              placeholder: `제목 (선택 · 최대 ${HEADING_MAX}자)`,
              'aria-label': `블록 ${b + 1} 제목`,
              'data-role': 'dg-heading',
              'data-block': String(b),
            },
            block.heading,
            (value) => {
              block.heading = value;
            },
          ),
          block.lines.map((line, i) =>
            h(
              'div',
              { class: 'dg-line-row' },
              input(
                {
                  maxlength: String(LINE_MAX),
                  placeholder: `공지 문구 (최대 ${LINE_MAX}자)`,
                  'aria-label': `블록 ${b + 1} ${i + 1}번째 줄`,
                  'data-role': 'dg-line',
                  'data-block': String(b),
                  'data-line': String(i),
                },
                line,
                (value) => {
                  block.lines[i] = value;
                },
              ),
              action('－', { 'aria-label': `블록 ${b + 1} ${i + 1}번째 줄 지우기`, 'data-action': 'dg-line-remove' }, block.lines.length > 1, () =>
                block.lines.splice(i, 1),
              ),
            ),
          ),
          h(
            'div',
            { class: 'dg-row-actions' },
            action('＋ 줄 추가', { 'data-action': 'dg-line-add' }, block.lines.length < LINES_MAX, () => block.lines.push('')),
            action('블록 삭제', { 'data-action': 'dg-block-remove' }, state.blocks.length > 1, () => state.blocks.splice(b, 1)),
          ),
        ),
      ),
      action('＋ 블록 추가', { 'data-action': 'dg-block-add' }, state.blocks.length < BLOCKS_MAX, () =>
        state.blocks.push({ heading: '', lines: [''] }),
      ),
    );
  };

  drawEditor();
  drawTemplates();
  // Saved text is previewed at once; the opening defaults wait for the operator's first change.
  changed({ edited: false, delay: 0, preview: saved });

  return {
    el: h(
      'div',
      { class: 'dg-composer' },
      h('div', { class: 'dg-editor-pane' }, editor, error, placeholderNote),
      h(
        'div',
        { class: 'dg-preview-pane' },
        h('div', { class: 'dg-preview-head' }, h('b', {}, '템플릿 미리보기'), previewState),
        tiles,
        choice,
        large,
      ),
    ),
    content: contentOf,
    template: () => state.template,
    dirty: () => state.dirty,
    empty,
    // Ready to save: a template is chosen and the server previewed the text exactly as it is now.
    ready: () => state.template !== null && state.validFor === state.generation && !empty(),
    // A preset's text replaces the inputs; the preset's template is only a suggestion.
    fill(blocks, suggested) {
      state.blocks = clone(blocks);
      state.template = suggested ?? state.template;
      state.dirty = false;
      drawEditor();
      drawTemplates();
      changed({ edited: false, delay: 0 });
    },
    onChange(listener) {
      listeners.push(listener);
    },
  };
}

// The `국내배송` / `해외배송` preset buttons of one placement (ADR-0033 §11). A preset replaces the
// inputs with the server's example text, asking first when they were edited since they were last
// filled or saved; its template is only a suggestion.
export function presetBar(settings, placementKey, editor) {
  const note = h('span', { class: 'mini', 'data-role': 'dg-preset-state' });
  const buttons = settings.presets.map((preset) => {
    const button = h('button', { type: 'button', class: 'btn', 'data-role': 'dg-preset', 'data-preset': preset.key }, preset.label);
    button.addEventListener('click', () => {
      const replace =
        !editor.dirty() || window.confirm(`입력한 문구를 「${preset.label}」 예시 문구로 바꿀까요?\n지금 입력한 내용은 사라집니다.`);
      if (!replace) return;
      editor.fill(placementKey === 'TOP' ? preset.top.blocks : preset.bottom.blocks, preset.template);
      note.textContent = `「${preset.label}」 예시 문구를 채웠습니다 · 추천 템플릿: ${labelOf(settings, preset.template)}`;
    });
    return button;
  });
  return h('div', { class: 'dg-presets' }, h('span', { class: 'mini' }, '예시 문구 채우기'), buttons, note);
}

// The example-text caution under the presets (ADR-0033 §11, DG-08): what reaches a product is
// only the operator's own saved text.
export function exampleNote(until) {
  return h(
    'div',
    { class: 'note dg-preset-note', 'data-role': 'dg-preset-note' },
    h('b', {}, '예시 문구입니다. '),
    `판매자의 실제 배송·교환·반품 조건과 맞는지 확인하고, ${PLACEHOLDER} 자리는 실제 값으로 바꾼 뒤 저장하세요. ${until}`,
  );
}
