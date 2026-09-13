import { contractPage } from './contract-page.js';

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
});
