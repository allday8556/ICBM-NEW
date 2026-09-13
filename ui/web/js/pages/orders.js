import { contractPage } from './contract-page.js';

export default contractPage({
  key: 'orders',
  endpoint: '/api/v1/screens/orders',
  title: '주문관리',
  icon: '🛒',
  help: '모든 판매 채널의 주문을 한 곳에서 통합 관리하고 정확하게 처리합니다.',
  empty: {
    title: '동기화된 주문이 없습니다',
    copy: '판매 채널 연결과 주문 동기화 후 주문이 표시됩니다.',
    actionLabel: '스마트스토어 연결하기',
    to: { page: 'settings', params: { tab: 'smartstore' } },
  },
});
