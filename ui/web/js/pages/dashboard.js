// 대시보드: v29's composition (owner decision 2026-10-07, option 가) over reads the server already
// has. Five cards, the recent collections, the registrations by marketplace, the order and alert
// summary, today's inflow, the daily trend and the top categories; a slot no read states keeps its
// place and reads 데이터 없음, never an estimate or demo content. The review counts follow as the
// server states them (ADR-0016 §7): a kind it cannot count yet says so, and nothing here decides
// whether there is work.

import { getJson } from '../core/api.js';
import { contractPage } from './contract-page.js';
import { fragment, h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { platformTag } from '../core/platform.js';
import { reviewCountTable } from '../components/review-counts.js';

const KINDS = ['collect_evidence', 'stock', 'source_change', 'compliance', 'registration_error', 'fulfillment'];
const RUNS = '/api/v1/collect/collections';
const REVISIONS = '/api/v1/collect/revisions';
const REGISTER = '/api/v1/screens/register';
const NO_DATA = '데이터 없음';
const RECENT = 5;
const MARKETPLACES = ['smartstore', 'coupang', 'gmarket', 'st11', 'auction'];
const OUTCOME_CHIP = {
  PENDING: ['수집 중', 'info'],
  RECORDED: ['기록됨', 'good'],
  NO_REVISION: ['기록할 식별자 없음', 'warn'],
  FAILED: ['실패', 'bad'],
};

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

function noData(tag = 'span', attrs = {}) {
  return h(tag, { class: 'no-data', ...attrs }, NO_DATA);
}

function count(slot, value) {
  slot.classList.remove('no-data');
  slot.dataset.total = String(value);
  slot.textContent = Number(value).toLocaleString('ko-KR');
}

// A card is a way into its screen; its number is a server count or 데이터 없음.
function card(ctx, key, icon, label, page) {
  const value = noData('strong', { 'data-card-value': key });
  const node = h(
    'button',
    { type: 'button', class: 'kpi state-card', 'data-card': key, onclick: () => ctx.navigate(page) },
    h('span', { class: 'kpi-top' }, h('span', { class: 'kicon', 'aria-hidden': 'true' }, icon), label),
    value,
    h('span', { class: 'trend no-data' }, '전일 대비 데이터 없음'),
  );
  return { node, value };
}

function outcomeChip(outcome) {
  const [label, tone] = OUTCOME_CHIP[outcome] ?? [outcome, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, label);
}

// The revision's own confirmed name, or nothing when it states none.
async function revisionName(revisionId) {
  try {
    const revision = await getJson(`${REVISIONS}/${encodeURIComponent(revisionId)}`);
    const field = revision.fields.find((candidate) => candidate.key === 'original_name');
    if (field?.status !== 'CONFIRMED' || !field.value_json) return null;
    const value = JSON.parse(field.value_json);
    return typeof value === 'string' ? value : value?.text ?? null;
  } catch {
    return null;
  }
}

function recentPanel(ctx) {
  const body = h('tbody', {}, h('tr', {}, h('td', { class: 'table-empty', colspan: '4' }, '불러오는 중입니다.')));
  const panel = h(
    'section',
    { class: 'panel', 'data-role': 'dashboard-recent' },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h3', { class: 'panel-title' }, '최근 수집 현황'),
      h('button', { type: 'button', class: 'inline-link', onclick: () => ctx.navigate('collect') }, '전체보기 ›'),
    ),
    h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...['수집 시간', '상품명', '공급처', '상태'].map((label) => h('th', {}, label)))), body),
  );
  return { panel, body };
}

function fillRecent(body, listed) {
  if (!listed.runs.length) {
    body.replaceChildren(h('tr', {}, h('td', { class: 'table-empty', colspan: '4' }, '아직 수집 기록이 없습니다.')));
    return;
  }
  body.replaceChildren(
    ...listed.runs.map((run) => {
      const name = h('span', { class: 'prod-title' }, run.outcome === 'RECORDED' ? '…' : '—');
      if (run.outcome === 'RECORDED' && run.revision_id) {
        revisionName(run.revision_id).then((found) => {
          if (found) name.textContent = found;
          else name.replaceWith(noData());
        });
      }
      return h(
        'tr',
        { 'data-recent-run': run.collection_run_id },
        h('td', {}, dotDateTime(run.requested_at)),
        h('td', {}, h('div', { class: 'prod' }, h('span', { class: 'thumb', 'aria-hidden': 'true' }, '▣'), name)),
        h('td', {}, run.supplier_key),
        h('td', {}, outcomeChip(run.outcome)),
      );
    }),
  );
}

// No read breaks registrations down by marketplace yet: the ring holds the server's total and each
// marketplace keeps its place in the legend.
function registrationsPanel(ctx) {
  const total = noData('b', { 'data-role': 'registrations-total' });
  const panel = h(
    'section',
    { class: 'panel', 'data-role': 'dashboard-registrations' },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h3', { class: 'panel-title' }, '플랫폼별 등록 현황'),
      h('button', { type: 'button', class: 'inline-link', onclick: () => ctx.navigate('register') }, '자세히 보기 ›'),
    ),
    h(
      'div',
      { class: 'donut-wrap' },
      h('div', { class: 'donut' }, h('div', { class: 'donut-label' }, h('div', {}, '전체 등록', h('br', {}), total))),
      h(
        'div',
        { class: 'legend' },
        ...MARKETPLACES.map((key) => h('div', { class: 'legend-row', 'data-marketplace': key }, platformTag(key), noData())),
      ),
    ),
    h('div', { class: 'note' }, h('b', {}, '전일 대비'), ' ', NO_DATA),
  );
  return { panel, total };
}

// A review kind's open count only when the server states it as current.
function reviewCount(view) {
  if (view?.state === 'CURRENT' && typeof view.open === 'number') {
    return h('span', { class: view.open > 0 ? 'chip bad' : 'chip good' }, String(view.open));
  }
  return noData();
}

function ordersPanel(ctx, view) {
  const counts = view.review_counts;
  return h(
    'section',
    { class: 'panel', 'data-role': 'dashboard-orders' },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h3', { class: 'panel-title' }, '주문/알림 요약'),
      h('button', { type: 'button', class: 'inline-link', onclick: () => ctx.navigate('orders') }, '전체보기 ›'),
    ),
    h(
      'div',
      { class: 'dashboard-order-counts' },
      ...['신규 주문', '배송 진행', '배송 완료'].map((label) => h('div', {}, h('div', { class: 'mini' }, label), noData('b'))),
    ),
    h(
      'div',
      { class: 'alert-list' },
      // ADR-0025 §8: 발송 대기 is shown only while the order owner is connected.
      h('div', { class: 'alert-row', 'data-role': 'awaiting-dispatch' }, h('span', {}, '발송 대기'), typeof view.orders_awaiting_dispatch === 'number' ? h('b', {}, `${view.orders_awaiting_dispatch}건`) : noData()),
      h('div', { class: 'alert-row' }, h('span', {}, '재고 부족 상품'), noData()),
      h('div', { class: 'alert-row' }, h('span', {}, '품절 확인 필요'), reviewCount(counts.stock)),
      h('div', { class: 'alert-row' }, h('span', {}, '등록 오류'), reviewCount(counts.registration_error)),
      h('div', { class: 'alert-row' }, h('span', {}, '가격 변동 알림'), reviewCount(counts.source_change)),
      h('div', { class: 'alert-row' }, h('span', {}, '시스템 알림'), noData()),
    ),
  );
}

function trafficPanel(ctx) {
  return h(
    'section',
    { class: 'panel dashboard-traffic', 'data-role': 'dashboard-traffic' },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h3', { class: 'panel-title' }, '오늘 상품 유입'),
      h('button', { type: 'button', class: 'btn', onclick: () => ctx.navigate('analytics') }, '유입 분석 보기 ›'),
    ),
    h(
      'div',
      { class: 'traffic-mini-grid' },
      ...['스마트스토어 UV', '스마트스토어 PV', '쿠팡 PV', '11번가', '추적 상품'].map((label) =>
        h('div', {}, h('span', { class: 'mini' }, label), noData('b')),
      ),
    ),
  );
}

function bottomPanels() {
  return h(
    'div',
    { class: 'dashboard-bottom' },
    h('section', { class: 'panel', 'data-role': 'dashboard-trend' }, h('h3', { class: 'panel-title' }, '일별 수집/등록 추이'), h('div', { class: 'dashboard-chart' }, noData())),
    h('section', { class: 'panel', 'data-role': 'dashboard-categories' }, h('h3', { class: 'panel-title' }, '주요 카테고리 TOP 5'), h('div', { class: 'dashboard-chart' }, noData())),
  );
}

function ready(view, ctx) {
  const cards = [
    card(ctx, 'collected', '▤', '전체 수집', 'collect'),
    card(ctx, 'waiting', '🛒', '판매 대기', 'register'),
    card(ctx, 'sold', '✓', '판매 완료', 'analytics'),
    card(ctx, 'registrable', '◇', '등록 가능', 'db'),
    card(ctx, 'review', '!', '확인 필요', 'db'),
  ];
  const recent = recentPanel(ctx);
  const registrations = registrationsPanel(ctx);
  // Reads the server already has: the recent runs with their total, and the registration counts.
  getJson(`${RUNS}?limit=${RECENT}`).then(
    (listed) => {
      count(cards[0].value, listed.total);
      fillRecent(recent.body, listed);
    },
    () => recent.body.replaceChildren(h('tr', {}, h('td', { class: 'table-empty', colspan: '4' }, NO_DATA))),
  );
  getJson(REGISTER).then(
    (screen) => {
      count(cards[1].value, screen.registration_candidates_total);
      count(registrations.total, screen.registrations_total);
    },
    () => {},
  );
  return fragment(
    h('div', { class: 'grid5 page-kpis', 'data-role': 'dashboard-cards' }, ...cards.map((entry) => entry.node)),
    h('div', { class: 'dashboard-panels' }, recent.panel, registrations.panel, ordersPanel(ctx, view)),
    trafficPanel(ctx),
    bottomPanels(),
    h(
      'div',
      { class: 'panel', 'data-role': 'dashboard-review' },
      h('h2', { class: 'panel-title' }, '검토 항목'),
      h(
        'div',
        { class: 'mini' },
        '‘최신’인 종류만 건수가 확정입니다. ‘집계 전’·‘최신 아님’은 0건이라는 뜻이 아닙니다.',
      ),
      reviewCountTable(KINDS.map((key) => view.review_counts[key])),
    ),
    h(
      'div',
      { class: 'panel', 'data-role': 'dashboard-summary' },
      kv('연결된 공급처', String(view.suppliers_connected)),
      kv('연결된 판매 채널', String(view.marketplaces_connected)),
      kv('상품', String(view.products_total)),
    ),
  );
}

export default contractPage({
  key: 'dashboard',
  endpoint: '/api/v1/screens/dashboard',
  title: '대시보드',
  icon: '▦',
  help: 'ICBM과 함께 더 많은 가능성을 만들어가는 오늘도 좋은 하루입니다.',
  empty: {
    title: '첫 운영을 시작하세요',
    copy: '공급처와 판매 채널을 연결하면 수집·등록·주문 현황이 여기에 표시됩니다.',
    actionLabel: '공급처 연결하기',
    to: { page: 'collect', params: { view: 'suppliers' } },
  },
  ready,
});
