// The SmartStore tag recommendation (ADR-0028 §7; T4): 통합DB detail › 태그 / AI 상태 and the
// editor's 기본정보 tag block.
//
// Everything shown is the PRODUCT DB enrichment owner's read: the product's `tags` result of the
// `SMARTSTORE_TAGS_V1` task for one SmartStore account — the tags with their source (the
// platform's recommended tag, or a tag the AI wrote itself), what the filter removed and why, when
// the restricted check ran, the review state, the staleness and the model. `✨ AI 태그 추천` is
// live only while the `ai` capability is READY and a SmartStore account is bound to the
// connection; the server reads the platform in its job, never here. No tag is ever sent to the
// marketplace (ADR-0028 AIT-01).

import { h } from '../core/dom.js';
import { AI_ERROR_COPY, staleChip } from './ai-name.js';
import { boundSmartStoreTarget as boundTarget, targetedPanel, targetedResult } from './ai-targeted.js';

export const TAG_TASK = 'SMARTSTORE_TAGS_V1';
export const TAG_RESULT = 'tags';
const SOURCE_COPY = { PLATFORM: '네이버 추천', AI_DIRECT: 'AI 직접' };
const REMOVED_COPY = {
  restricted: '제한 태그',
  not_checked: '제한 여부 확인 안 됨',
  name_or_brand: '상품명·브랜드와 같음',
  over_limit: '10개 초과',
  too_long: '50자 초과',
};
const ERROR_COPY = {
  ...AI_ERROR_COPY,
  AI_TAGS_NONE_USABLE: '쓸 수 있는 태그가 남지 않았습니다 (제한 태그 · 중복 제외 후)',
  AI_TARGET_NOT_BOUND: '이 판매 계정은 지금 연결된 스마트스토어 계정이 아닙니다',
  AI_TASK_NEEDS_TARGET: '스마트스토어 판매 계정이 필요합니다',
  SMARTSTORE_SESSION_UNAVAILABLE: '스마트스토어 연결을 확인하세요 (설정 › 스마트스토어)',
  SMARTSTORE_TAG_RESPONSE_INVALID: '네이버에서 추천 태그를 읽지 못했습니다. 잠시 후 다시 시도하세요',
};

export async function boundSmartStoreTarget() {
  return boundTarget();
}

export function tagResult(view, target) {
  return targetedResult(view, TAG_TASK, TAG_RESULT, target);
}

export function tagRecommendation(result) {
  if (!result) return h('div', { class: 'mini no-data', 'data-role': 'ai-tags-none' }, '아직 태그 추천이 없습니다');
  if (result.status !== 'OK') {
    return h(
      'div',
      { class: 'ai-tags-result', 'data-role': 'ai-tags-failed', 'data-error': result.error_code ?? '' },
      h('span', { class: 'chip bad' }, '태그 추천 실패'),
      h('span', { class: 'mini' }, ERROR_COPY[result.error_code] ?? '태그 추천을 받지 못했습니다. 다시 시도하세요'),
    );
  }
  const value = result.value ?? {};
  const removed = value.removed ?? [];
  const model = result.provenance?.actual_model ?? result.provenance?.requested_model ?? '';
  return h(
    'div',
    {
      class: 'ai-tags-result',
      'data-role': 'ai-tags-result',
      'data-sequence': String(result.sequence),
      'data-model': model,
      'data-restricted-checked-at': value.restricted_checked_at ?? '',
    },
    h(
      'div',
      { class: 'tags', 'data-role': 'ai-tags-list' },
      ...(value.recommended ?? []).map((tag) =>
        h('span', { class: 'tag', 'data-source': tag.source, title: SOURCE_COPY[tag.source] ?? tag.source }, `#${tag.text}`, h('span', { class: 'mini' }, ` ${SOURCE_COPY[tag.source] ?? ''}`)),
      ),
    ),
    removed.length
      ? h(
          'div',
          { class: 'mini', 'data-role': 'ai-tags-removed' },
          `제외 ${removed.length}개 · `,
          removed.map((item) => `${item.text} (${REMOVED_COPY[item.reason] ?? '기타'})`).join(', '),
        )
      : null,
    h(
      'div',
      { class: 'row' },
      result.requires_review ? h('span', { class: 'chip warn', 'data-role': 'ai-tags-review' }, '검토 필요') : null,
      result.stale ? staleChip('ai-tags-stale', result) : h('span', { class: 'chip good', 'data-role': 'ai-tags-fresh' }, '최신'),
      model ? h('span', { class: 'mini', 'data-role': 'ai-tags-model' }, `모델 ${model}`) : null,
    ),
  );
}

// `✨ AI 태그 추천` with the recommendation under it. `target` is a SmartStore account, or null to
// use the account bound to the connection. `onResult(result, target)` hears every read.
export function aiTagsPanel(productGroupId, target = null, { onResult } = {}) {
  return targetedPanel({
    productGroupId,
    target,
    task: TAG_TASK,
    resultKey: TAG_RESULT,
    role: 'ai-tags',
    label: 'AI 태그 추천',
    running: '네이버 추천 태그를 읽고 AI가 고르는 중…',
    render: tagRecommendation,
    errorCopy: ERROR_COPY,
    onResult,
  });
}
