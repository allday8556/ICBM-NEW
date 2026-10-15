import { contractPage } from './contract-page.js';

export default contractPage({
  key: 'inquiry',
  endpoint: '/api/v1/screens/inquiry',
  title: '문의관리',
  icon: '✉',
  help: '플랫폼별 고객 문의를 한 곳에서 모아 확인하고, 빠르게 답변/처리합니다.',
  // The connect button is offered only while no sales channel is connected.
  empty: (view) =>
    view.marketplaces_connected > 0
      ? {
          title: '동기화된 문의가 없습니다',
          copy: '스마트스토어는 연결되어 있습니다. 문의를 가져오는 기능은 준비 중입니다.',
        }
      : {
          title: '동기화된 문의가 없습니다',
          copy: '플랫폼 문의 연동 후 고객 문의가 이곳에 표시됩니다.',
          actionLabel: '스마트스토어 연결하기',
          to: { page: 'settings', params: { tab: 'smartstore' } },
        },
  readyNote: (view) => ({
    title: `문의 ${Number(view.inquiries_total).toLocaleString('ko-KR')}건`,
    copy: '문의 목록과 답변 기능은 준비 중입니다.',
  }),
});
