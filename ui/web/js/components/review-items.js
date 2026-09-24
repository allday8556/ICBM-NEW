// The ReviewItems of one canonical owner scope, as the server holds them (ADR-0016 §5, §7, §8).
//
// Shared by the existing screens that own a scope: 수집관리 (a COLLECT source product), 통합DB (an
// M4 Product) and 등록관리 (a REGISTER account). There is no top-level review screen. The block
// renders server states and each producer's coverage verdict; it counts nothing and decides
// nothing. A resolution names the scope and generation it was shown, and the server re-derives the
// owner: the item stays open while the owner still states the condition.

import { getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';

export const REVIEW = '/api/v1/review/items';
const OPERATOR = 'operator';
const REVIEW_STATE = {
  OPEN: ['검토 필요', 'warn'],
  RESOLVED: ['해결됨', 'good'],
  SUPERSEDED: ['새 리비전으로 대체', 'info'],
};
const DISPOSITION_LABEL = {
  OWNER_ACTION_TAKEN: '원천에서 조치함',
  FOLLOW_UP_REQUIRED: '후속 조치 필요',
  NO_ACTION_TAKEN: '조치하지 않음',
};
export const COVERAGE_COPY = {
  REVIEW_COVERAGE_NO_PASS_THIS_RUN: '검토 목록의 전체 대조가 아직 끝나지 않았습니다.',
  REVIEW_COVERAGE_STALE: '검토 목록의 마지막 전체 대조가 오래되었습니다.',
  REVIEW_INDEX_FAILURE_UNRECOVERED: '검토 목록 색인에 실패한 뒤 아직 복구되지 않았습니다.',
  REVIEW_OWNER_MOVED_SINCE_PASS: '마지막 전체 대조 이후 원본이 바뀌어 다시 대조하는 중입니다.',
  REVIEW_OWNER_TRUTH_UNREADABLE: '원본 상태를 읽지 못했습니다.',
};
const DEFAULT_OUTCOME = {
  RESOLVED: '원본이 더 이상 이 조건을 도출하지 않아 항목이 해결되었습니다.',
  CONDITION_PERSISTS: '원본이 아직 이 조건을 도출하고 있어 항목은 열린 채로 남습니다. 해결 기록은 남았습니다.',
  SUPERSEDED: '원본이 새 리비전으로 바뀌어 새 검토 항목이 열렸습니다.',
};

// One block per screen region: ``block(scope)`` reads and draws; a newer render owns the block,
// so a slower, older response never replaces it.
export function reviewItemsBlock({
  kindLabel = {},
  outcomeCopy = DEFAULT_OUTCOME,
  emptyCopy = '이 범위에 기록된 검토 항목이 없습니다.',
  errorCopy = (code, message) => message ?? code,
  scopeLabel = () => null,
  className = 'collect-review',
} = {}) {
  let seq = 0;

  function row(item, onResolved) {
    const [label, tone] = REVIEW_STATE[item.state] ?? [item.state, null];
    const where = scopeLabel(item);
    const element = h(
      'div',
      {
        class: 'collect-review-item',
        'data-review-item': item.review_item_id,
        'data-producer': item.producer,
        'data-state': item.state,
        'data-generation': String(item.generation),
      },
      h(
        'div',
        { class: 'supplier-head-row' },
        h('b', {}, kindLabel[item.kind] ?? item.kind),
        h('span', { class: 'mono', 'data-role': 'review-subject' }, item.subject),
        h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-review-state': item.state }, label),
      ),
      h('div', { class: 'mini', 'data-reason': item.reason_code }, item.reason_code),
      where ? h('div', { class: 'mini mono', 'data-role': 'review-scope' }, where) : null,
    );
    if (item.state !== 'OPEN') return element;
    const disposition = h(
      'select',
      { name: 'disposition', 'data-role': 'review-disposition' },
      ...Object.entries(DISPOSITION_LABEL).map(([value, text]) => h('option', { value }, text)),
    );
    const note = h('input', { type: 'text', name: 'note', maxlength: '500', placeholder: '메모 (선택)', 'data-role': 'review-note' });
    const button = h('button', { type: 'submit', class: 'btn', 'data-action': 'resolve-review' }, '해결 기록');
    const answer = h('div', { 'data-role': 'review-answer' });
    let inFlight = false;
    const form = h('form', { class: 'supplier-actions', novalidate: true }, disposition, note, button);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      // One resolution per click: a second click while the first is on the wire does nothing.
      if (inFlight) return;
      inFlight = true;
      button.disabled = true;
      answer.replaceChildren();
      try {
        const resolved = await sendJson('POST', `${REVIEW}/${encodeURIComponent(item.review_item_id)}/resolve`, {
          expected_scope: item.scope,
          expected_generation: item.generation,
          disposition: disposition.value,
          note: note.value.trim() || null,
          actor: OPERATOR,
        });
        // The server's answer is shown on the redrawn block, which reads the item back.
        onResolved(resolved.outcome);
      } catch (error) {
        const code = error?.error?.code ?? null;
        answer.replaceChildren(h('div', { class: 'note', 'data-reason': code ?? '' }, errorCopy(code, error?.error?.message)));
        inFlight = false;
        button.disabled = false;
      }
    });
    element.append(form, answer);
    return element;
  }

  async function block(scope, lastOutcome = null) {
    const mine = ++seq;
    const holder = h('div', { class: className, 'data-role': 'review-items' });
    let listed;
    try {
      listed = await getJson(`${REVIEW}?${new URLSearchParams(scope)}`);
    } catch (error) {
      const code = error?.error?.code ?? null;
      holder.append(h('div', { class: 'note', 'data-reason': code ?? '' }, errorCopy(code, error?.error?.message)));
      return holder;
    }
    if (mine !== seq) return null; // a newer render owns the block now
    const stale = listed.coverage.filter((c) => !c.current);
    holder.dataset.coverage = stale.length ? 'NOT_CURRENT' : 'CURRENT';
    const open = listed.items.filter((item) => item.state === 'OPEN');
    const rest = listed.items.filter((item) => item.state !== 'OPEN');
    const redraw = async (outcome) => {
      const next = await block(scope, outcome);
      if (next && holder.isConnected) holder.replaceWith(next);
    };
    holder.append(fragment(
      h('div', { class: 'supplier-head-row' }, h('b', {}, '검토 항목')),
      lastOutcome
        ? h('div', { class: 'note', 'data-role': 'review-outcome', 'data-outcome': lastOutcome }, outcomeCopy[lastOutcome] ?? lastOutcome)
        : null,
      ...stale.map((c) => h('div', { class: 'note', 'data-coverage': c.producer, 'data-reason': c.reason ?? '' }, COVERAGE_COPY[c.reason] ?? c.reason)),
      open.length || rest.length
        ? null
        : h(
            'div',
            { class: 'note', 'data-role': 'review-empty' },
            stale.length ? '검토 목록이 최신이 아니어서, 비어 있어도 검토할 것이 없다는 뜻은 아닙니다.' : emptyCopy,
          ),
      ...open.map((item) => row(item, redraw)),
      ...rest.map((item) => row(item, redraw)),
    ));
    return holder;
  }

  return block;
}
