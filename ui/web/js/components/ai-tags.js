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

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { markInert } from '../core/inert.js';
import { toast } from '../core/toast.js';
import { enrichmentUrl } from './ai-name.js';
import { stateText } from './ai-provider.js';

export const TAG_TASK = 'SMARTSTORE_TAGS_V1';
export const TAG_RESULT = 'tags';
const DRAFT_TARGETS = '/api/v1/register/draft-targets';
const OPERATOR = 'operator';
const POLL_MS = 2000;
const POLL_LIMIT = 90;

const SOURCE_COPY = { PLATFORM: '네이버 추천', AI_DIRECT: 'AI 직접' };
const REMOVED_COPY = {
  restricted: '제한 태그',
  not_checked: '제한 여부 확인 안 됨',
  name_or_brand: '상품명·브랜드와 같음',
  over_limit: '10개 초과',
  too_long: '50자 초과',
};
const STALE_COPY = { facts: '상품 정보', prompt: '프롬프트', policy: '정책', provider: 'AI 공급자', schema: '결과 형식' };
const ERROR_COPY = {
  AI_TAGS_NONE_USABLE: '쓸 수 있는 태그가 남지 않았습니다 (제한 태그 · 중복 제외 후)',
  AI_TARGET_NOT_BOUND: '이 판매 계정은 지금 연결된 스마트스토어 계정이 아닙니다',
  AI_TASK_NEEDS_TARGET: '스마트스토어 판매 계정이 필요합니다',
  SMARTSTORE_SESSION_UNAVAILABLE: '스마트스토어 연결 세션이 없습니다. 연결 상태를 확인하세요',
  SMARTSTORE_TAG_RESPONSE_INVALID: '네이버 태그 응답이 문서와 다릅니다',
  AI_OUTPUT_SCHEMA_INVALID: 'AI 답변이 결과 형식을 지키지 않았습니다',
};

function errorCopy(error) {
  if (error instanceof ApiError) return ERROR_COPY[error.error?.code] ?? error.message;
  return String(error);
}

// The SmartStore account bound to the connection now, or null (the server checks again).
export async function boundSmartStoreTarget() {
  const listed = await getJson(DRAFT_TARGETS);
  const target = listed.targets.find((candidate) => candidate.marketplace_key === 'smartstore' && candidate.binding === 'BOUND');
  return target ? { marketplace_key: target.marketplace_key, marketplace_account_id: target.marketplace_account_id } : null;
}

export function tagResult(view, target) {
  return (
    view.results.find(
      (result) =>
        result.task_key === TAG_TASK &&
        result.result_key === TAG_RESULT &&
        result.target?.marketplace_key === target.marketplace_key &&
        result.target?.marketplace_account_id === target.marketplace_account_id,
    ) ?? null
  );
}

export function tagRecommendation(result) {
  if (!result) return h('div', { class: 'mini no-data', 'data-role': 'ai-tags-none' }, '아직 태그 추천이 없습니다');
  if (result.status !== 'OK') {
    return h(
      'div',
      { class: 'ai-tags-result', 'data-role': 'ai-tags-failed', 'data-error': result.error_code ?? '' },
      h('span', { class: 'chip bad' }, '태그 추천 실패'),
      h('span', { class: 'mini' }, ERROR_COPY[result.error_code] ?? result.error_code ?? '알 수 없는 오류'),
    );
  }
  const value = result.value ?? {};
  const removed = value.removed ?? [];
  const model = result.provenance?.actual_model ?? result.provenance?.requested_model ?? '—';
  const checked = typeof value.restricted_checked_at === 'string' ? value.restricted_checked_at.slice(0, 16).replace('T', ' ') : '—';
  return h(
    'div',
    { class: 'ai-tags-result', 'data-role': 'ai-tags-result', 'data-sequence': String(result.sequence) },
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
          removed.map((item) => `${item.text} (${REMOVED_COPY[item.reason] ?? item.reason})`).join(', '),
        )
      : null,
    h(
      'div',
      { class: 'row' },
      h('span', { class: 'mini', 'data-role': 'ai-tags-checked' }, `제한 태그 확인 ${checked}`),
      result.requires_review ? h('span', { class: 'chip warn', 'data-role': 'ai-tags-review' }, '검토 필요') : null,
      result.stale
        ? h('span', { class: 'chip warn', 'data-role': 'ai-tags-stale' }, `입력 바뀜 · ${result.stale_reasons.map((key) => STALE_COPY[key] ?? key).join(', ')}`)
        : h('span', { class: 'chip good', 'data-role': 'ai-tags-fresh' }, '최신'),
      h('span', { class: 'mini', 'data-role': 'ai-tags-model' }, `모델 ${model}`),
    ),
  );
}

// `✨ AI 태그 추천` with the recommendation under it. `target` is a SmartStore account, or null to
// use the account bound to the connection. `onResult(result, target)` hears every read.
export function aiTagsPanel(productGroupId, target = null, { onResult } = {}) {
  const body = h('div', { class: 'ai-tags-body' }, h('span', { class: 'mini' }, '불러오는 중…'));
  const button = h('button', { type: 'button', class: 'btn ai-btn', 'data-action': 'ai-tags-request' }, '✨ AI 태그 추천');
  const host = h('div', { class: 'ai-tags', 'data-role': 'ai-tags', 'data-product': productGroupId }, button, body);
  let account = target;
  let busy = false;

  function show(view) {
    const ready = view.ai_capability?.status === 'READY' && account !== null;
    button.dataset.ready = ready ? 'true' : 'false';
    if (ready) {
      delete button.dataset.inert;
      button.removeAttribute('aria-disabled');
    } else if (account === null) {
      markInert(button, 'AI 태그 추천 · 연결된 스마트스토어 판매 계정이 없습니다');
    } else {
      markInert(button, `AI 태그 추천 · ${stateText(view.ai_capability ?? {})}`);
    }
    const result = account ? tagResult(view, account) : null;
    body.replaceChildren(account ? tagRecommendation(result) : h('div', { class: 'mini no-data' }, '연결된 스마트스토어 판매 계정이 없습니다'));
    onResult?.(result, account);
    return result;
  }

  async function read() {
    return show(await getJson(enrichmentUrl(productGroupId)));
  }

  button.addEventListener('click', async () => {
    if (busy || button.dataset.ready !== 'true') return;
    busy = true;
    button.disabled = true;
    try {
      const before = tagResult(await getJson(enrichmentUrl(productGroupId)), account)?.sequence ?? 0;
      const sent = await sendJson('POST', enrichmentUrl(productGroupId), { actor: OPERATOR, tasks: [TAG_TASK], target: account });
      const jobId = sent.job_id;
      body.replaceChildren(h('span', { class: 'mini', 'data-role': 'ai-tags-running' }, '네이버 추천 태그를 읽고 AI가 고르는 중…'));
      for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        const view = await getJson(enrichmentUrl(productGroupId));
        const result = tagResult(view, account);
        const settled = await jobSettled(jobId);
        if ((result?.sequence ?? 0) > before || settled) {
          show(view);
          toast(
            (result?.sequence ?? 0) > before
              ? result.status === 'OK'
                ? '태그 추천을 받았습니다'
                : '태그 추천에 실패했습니다'
              : '입력이 그대로라 이전 추천을 그대로 씁니다',
          );
          return;
        }
      }
      toast('태그 추천이 아직 끝나지 않았습니다', '잠시 후 다시 열어 확인하세요.');
      await read();
    } catch (error) {
      toast('AI 태그 추천을 요청하지 못했습니다', errorCopy(error));
      await read().catch(() => {});
    } finally {
      busy = false;
      button.disabled = false;
    }
  });

  // A job that found its inputs unchanged records nothing (ADR-0028 §4 note): the panel stops
  // waiting once the request's own job has ended.
  async function jobSettled(jobId) {
    if (!jobId) return true;
    const job = await getJson(`/api/v1/system/jobs/${encodeURIComponent(jobId)}`).catch(() => null);
    return job !== null && ['SUCCEEDED', 'DEAD'].includes(job.state);
  }

  (async () => {
    if (account === null) account = await boundSmartStoreTarget().catch(() => null);
    await read();
  })().catch((error) => {
    markInert(button, 'AI 태그 추천 · 상태를 읽지 못했습니다');
    body.replaceChildren(h('div', { class: 'note' }, `태그 추천을 읽지 못했습니다 · ${errorCopy(error)}`));
  });
  return host;
}
