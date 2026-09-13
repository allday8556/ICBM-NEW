import { contractPage } from './contract-page.js';

// The registration-automation toggles (ADR-0004) belong to the populated registration view.
// The EMPTY contract renders the approved zero-data surface, so no automation control is bound.
export default contractPage({
  key: 'register',
  endpoint: '/api/v1/screens/register',
  title: '등록관리',
  icon: '▱',
  help: '수집한 상품을 각 플랫폼에 등록하고, 등록 현황을 한눈에 관리합니다.',
  empty: {
    title: '등록 후보가 없습니다',
    copy: '통합DB에서 등록 가능한 상품을 선택하면 Preflight를 거쳐 등록할 수 있습니다.',
    actionLabel: '통합DB 보기',
    to: { page: 'db' },
  },
});
