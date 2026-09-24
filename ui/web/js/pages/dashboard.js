import { contractPage } from './contract-page.js';
import { fragment, h } from '../core/dom.js';
import { reviewCountTable } from '../components/review-counts.js';

const KINDS = ['collect_evidence', 'stock', 'source_change', 'compliance', 'registration_error', 'fulfillment'];

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

// READY: the server's counts as it states them. A review kind the server cannot count yet says
// so; nothing here decides whether there is work (ADR-0016 §7).
function ready(view) {
  return fragment(
    h(
      'div',
      { class: 'panel', 'data-role': 'dashboard-summary' },
      kv('연결된 공급처', String(view.suppliers_connected)),
      kv('연결된 판매 채널', String(view.marketplaces_connected)),
      kv('상품', String(view.products_total)),
    ),
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
