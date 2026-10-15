import { contractPage } from './contract-page.js';
import { h } from '../core/dom.js';
import { countText, reviewCountTable } from '../components/review-counts.js';
import { stockRecheckPanel } from '../components/stock-recheck.js';

// READY: the STOCK review count as the server states it. Until every producer that can emit STOCK
// is wired and current, it is not a count (it reads 확인 중), and this screen never says there is
// nothing to check.
function ready(view) {
  const stock = view.stock_review;
  return h(
    'div',
    { class: 'panel', 'data-role': 'soldout-review', 'data-state': stock.state },
    h('h2', { class: 'panel-title' }, `품절 확인: ${countText(stock)}`),
    reviewCountTable([stock]),
  );
}

export default contractPage({
  key: 'soldout',
  endpoint: '/api/v1/screens/soldout',
  title: '품절확인',
  icon: '✓',
  help: '자동으로 감지된 품절 의심 상품을 검토하고 정확한 상태를 확정합니다.',
  empty: {
    title: '확인할 재고 항목이 없습니다',
    copy: '품절 의심 신호가 발견되면 검토 항목이 이곳에 표시됩니다.',
    actionLabel: '수집 상태 확인',
    to: { page: 'collect', params: { view: 'jobs' } },
  },
  ready,
  // M6-B: the listed source products' supplier stock and its recheck.
  extra: () => stockRecheckPanel(),
});
