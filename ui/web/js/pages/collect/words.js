// 수집관리 › 공급처 관리 › 「수집 규칙」 (ADR-0036 §6, slice O4).
//
// Live on an onboarded supplier's card while it is 분석 중 (RECON) or 사용 중 (ACTIVE). Each
// template slot shows the template's own words (read-only) beside this supplier's words, which the
// operator edits as plain words, one per line: a word may itself hold a comma (ADR-0035's phrases). 「미리보기」 re-analyses the stored sample
// captures under the current and the proposed words — nothing is read from the supplier, nothing is
// saved — and shows, per sample, the unread rows the new words read and the field statuses they
// change. 「저장」 appends a words revision (the store's word rules decide what passes); a 사용 중
// supplier returns to 분석 중 and must be approved again. 「다시 분석」 then re-reads the stored
// captures under the saved words. The page decides nothing: every refusal is the server's.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { openModal } from '../../core/modal.js';
import { toast } from '../../core/toast.js';
import { FIELD_LABEL, SLOT_LABEL, STATUS, message } from './analysis.js';
import { ONBOARDING } from './onboarding.js';

const KIND_LABEL = { row: '행 이름', phrase: '문구', mark: '표시' };

const WORDS_ERROR = {
  ONBOARDING_WORDS_INVALID: '기호 없이 단어만 넣을 수 있습니다. 항목마다 최대 12개, 단어당 40자까지입니다.',
  ONBOARDING_WORDS_SLOT_UNKNOWN: '이 쇼핑몰에 없는 항목입니다. 창을 닫았다 다시 여세요.',
  ONBOARDING_WORDS_MOVED: '그 사이 수집 규칙이 바뀌었습니다. 창을 닫았다 다시 열어 최신 규칙에서 고치세요.',
  ONBOARDING_WORDS_UNCHANGED: '지금 규칙과 같습니다. 바꾼 단어가 없습니다.',
  ONBOARDING_SUPPLIER_DISABLED: '비활성 공급처의 수집 규칙은 바꾸지 않습니다.',
};

function wordsMessage(error, fallback) {
  return WORDS_ERROR[error?.error?.code] ?? message(error, fallback);
}

// One word per line. A comma is part of a word (a phrase such as 「쿠팡, 스마트스토어 판매 가능」),
// never a separator.
function parseWords(text) {
  const words = [];
  for (const part of text.split('\n')) {
    const word = part.trim();
    if (word && !words.includes(word)) words.push(word);
  }
  return words;
}

function statusLabel(status) {
  return (STATUS[status] ?? [status])[0];
}

function previewBlock(sample) {
  const lines = [];
  if (sample.diverged) {
    lines.push(h('div', { class: 'note', 'data-role': 'diverged' }, '이 샘플의 저장본은 페이지와 다르게 읽혀 승인에 쓰이지 않습니다.'));
  }
  if (sample.newly_read.length) {
    lines.push(
      h('div', { 'data-role': 'newly-read' }, h('b', {}, '새로 읽음: '), sample.newly_read.map((label) => h('span', { class: 'chip good', 'data-label': label }, label))),
    );
  }
  if (sample.newly_unread.length) {
    lines.push(
      h('div', { 'data-role': 'newly-unread' }, h('b', {}, '더 이상 읽지 않음: '), sample.newly_unread.map((label) => h('span', { class: 'chip warn', 'data-label': label }, label))),
    );
  }
  for (const change of sample.changes) {
    lines.push(
      h(
        'div',
        { 'data-change': change.field },
        `${FIELD_LABEL[change.field] ?? change.field}: ${statusLabel(change.now)} → ${statusLabel(change.proposed)}`,
      ),
    );
  }
  if (!lines.length) lines.push(h('div', { class: 'mini', 'data-role': 'no-change' }, '바뀌는 것이 없습니다.'));
  return h(
    'div',
    { class: 'analysis-sample', 'data-preview-sample': sample.sample_url },
    h('b', { class: 'analysis-url' }, sample.sample_url),
    h('div', { class: 'mini' }, `읽지 못한 행/문구 ${sample.unread_now}개 → ${sample.unread_proposed}개`),
    lines,
  );
}

export async function openCollectionWords(record, { errorMessage, onSaved }) {
  const key = record.supplier_key;
  const base = `${ONBOARDING}/suppliers/${key}`;
  const status = h('div', { class: 'note', 'data-role': 'words-status', hidden: true });
  const table = h('div', { 'data-role': 'words-table' });
  const preview = h('div', { class: 'analysis-samples', 'data-role': 'words-preview' });
  const previewButton = h('button', { type: 'button', class: 'btn', 'data-action': 'preview-words' }, '미리보기');
  const saveButton = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-words' }, '저장');
  const reanalyse = h('button', { type: 'button', class: 'btn', 'data-action': 'reanalyse-words', hidden: true }, '다시 분석');
  const inputs = new Map();
  let view = null;
  let busy = false;

  const say = (text) => {
    status.hidden = !text;
    status.textContent = text ?? '';
  };
  const refreshButtons = () => {
    previewButton.disabled = busy || !view;
    saveButton.disabled = busy || !view;
    reanalyse.disabled = busy;
  };
  const proposed = () => {
    const words = {};
    for (const [slot, input] of inputs) {
      const list = parseWords(input.value);
      if (list.length) words[slot] = list;
    }
    return words;
  };

  function render() {
    inputs.clear();
    const warning =
      view.state === 'ACTIVE'
        ? [h('div', { class: 'note', 'data-role': 'active-warning' }, '사용 중인 공급처입니다. 저장하면 분석 중으로 돌아가고, 다시 승인해야 수집에 쓰입니다.')]
        : [];
    table.replaceChildren(
      ...warning,
      h(
        'table',
        { class: 'table words-table' },
        h('thead', {}, h('tr', {}, h('th', {}, '항목'), h('th', {}, '기본 단어'), h('th', {}, '이 공급처 단어'))),
        h(
          'tbody',
          {},
          view.slots.map((slot) => {
            const input = h('textarea', {
              rows: Math.max(1, slot.site.length),
              autocomplete: 'off',
              'data-role': 'slot-words',
              'aria-label': `${SLOT_LABEL[slot.slot] ?? '기타 항목'} 단어`,
              placeholder: '한 줄에 한 단어',
            });
            input.value = slot.site.join('\n');
            inputs.set(slot.slot, input);
            return h(
              'tr',
              { 'data-slot': slot.slot, 'data-kind': slot.kind },
              h('td', {}, SLOT_LABEL[slot.slot] ?? '기타 항목', h('div', { class: 'mini' }, KIND_LABEL[slot.kind] ?? '')),
              h(
                'td',
                { class: 'analysis-value', 'data-role': 'defaults' },
                slot.defaults.length ? slot.defaults.map((word) => h('span', { class: 'chip' }, word)) : h('span', { class: 'mini' }, '-'),
              ),
              h('td', {}, input),
            );
          }),
        ),
      ),
    );
  }

  async function load() {
    view = await getJson(`${base}/words`);
    render();
    refreshButtons();
  }

  async function run(work) {
    busy = true;
    refreshButtons();
    try {
      await work();
    } catch (error) {
      say(wordsMessage(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  }

  previewButton.addEventListener('click', () =>
    run(async () => {
      const result = await sendJson('POST', `${base}/words/preview`, { words: proposed() });
      preview.replaceChildren(
        ...(result.samples.length
          ? result.samples.map(previewBlock)
          : [h('div', { class: 'mini', 'data-role': 'no-samples' }, '저장된 샘플이 없습니다. 사이트 분석에서 샘플을 먼저 읽으세요.')]),
      );
      say(`저장된 샘플 ${result.samples.length}개를 새 규칙으로 미리 봤습니다. 공급처에는 요청하지 않았고, 아직 저장하지 않았습니다.`);
    }),
  );

  saveButton.addEventListener('click', () =>
    run(async () => {
      const wasActive = view.state === 'ACTIVE';
      view = await sendJson('PUT', `${base}/words`, { words: proposed(), expected_seq: view.words_seq });
      render();
      reanalyse.hidden = false;
      status.dataset.wordsSeq = String(view.words_seq);
      say(`수집 규칙을 저장했습니다.${wasActive ? ' 공급처는 분석 중으로 돌아갔습니다.' : ''} 「다시 분석」으로 저장된 샘플을 새 규칙으로 다시 봅니다.`);
      toast('수집 규칙', '저장했습니다.');
      onSaved?.();
    }),
  );

  reanalyse.addEventListener('click', () =>
    run(async () => {
      const result = await sendJson('POST', `${base}/reanalyse`);
      say(`저장된 샘플 ${result.analysed}개를 새 규칙으로 다시 분석했습니다. 공급처에는 요청하지 않았습니다.`);
    }),
  );

  openModal({
    eyebrow: '공급처 관리',
    title: `수집 규칙 · ${record.display_name}`,
    help: '기본 단어 옆에 이 공급처가 쓰는 단어를 더합니다. 미리보기는 저장된 샘플을 다시 볼 뿐 공급처에 요청하지 않습니다.',
    body: h(
      'div',
      { class: 'analysis', 'data-role': 'collection-words' },
      table,
      h('div', { class: 'analysis-add' }, previewButton, saveButton, reanalyse),
      status,
      preview,
    ),
  });
  refreshButtons();
  try {
    await load();
  } catch (error) {
    say(wordsMessage(error, errorMessage));
  }
}
