import { h } from '../core/dom.js';
import { dotDate } from '../core/format.js';
import { datePill } from '../components/page-head.js';
import { openPromptEditor } from '../components/prompt-registry.js';
import { contractPage } from './contract-page.js';

export default contractPage({
  key: 'ai-insight',
  endpoint: '/api/v1/screens/ai-insight',
  title: 'AI 쇼핑 인사이트',
  navLabel: 'AI쇼핑인사이트',
  icon: '✨',
  help: '홈쇼핑 방송 일정, 검색 데이터, 추천 아이템을 한 화면에서 보고 소싱·등록 전략을 빠르게 결정하세요.',
  headActions: (view) => [
    // Opens the agent's role and task in the prompt editor (ADR-0026 AIF-1), as v29 does. It edits
    // prompts only: no AI provider exists, and nothing here runs an analysis.
    h(
      'button',
      {
        type: 'button',
        class: 'btn ai',
        'data-prompt-key': 'ROLE_SHOPPING_INSIGHT_V1',
        onclick: () => openPromptEditor({ role: 'ROLE_SHOPPING_INSIGHT_V1', task: 'TASK_TREND_ANALYSIS_V1', layer: 'role' }),
      },
      '✨ Shopping Insight Agent 설정',
    ),
    datePill(`${dotDate(view.meta.generated_at)}　오늘 기준`),
  ],
  empty: {
    title: '내부 판매/상품 이력이 아직 없습니다',
    copy: '상품·판매 이력이 쌓이면 AI 쇼핑 인사이트가 내부 데이터와 함께 분석됩니다.',
    actionLabel: '통합DB 보기',
    to: { page: 'db' },
  },
});
