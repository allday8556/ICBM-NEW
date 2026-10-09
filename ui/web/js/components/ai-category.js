// The SmartStore category recommendation (ADR-0029 §5; C3): 통합DB detail › 카테고리 and the
// editor's 기본정보 category.
//
// What is shown is the PRODUCT DB enrichment owner's `category` result of `SMARTSTORE_CATEGORY_V1`
// for one SmartStore account: the leaf the AI chose among the official catalog's candidates, its
// whole name, the confidence, the review state, the staleness and the model. The candidates come
// from ICBM's durable catalog only (AIC-01); an AI category never confirms itself (AIC-04).

import { h } from '../core/dom.js';
import { targetedPanel, targetedResult } from './ai-targeted.js';

export const CATEGORY_TASK = 'SMARTSTORE_CATEGORY_V1';
export const CATEGORY_RESULT = 'category';
const STALE_COPY = { facts: '상품 정보', prompt: '프롬프트', policy: '정책', provider: 'AI 공급자', schema: '결과 형식' };
const ERROR_COPY = {
  AI_CATEGORY_CATALOG_MISSING: '공식 카테고리 목록이 아직 없습니다. 설정에서 카테고리 목록을 동기화하세요',
  AI_CATEGORY_NO_CANDIDATES: '상품명과 맞는 공식 카테고리 후보가 없습니다',
  AI_CATEGORY_NOT_A_CANDIDATE: 'AI가 후보에 없는 카테고리를 골랐습니다',
  AI_TARGET_NOT_BOUND: '이 판매 계정은 지금 연결된 스마트스토어 계정이 아닙니다',
  AI_TASK_NEEDS_TARGET: '스마트스토어 판매 계정이 필요합니다',
  AI_OUTPUT_SCHEMA_INVALID: 'AI 답변이 결과 형식을 지키지 않았습니다',
};

export function categoryResult(view, target) {
  return targetedResult(view, CATEGORY_TASK, CATEGORY_RESULT, target);
}

export function categoryRecommendation(result) {
  if (!result) return h('div', { class: 'mini no-data', 'data-role': 'ai-category-none' }, '아직 카테고리 추천이 없습니다');
  if (result.status !== 'OK') {
    return h(
      'div',
      { class: 'ai-category-result', 'data-role': 'ai-category-failed', 'data-error': result.error_code ?? '' },
      h('span', { class: 'chip bad' }, '카테고리 추천 실패'),
      h('span', { class: 'mini' }, ERROR_COPY[result.error_code] ?? result.error_code ?? '알 수 없는 오류'),
    );
  }
  const value = result.value ?? {};
  const confidence = typeof result.confidence === 'number' ? `${Math.round(result.confidence * 100)}%` : '—';
  const model = result.provenance?.actual_model ?? result.provenance?.requested_model ?? '—';
  return h(
    'div',
    { class: 'ai-category-result', 'data-role': 'ai-category-result', 'data-sequence': String(result.sequence) },
    h('div', { class: 'kv' }, h('span', {}, '카테고리'), h('b', { 'data-role': 'ai-category-name', 'data-category': value.category_id ?? '' }, value.whole_category_name ?? value.category_id ?? '—')),
    h('div', { class: 'kv' }, h('span', {}, '카테고리 추천 신뢰도'), h('b', { 'data-role': 'ai-category-confidence' }, confidence)),
    h(
      'div',
      { class: 'row' },
      h('span', { class: 'mini' }, `후보 ${value.candidate_count ?? '—'}개 중 선택`),
      result.requires_review ? h('span', { class: 'chip warn', 'data-role': 'ai-category-review' }, '검토 필요') : null,
      result.stale
        ? h('span', { class: 'chip warn', 'data-role': 'ai-category-stale' }, `입력 바뀜 · ${result.stale_reasons.map((key) => STALE_COPY[key] ?? key).join(', ')}`)
        : h('span', { class: 'chip good', 'data-role': 'ai-category-fresh' }, '최신'),
      h('span', { class: 'mini', 'data-role': 'ai-category-model' }, `모델 ${model}`),
    ),
  );
}

export function aiCategoryPanel(productGroupId, target = null, { onResult } = {}) {
  return targetedPanel({
    productGroupId,
    target,
    task: CATEGORY_TASK,
    resultKey: CATEGORY_RESULT,
    role: 'ai-category',
    label: 'AI 카테고리 추천',
    running: '공식 카테고리 후보 중에서 AI가 고르는 중…',
    render: categoryRecommendation,
    errorCopy: ERROR_COPY,
    onResult,
  });
}
