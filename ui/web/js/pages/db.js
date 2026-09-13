import { h } from '../core/dom.js';
import { markInert } from '../core/inert.js';
import { contractPage } from './contract-page.js';

export default contractPage({
  key: 'db',
  endpoint: '/api/v1/screens/db',
  title: '통합DB',
  icon: '◫',
  help: '모든 상품을 하나의 DB에서 관리하고 등록부터 판매까지 빠르게 운영합니다.',
  headActions: () => [markInert(h('button', { type: 'button', class: 'btn' }, '⇩ 엑셀 다운로드'))],
  empty: {
    title: '통합DB에 상품이 없습니다',
    copy: '수집이 완료되면 정규화된 상품이 이곳에 쌓입니다.',
    actionLabel: '상품 수집하기',
    to: { page: 'collect', params: { view: 'jobs' } },
  },
});
