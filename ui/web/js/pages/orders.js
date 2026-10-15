import { contractPage } from './contract-page.js';
import { orderListPanel } from '../components/order-list.js';

export default contractPage({
  key: 'orders',
  endpoint: '/api/v1/screens/orders',
  title: '주문관리',
  icon: '🛒',
  help: '모든 판매 채널의 주문을 한 곳에서 통합 관리하고 정확하게 처리합니다.',
  // The connect button is offered only while no sales channel is connected.
  empty: (view) =>
    view.marketplaces_connected > 0
      ? {
          title: '아직 가져온 주문이 없습니다',
          copy: '스마트스토어는 연결되어 있습니다. 아래 「지금 동기화」를 누르면 주문을 가져옵니다.',
        }
      : {
          title: '동기화된 주문이 없습니다',
          copy: '판매 채널 연결과 주문 동기화 후 주문이 표시됩니다.',
          actionLabel: '스마트스토어 연결하기',
          to: { page: 'settings', params: { tab: 'smartstore' } },
        },
  // M6-D: the ingested orders, their resolution and 지금 동기화. With orders, this list is the screen.
  extra: () => orderListPanel(),
});
