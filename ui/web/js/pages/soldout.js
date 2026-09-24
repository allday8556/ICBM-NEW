import { contractPage } from './contract-page.js';
import { h } from '../core/dom.js';
import { countText, reviewCountTable } from '../components/review-counts.js';

// READY: the STOCK review count as the server states it. Until every producer that can emit STOCK
// is wired and current, it is not a count, and this screen never says there is nothing to check.
function ready(view) {
  const stock = view.stock_review;
  return h(
    'div',
    { class: 'panel', 'data-role': 'soldout-review', 'data-state': stock.state },
    h('h2', { class: 'panel-title' }, `재고 검토 항목: ${countText(stock)}`),
    stock.state === 'CURRENT'
      ? null
      : h(
          'div',
          { class: 'note', 'data-role': 'soldout-not-authoritative' },
          '재고 검토 건수가 아직 확정되지 않았습니다. 확인할 항목이 없다는 뜻이 아닙니다.',
        ),
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
});
