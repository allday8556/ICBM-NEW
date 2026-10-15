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
  OPEN: ['확인 필요', 'warn'],
  RESOLVED: ['해결됨', 'good'],
  SUPERSEDED: ['새 결과로 대체됨', 'info'],
};
const DISPOSITION_LABEL = {
  OWNER_ACTION_TAKEN: '공급처·마켓에서 해결함',
  FOLLOW_UP_REQUIRED: '나중에 다시 확인',
  NO_ACTION_TAKEN: '조치하지 않음',
};
// Whatever the server says about how complete the list is, the seller reads one plain line; the
// reason code stays in the note's data-reason attribute.
const COVERAGE_LINE = '확인 목록을 정리하는 중입니다. 잠시 후 다시 보면 빠진 항목이 채워질 수 있습니다.';
const DEFAULT_OUTCOME = {
  RESOLVED: '더 이상 확인할 필요가 없어 해결되었습니다.',
  CONDITION_PERSISTS: '아직 확인이 필요해 항목이 남아 있습니다. 처리 기록은 저장했습니다.',
  SUPERSEDED: '새로 바뀐 정보로 새 확인 항목이 열렸습니다.',
};
// What each item asks the seller to check (the server's reason codes), in plain words; a code
// with no line here reads by its family, then generically. The code stays in data-reason.
const REASON_COPY = {
  SOURCE_FIELD_REVIEW_REQUIRED: '수집한 정보 중 정확히 읽지 못한 항목이 있습니다.',
  SOURCE_STOCK_REVIEW_REQUIRED: '공급처의 재고(품절) 여부를 확인해야 합니다.',
  SOURCE_IMAGE_UNRESOLVED: '가져오지 못한 이미지가 있습니다.',
  LISTED_SOURCE_SOLD_OUT: '판매 중인 상품이 공급처에서 품절되었습니다.',
  REGISTER_INTENT_UNKNOWN: '등록 결과를 아직 확인하지 못했습니다.',
  REGISTER_READBACK_MISMATCH: '마켓에 등록된 내용이 보낸 내용과 다릅니다.',
  REGISTER_VERIFICATION_OVERDUE: '등록 결과 확인이 늦어지고 있습니다.',
  REGISTER_SCOPE_PAUSED_AUTH: '스마트스토어 로그인 문제로 등록 전송이 멈췄습니다.',
  REGISTER_SCOPE_PAUSED_POLICY: '마켓 정책 문제로 등록 전송이 멈췄습니다.',
  REGISTER_SCOPE_PAUSED_FAILURE_BUDGET: '연속 실패로 등록 전송이 멈췄습니다.',
};
const REASON_FAMILY = [
  [/^SOURCE_IMAGE_/, '가져오지 못한 이미지가 있습니다.'],
  [/^(M4_PRICING\.|PRICING_|PRICE_)/, '가격 계산에 확인이 필요합니다.'],
  [/IMAGE/, '이미지 확인이 필요합니다.'],
  [/^CATEGORY_/, '카테고리 확인이 필요합니다.'],
  [/^(NOTICE_|ATTRIBUTE_|FIELD_)/, '상품정보 입력 확인이 필요합니다.'],
  [/^OPTION/, '옵션 확인이 필요합니다.'],
  [/DUPLICATE|_EXISTS$/, '중복 등록 여부 확인이 필요합니다.'],
  [/^REGISTER_/, '등록 상태 확인이 필요합니다.'],
];
// The field a ``field:<key>`` subject names; any other subject is the server's own bookkeeping.
const FIELD_LABEL = {
  original_name: '상품명',
  prices: '가격',
  options: '옵션',
  images: '이미지',
  stock: '재고',
  shipping: '배송',
  minimum_sale_price: '최저판매가',
  quantity_tiers: '수량별 가격',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  notice: '상품정보고시',
  detail_description: '상세설명',
  sales_channels: '판매채널',
};

export function reviewReason(code) {
  if (REASON_COPY[code]) return REASON_COPY[code];
  const family = REASON_FAMILY.find(([pattern]) => pattern.test(String(code)));
  return family ? family[1] : '확인이 필요합니다.';
}

function subjectLabel(subject) {
  const [kind, key] = String(subject).split(':');
  if (kind === 'field' && FIELD_LABEL[key]) return `항목: ${FIELD_LABEL[key]}`;
  if (kind === 'image') return '항목: 이미지';
  return null;
}

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
    const [label, tone] = REVIEW_STATE[item.state] ?? ['확인 필요', 'warn'];
    const where = scopeLabel(item);
    const subject = subjectLabel(item.subject);
    const element = h(
      'div',
      {
        class: 'collect-review-item',
        'data-review-item': item.review_item_id,
        'data-producer': item.producer,
        'data-state': item.state,
        'data-generation': String(item.generation),
        'data-subject': item.subject,
      },
      h(
        'div',
        { class: 'supplier-head-row' },
        h('b', {}, kindLabel[item.kind] ?? '확인 항목'),
        subject ? h('span', { class: 'mini', 'data-role': 'review-subject' }, subject) : null,
        h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-review-state': item.state }, label),
      ),
      h('div', { class: 'mini', 'data-reason': item.reason_code }, reviewReason(item.reason_code)),
      where ? h('div', { class: 'mini', 'data-role': 'review-scope' }, where) : null,
    );
    if (item.state !== 'OPEN') return element;
    const disposition = h(
      'select',
      { name: 'disposition', 'data-role': 'review-disposition' },
      ...Object.entries(DISPOSITION_LABEL).map(([value, text]) => h('option', { value }, text)),
    );
    const note = h('input', { type: 'text', name: 'note', maxlength: '500', placeholder: '메모 (선택)', 'data-role': 'review-note' });
    const button = h('button', { type: 'submit', class: 'btn', 'data-action': 'resolve-review' }, '처리 기록');
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
      h('div', { class: 'supplier-head-row' }, h('b', {}, '확인 항목')),
      lastOutcome
        ? h('div', { class: 'note', 'data-role': 'review-outcome', 'data-outcome': lastOutcome }, outcomeCopy[lastOutcome] ?? '처리 기록을 저장했습니다.')
        : null,
      stale.length
        ? h(
            'div',
            { class: 'note', 'data-coverage': stale.map((c) => c.producer).join(' '), 'data-reason': stale.map((c) => c.reason ?? '').join(' ') },
            COVERAGE_LINE,
          )
        : null,
      open.length || rest.length
        ? null
        : h('div', { class: 'note', 'data-role': 'review-empty' }, stale.length ? '아직 확인 중입니다.' : emptyCopy),
      ...open.map((item) => row(item, redraw)),
      ...rest.map((item) => row(item, redraw)),
    ));
    return holder;
  }

  return block;
}
