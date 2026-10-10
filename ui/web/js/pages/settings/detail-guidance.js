// 상세페이지 공지 (ADR-0033 §4, §11; G3): the store-wide top and bottom notices of the detail page,
// drawn by the server as images from five templates.
//
// The page reads `GET /api/v1/settings/detail-guidance` and only POSTs. It validates, renders,
// numbers and judges nothing: the server validates the plain text (1–3 blocks, a heading of at
// most 20 characters, 1–6 lines of at most 40, no URL or markup), renders the five previews and
// the saved image with its one renderer, appends every revision against the sequence it was read
// at, and judges each period's status by its own clock. The input limits below only mirror those
// rules as `maxlength` and add/remove bounds. A preset only fills the inputs with the server's
// example text; choosing a template only picks a design and never changes the text. Nothing here
// reaches a product or a marketplace.

import { ApiError, getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/settings/detail-guidance';
const ACTOR = 'operator';
const TITLE = '상세페이지 공지';
const PREVIEW_DELAY_MS = 400;
// ADR-0033 §1, mirrored as input bounds only; the server validates every save and preview.
const BLOCKS_MAX = 3;
const LINES_MAX = 6;
const HEADING_MAX = 20;
const LINE_MAX = 40;
const PLACEHOLDER = '○○';

const PLACEMENTS = [
  { key: 'TOP', role: 'dg-top', label: '상단 공지', where: '상세 이미지 위' },
  { key: 'BOTTOM', role: 'dg-bottom', label: '하단 공지', where: '본문 아래' },
];
// What a placement with no standing notice opens with (ADR-0033 §4): editable defaults, saved
// only when the operator saves them.
const OPENING = {
  TOP: [{ heading: '', lines: [''] }],
  BOTTOM: [
    { heading: '배송 안내', lines: [''] },
    { heading: 'C/S 안내', lines: [''] },
  ],
};
const PERIOD_OPENING = [{ heading: '', lines: [''] }];
const STATUS_CHIP = { SCHEDULED: 'chip info', ACTIVE: 'chip good', ENDED: 'chip' };

const ERROR_COPY = {
  GUIDANCE_CURRENT_MOVED: '다른 곳에서 이 공지가 먼저 바뀌었습니다. 다시 불러온 뒤 저장하세요.',
  GUIDANCE_UNCHANGED: '현재 저장된 공지와 같아 바뀐 것이 없습니다. 저장하지 않았습니다.',
  GUIDANCE_PERIOD_INVALID: '기간을 확인하세요. 시작과 끝을 모두 입력하고, 시작이 끝보다 앞서야 합니다.',
  GUIDANCE_TEXT_TOO_WIDE: '템플릿의 글상자보다 넓은 줄이 있습니다. 줄을 줄이거나 두 줄로 나누세요.',
  GUIDANCE_TEXT_INVALID: '쓸 수 없는 공지 문구입니다.',
  GUIDANCE_UNKNOWN: '이 공지를 찾을 수 없습니다. 다시 불러오세요.',
};

function clone(blocks) {
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

function serverLine(error) {
  const body = error?.error;
  if (body?.message) return `${body.message} (${body.code})`;
  return String(error?.message ?? error);
}

function errorText(error) {
  const reason = error instanceof ApiError ? reasonText(error) : null;
  const where = whereText(error?.error?.details?.where);
  const text = reason ?? serverLine(error);
  return where ? `${where}: ${text}` : text;
}

function localTime(iso) {
  return new Date(iso).toLocaleString('ko-KR', { dateStyle: 'medium', timeStyle: 'short' });
}

// A `datetime-local` value is the operator's local wall time; it is sent as an instant with the
// browser's offset for that moment, and the server stores it as UTC (ADR-0033 §4).
function instant(value) {
  if (!value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return null;
  const pad = (n) => String(n).padStart(2, '0');
  const offset = -at.getTimezoneOffset();
  const sign = offset >= 0 ? '+' : '-';
  const zone = `${sign}${pad(Math.floor(Math.abs(offset) / 60))}:${pad(Math.abs(offset) % 60)}`;
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    `T${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}${zone}`
  );
}

function labelOf(settings, template) {
  return settings.templates.find((item) => item.name === template)?.label ?? template;
}

// The text inputs of one notice, the five-template live preview and the template choice. The
// preview is the server's: every change asks it again (debounced), and only an answer for the
// text as it is now makes the notice ready to save.
function composer({ blocks, template, templates, saved = false }) {
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
      const images = await sendJson('POST', `${BASE}/preview`, { content: contentOf() });
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

function messageLine() {
  return h('div', { class: 'dg-message', 'data-role': 'dg-message', role: 'status' });
}

// Shows what the save answered; a notice that moved since it was read offers to read it again.
function report(line, error, reload) {
  const code = error?.error?.code;
  line.className = code === 'GUIDANCE_UNCHANGED' ? 'dg-message' : 'dg-message bad';
  line.dataset.code = code ?? '';
  line.replaceChildren(errorText(error));
  if (code === 'GUIDANCE_CURRENT_MOVED') {
    const again = h('button', { type: 'button', class: 'btn dg-small', 'data-action': 'dg-reload' }, '다시 불러오기');
    again.addEventListener('click', reload);
    line.append(' ', again);
  }
}

async function append(payload) {
  return sendJson('POST', `${BASE}/revisions`, { actor: ACTOR, ...payload });
}

// The saved standing notice: its image, its template and whether it is on.
function currentView(current, settings, placement, reload) {
  if (!current) {
    return h(
      'div',
      { class: 'dg-current', 'data-role': 'dg-current', 'data-seq': '' },
      h('span', { class: 'chip', 'data-role': 'dg-enabled' }, '저장된 상시 공지 없음'),
    );
  }
  const toggle = h(
    'button',
    { type: 'button', class: 'btn dg-small', 'data-action': 'dg-toggle' },
    current.enabled ? '공지 끄기' : '공지 켜기',
  );
  const line = messageLine();
  toggle.addEventListener('click', async () => {
    toggle.disabled = true;
    try {
      // The same text and template, with `enabled` flipped: one more revision (DG-03).
      await append({
        placement: placement.key,
        kind: 'STANDING',
        guidance_id: current.guidance_id,
        expected_current_seq: current.seq,
        template: current.template,
        content: current.content,
        enabled: !current.enabled,
      });
      toast(TITLE, `${placement.label} 상시 공지를 ${current.enabled ? '껐습니다' : '켰습니다'}.`);
      await reload();
    } catch (error) {
      report(line, error, reload);
      toggle.disabled = false;
    }
  });
  const label = labelOf(settings, current.template);
  return h(
    'div',
    { class: 'dg-current', 'data-role': 'dg-current', 'data-seq': String(current.seq), 'data-enabled': String(current.enabled) },
    h('img', { src: current.image_url, alt: `저장된 ${placement.label} (${label})`, 'data-role': 'dg-current-image' }),
    h(
      'div',
      { class: 'dg-current-meta' },
      h(
        'div',
        { class: 'dg-chips' },
        h('span', { class: 'chip info', 'data-role': 'dg-current-template' }, label),
        h('span', { class: current.enabled ? 'chip good' : 'chip warn', 'data-role': 'dg-enabled' }, current.enabled ? '사용 중' : '꺼짐'),
      ),
      h('span', { class: 'mini' }, `리비전 #${current.seq} · ${current.authored_by} · ${localTime(current.authored_at)}`),
      current.enabled ? null : h('span', { class: 'mini' }, '꺼진 공지는 상품에 들어가지 않습니다.'),
      h('div', {}, toggle),
      line,
    ),
  );
}

function standingPanel(placement, view, settings, reload) {
  const current = view?.standing ?? null;
  const editor = composer({
    blocks: current ? current.content.blocks : OPENING[placement.key],
    template: current?.template ?? null,
    templates: settings.templates,
    saved: Boolean(current),
  });
  const presetNote = h('span', { class: 'mini', 'data-role': 'dg-preset-state' });
  const presets = settings.presets.map((preset) => {
    const button = h('button', { type: 'button', class: 'btn', 'data-role': 'dg-preset', 'data-preset': preset.key }, preset.label);
    button.addEventListener('click', () => {
      const replace =
        !editor.dirty() || window.confirm(`입력한 문구를 「${preset.label}」 예시 문구로 바꿀까요?\n지금 입력한 내용은 사라집니다.`);
      if (!replace) return;
      editor.fill(placement.key === 'TOP' ? preset.top.blocks : preset.bottom.blocks, preset.template);
      presetNote.textContent = `「${preset.label}」 예시 문구를 채웠습니다 · 추천 템플릿: ${labelOf(settings, preset.template)}`;
    });
    return button;
  });

  const line = messageLine();
  const save = h('button', { type: 'button', class: 'btn blue', 'data-role': 'dg-save' }, '상시 공지 저장');
  let busy = false;
  const sync = () => {
    save.disabled = busy || !editor.ready();
  };
  editor.onChange(sync);
  sync();
  save.addEventListener('click', async () => {
    busy = true;
    sync();
    try {
      const saved = await append({
        placement: placement.key,
        kind: 'STANDING',
        guidance_id: current?.guidance_id ?? null,
        expected_current_seq: current?.seq ?? null,
        template: editor.template(),
        content: editor.content(),
        // Saving the text keeps the notice on or off as it is; 공지 끄기/켜기 changes that.
        enabled: current ? current.enabled : true,
      });
      toast(TITLE, `${placement.label} 상시 공지를 저장했습니다 (리비전 #${saved.seq}).`);
      await reload();
    } catch (error) {
      report(line, error, reload);
      busy = false;
      sync();
    }
  });

  return h(
    'div',
    { class: 'dg-part', 'data-role': 'dg-standing' },
    h('div', { class: 'dg-part-head' }, h('b', {}, '상시 공지'), h('span', { class: 'mini' }, '모든 상품에 기본으로 들어가는 공지입니다.')),
    currentView(current, settings, placement, reload),
    h('div', { class: 'dg-presets' }, h('span', { class: 'mini' }, '예시 문구 채우기'), presets, presetNote),
    h(
      'div',
      { class: 'note dg-preset-note', 'data-role': 'dg-preset-note' },
      h('b', {}, '예시 문구입니다. '),
      `판매자의 실제 배송·교환·반품 조건과 맞는지 확인하고, ${PLACEHOLDER} 자리는 실제 값으로 바꾼 뒤 저장하세요. 저장하기 전에는 어떤 상품에도 들어가지 않습니다.`,
    ),
    editor.el,
    h('div', { class: 'dg-actions' }, line, save),
  );
}

function periodRow(notice, settings, placement, reload) {
  const label = labelOf(settings, notice.template);
  const line = messageLine();
  const end = notice.status === 'ENDED'
    ? null
    : h('button', { type: 'button', class: 'btn dg-small', 'data-action': 'dg-period-end' }, '조기 종료');
  end?.addEventListener('click', async () => {
    if (!window.confirm('이 기간 공지를 지금 종료할까요?')) return;
    end.disabled = true;
    try {
      // Ending early appends the same notice with `enabled = false` (ADR-0033 §4, DG-03).
      await append({
        placement: placement.key,
        kind: 'PERIOD',
        guidance_id: notice.guidance_id,
        expected_current_seq: notice.seq,
        template: notice.template,
        content: notice.content,
        enabled: false,
        starts_at: notice.starts_at,
        ends_at: notice.ends_at,
      });
      toast(TITLE, `${placement.label} 기간 공지를 종료했습니다.`);
      await reload();
    } catch (error) {
      report(line, error, reload);
      end.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'dg-period', 'data-role': 'dg-period', 'data-guidance-id': notice.guidance_id, 'data-status': notice.status ?? '' },
    h('img', { src: notice.image_url, alt: `${placement.label} 기간 공지 (${label})` }),
    h(
      'div',
      { class: 'dg-period-meta' },
      h('b', { 'data-role': 'dg-period-range' }, `${localTime(notice.starts_at)} ~ ${localTime(notice.ends_at)}`),
      h(
        'div',
        { class: 'dg-chips' },
        h('span', { class: STATUS_CHIP[notice.status] ?? 'chip', 'data-role': 'dg-status' }, notice.status_label ?? notice.status ?? ''),
        h('span', { class: 'chip' }, label),
      ),
      h('span', { class: 'mini' }, `리비전 #${notice.seq}${notice.enabled ? '' : ' · 조기 종료됨'}`),
      line,
    ),
    end,
  );
}

function periodForm(placement, settings, reload, close) {
  const editor = composer({ blocks: PERIOD_OPENING, template: null, templates: settings.templates });
  const field = (label, role) => {
    const el = h('input', { type: 'datetime-local', 'data-role': role, 'aria-label': `${label} (내 컴퓨터 시간)` });
    return { el, row: h('label', {}, h('span', {}, label), el) };
  };
  const start = field('시작', 'dg-start');
  const end = field('끝', 'dg-end');
  const line = messageLine();
  const save = h('button', { type: 'button', class: 'btn blue', 'data-role': 'dg-save' }, '기간 공지 저장');
  const cancel = h('button', { type: 'button', class: 'btn', 'data-action': 'dg-period-cancel' }, '취소');
  cancel.addEventListener('click', close);
  let busy = false;
  const sync = () => {
    save.disabled = busy || !editor.ready();
  };
  editor.onChange(sync);
  sync();
  save.addEventListener('click', async () => {
    busy = true;
    sync();
    try {
      await append({
        placement: placement.key,
        kind: 'PERIOD',
        guidance_id: null,
        expected_current_seq: null,
        template: editor.template(),
        content: editor.content(),
        enabled: true,
        starts_at: instant(start.el.value),
        ends_at: instant(end.el.value),
      });
      toast(TITLE, `${placement.label} 기간 공지를 추가했습니다.`);
      await reload();
    } catch (error) {
      report(line, error, reload);
      busy = false;
      sync();
    }
  });
  return h(
    'div',
    { class: 'dg-period-form', 'data-role': 'dg-period-form' },
    h(
      'div',
      { class: 'dg-period-times' },
      start.row,
      end.row,
      h('span', { class: 'mini' }, '내 컴퓨터 시간으로 입력합니다. 끝 시각이 되면 공지가 빠집니다.'),
    ),
    editor.el,
    h('div', { class: 'dg-actions' }, line, cancel, save),
  );
}

function periodsPanel(placement, view, settings, reload) {
  const periods = view?.periods ?? [];
  const formHost = h('div', {});
  const add = h('button', { type: 'button', class: 'btn', 'data-role': 'dg-period-add' }, '＋ 기간 공지 추가');
  const close = () => {
    formHost.replaceChildren();
    add.hidden = false;
  };
  add.addEventListener('click', () => {
    add.hidden = true;
    formHost.replaceChildren(periodForm(placement, settings, reload, close));
  });
  return h(
    'div',
    { class: 'dg-part', 'data-role': 'dg-periods' },
    h(
      'div',
      { class: 'dg-part-head' },
      h('b', {}, '기간 공지'),
      h('span', { class: 'mini' }, '휴무·명절·택배 마감처럼 시작부터 끝까지만 들어가는 공지입니다. 상태는 서버 시각으로 판단합니다.'),
    ),
    h(
      'div',
      { class: 'dg-period-list', 'data-role': 'dg-period-list', 'data-count': String(periods.length) },
      periods.length
        ? periods.map((notice) => periodRow(notice, settings, placement, reload))
        : h('span', { class: 'mini' }, '등록된 기간 공지가 없습니다.'),
    ),
    h('div', { class: 'dg-actions start' }, add),
    formHost,
  );
}

// One placement: its standing notice and its period notices, each re-read on its own after a
// save, so a save in one part never discards what is being typed in another.
function placementSection(placement, settings) {
  const standingHost = h('div', {});
  const periodsHost = h('div', {});
  const viewOf = (read) => read.placements.find((item) => item.placement === placement.key) ?? null;
  const draw = (part, read) => {
    if (part !== 'periods') {
      standingHost.replaceChildren(standingPanel(placement, viewOf(read), read, () => refresh('standing')));
    }
    if (part !== 'standing') {
      periodsHost.replaceChildren(periodsPanel(placement, viewOf(read), read, () => refresh('periods')));
    }
  };
  const refresh = async (part) => {
    try {
      draw(part, await getJson(BASE));
    } catch (error) {
      toast(TITLE, `다시 불러오지 못했습니다: ${errorText(error)}`);
    }
  };
  draw('all', settings);
  return h(
    'section',
    { class: 'dg-placement', 'data-role': placement.role, 'data-placement': placement.key },
    h('h4', {}, placement.label, h('span', { class: 'mini' }, ` · ${placement.where}`)),
    standingHost,
    periodsHost,
  );
}

export function detailGuidancePanel() {
  const host = h('div', { class: 'detail-guidance', 'data-role': 'detail-guidance' }, h('span', { class: 'chip' }, '불러오는 중'));
  getJson(BASE)
    .then((settings) => {
      host.dataset.state = 'ready';
      host.replaceChildren(...PLACEMENTS.map((placement) => placementSection(placement, settings)));
    })
    .catch((error) => {
      host.dataset.state = 'error';
      host.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
    });
  return host;
}
