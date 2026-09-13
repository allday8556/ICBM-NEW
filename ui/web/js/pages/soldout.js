import { contractPage } from './contract-page.js';

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
});
