import { dotDate } from '../core/format.js';
import { datePill } from '../components/page-head.js';
import { contractPage } from './contract-page.js';

export default contractPage({
  key: 'analytics',
  endpoint: '/api/v1/screens/analytics',
  title: '분석',
  icon: '▥',
  help: '데이터로 더 나은 커머스 운영을 만들어가는 ICBM의 인사이트를 확인하세요.',
  headActions: (view) => [
    datePill(`${dotDate(view.period.start)} ~ ${dotDate(view.period.end)}　최근 ${view.period.days}일`),
  ],
  empty: {
    title: '분석할 데이터가 아직 부족합니다',
    copy: '주문·등록·유입 데이터가 쌓이면 운영 지표가 표시됩니다.',
    actionLabel: '등록관리 보기',
    to: { page: 'register' },
  },
});
