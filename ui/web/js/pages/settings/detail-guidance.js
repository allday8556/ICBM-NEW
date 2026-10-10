// 상세페이지 공지 (ADR-0033 §4, §11; G3): the store-wide top and bottom notices of the detail page,
// drawn by the server as images from five templates.
//
// The page reads `GET /api/v1/settings/detail-guidance` and only POSTs. It validates, renders,
// numbers and judges nothing: the server validates the plain text (1–3 blocks, a heading of at
// most 20 characters, 1–6 lines of at most 40, no URL or markup), renders the five previews and
// the saved image with its one renderer, appends every revision against the sequence it was read
// at, and judges each period's status by its own clock. The notice inputs, the live preview and
// the presets are the shared editor `components/guidance-editor.js`, which the product editor's
// 상세페이지 step (G4) uses too: a preset only fills the inputs with the server's example text, and
// choosing a template only picks a design and never changes the text. Nothing here reaches a
// product until it is saved, and nothing here reaches a marketplace.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { toast } from '../../core/toast.js';
import {
  GUIDANCE_BASE as BASE,
  OPENING,
  PLACEMENTS,
  composer,
  errorText,
  exampleNote,
  labelOf,
  presetBar,
} from '../../components/guidance-editor.js';

const ACTOR = 'operator';
const TITLE = '상세페이지 공지';
const PERIOD_OPENING = [{ heading: '', lines: [''] }];
const STATUS_CHIP = { SCHEDULED: 'chip info', ACTIVE: 'chip good', ENDED: 'chip' };

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

// A stored instant as the `datetime-local` value of the operator's wall time (seconds only when
// the instant has them), so an edit opens with the period exactly as saved.
function wallTime(iso) {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  const seconds = at.getSeconds() ? `:${pad(at.getSeconds())}` : '';
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    `T${pad(at.getHours())}:${pad(at.getMinutes())}${seconds}`
  );
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
    presetBar(settings, placement.key, editor),
    exampleNote('저장하기 전에는 어떤 상품에도 들어가지 않습니다.'),
    editor.el,
    h('div', { class: 'dg-actions' }, line, save),
  );
}

function periodRow(notice, settings, placement, reload, editHost) {
  const label = labelOf(settings, notice.template);
  const line = messageLine();
  // An ENDED notice is history: it is shown, never edited or ended again.
  const open = notice.status !== 'ENDED';
  const end = open
    ? h('button', { type: 'button', class: 'btn dg-small', 'data-action': 'dg-period-end' }, '조기 종료')
    : null;
  const edit = open
    ? h('button', { type: 'button', class: 'btn dg-small', 'data-action': 'dg-period-edit' }, '수정')
    : null;
  edit?.addEventListener('click', () => {
    edit.hidden = true;
    const close = () => {
      editHost.replaceChildren();
      edit.hidden = false;
    };
    editHost.replaceChildren(periodForm(placement, settings, reload, close, notice));
  });
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
    {
      class: 'dg-period',
      'data-role': 'dg-period',
      'data-guidance-id': notice.guidance_id,
      'data-seq': String(notice.seq),
      'data-status': notice.status ?? '',
    },
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
    open ? h('div', { class: 'dg-period-actions' }, edit, end) : null,
  );
}

// Adds a period notice, or (with `notice`) edits one that has not ended: the same inputs opened
// with its saved text, template and period, appended as the next revision of the same notice
// against the sequence it was read at. Its kind and placement never change (ADR-0033 §4).
function periodForm(placement, settings, reload, close, notice = null) {
  const editor = composer({
    blocks: notice ? notice.content.blocks : PERIOD_OPENING,
    template: notice?.template ?? null,
    templates: settings.templates,
    saved: Boolean(notice),
  });
  const field = (label, role, saved) => {
    const value = saved ? wallTime(saved) : '';
    const el = h('input', { type: 'datetime-local', 'data-role': role, 'aria-label': `${label} (내 컴퓨터 시간)`, value });
    // An untouched field sends the saved instant itself, not its wall time read back.
    const at = () => (saved && el.value === value ? saved : instant(el.value));
    return { el, at, row: h('label', {}, h('span', {}, label), el) };
  };
  const start = field('시작', 'dg-start', notice?.starts_at);
  const end = field('끝', 'dg-end', notice?.ends_at);
  const line = messageLine();
  const save = h('button', { type: 'button', class: 'btn blue', 'data-role': 'dg-save' }, notice ? '기간 공지 수정 저장' : '기간 공지 저장');
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
      const saved = await append({
        placement: placement.key,
        kind: 'PERIOD',
        guidance_id: notice?.guidance_id ?? null,
        expected_current_seq: notice?.seq ?? null,
        template: editor.template(),
        content: editor.content(),
        enabled: true,
        starts_at: start.at(),
        ends_at: end.at(),
      });
      toast(
        TITLE,
        notice
          ? `${placement.label} 기간 공지를 수정했습니다 (리비전 #${saved.seq}).`
          : `${placement.label} 기간 공지를 추가했습니다.`,
      );
      await reload();
    } catch (error) {
      report(line, error, reload);
      busy = false;
      sync();
    }
  });
  return h(
    'div',
    notice
      ? { class: 'dg-period-form', 'data-role': 'dg-period-edit', 'data-guidance-id': notice.guidance_id, 'data-seq': String(notice.seq) }
      : { class: 'dg-period-form', 'data-role': 'dg-period-form' },
    notice ? h('b', { class: 'dg-period-form-title' }, '기간 공지 수정') : null,
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
        ? periods.flatMap((notice) => {
            const editHost = h('div', { class: 'dg-period-edit-host' });
            return [periodRow(notice, settings, placement, reload, editHost), editHost];
          })
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
