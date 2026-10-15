// The product-name recommendation (ADR-0027 §7; AIS-2): 통합DB detail › ✨ AI 추천 and the
// editor's 기본정보.
//
// Everything shown is the PRODUCT DB enrichment owner's read (GET …/enrichment): the target-free
// `product_name` result of the v29 bundle task, its confidence, review state, staleness and the
// model that answered. `✨ AI 추천` is live only while the `ai` capability is READY, and inert with
// the capability's reason otherwise. A request is reused by the server when today's inputs are
// unchanged; otherwise it is one job, and this panel reads the owner again until a newer result is
// recorded. The page decides nothing and computes no recommendation.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { errorText } from '../core/errors.js';
import { markInert } from '../core/inert.js';
import { toast } from '../core/toast.js';
import { sellerStateText } from './ai-provider.js';

export const NAME_TASK = 'TASK_PRODUCT_RECOMMEND_BUNDLE_V1';
export const NAME_RESULT = 'product_name';
const OPERATOR = 'operator';
const POLL_MS = 2000;
const POLL_LIMIT = 90; // three minutes

// Why an AI request failed, in plain words; anything about the AI program's setup points to 설정 › AI.
const SETUP = 'AI 연결 설정을 확인해야 합니다 (설정 › AI)';
export const AI_ERROR_COPY = {
  AI_OUTPUT_SCHEMA_INVALID: 'AI 답변 형식이 올바르지 않습니다. 다시 추천 받으세요',
  AI_OUTPUT_FIELD_MISSING: 'AI 답변에 추천이 없습니다. 다시 추천 받으세요',
  AI_OUTPUT_NOT_JSON: 'AI 답변 형식이 올바르지 않습니다. 다시 추천 받으세요',
  AI_SIDECAR_UNREACHABLE: 'AI 프로그램에 연결하지 못했습니다. 실행 중인지 확인하세요 (설정 › AI)',
  AI_SIDECAR_AUTH: 'AI 프로그램 로그인이 필요합니다 (설정 › AI)',
  AI_SIDECAR_RATE_LIMITED: 'AI 사용량 제한에 걸렸습니다. 잠시 후 다시 시도하세요',
  AI_SIDECAR_HTTP_ERROR: 'AI 프로그램이 오류로 답했습니다. 잠시 후 다시 시도하세요',
  AI_EXECUTABLE_MISMATCH: SETUP,
  AI_EXECUTABLE_NOT_SERVING: 'AI 프로그램이 실행 중이 아닙니다 (설정 › AI)',
  AI_ROUTING_MISMATCH: SETUP,
  AI_ROUTING_UNREADABLE: SETUP,
  AI_SIDECAR_KEY_UNREADABLE: SETUP,
  AI_PROVENANCE_MISMATCH: '요청하지 않은 AI가 답했습니다 (설정 › AI)',
  AI_DAILY_CAP_REACHED: '오늘 AI 사용 한도에 도달했습니다',
  AI_PROVIDER_NOT_CONFIGURED: 'AI가 아직 설정되지 않았습니다 (설정 › AI)',
};
const ERROR_COPY = AI_ERROR_COPY;
// A result whose inputs changed since it was made: the reasons stay in data-stale-reasons.
export function staleChip(role, result) {
  return h('span', { class: 'chip warn', 'data-role': role, 'data-stale-reasons': result.stale_reasons.join(' ') }, '정보가 바뀜 · 다시 추천 받으세요');
}

export function enrichmentUrl(productGroupId) {
  return `/api/v1/products/${encodeURIComponent(productGroupId)}/enrichment`;
}

// The target-free product-name result of the bundle task, or null.
export function nameResult(view) {
  return view.results.find((result) => result.task_key === NAME_TASK && result.result_key === NAME_RESULT && result.target === null) ?? null;
}

function errorCopy(error) {
  return errorText(error, ERROR_COPY);
}

// One recorded result as the operator reads it: the recommendation and its state chips.
export function recommendation(result) {
  if (!result) return h('div', { class: 'mini no-data', 'data-role': 'ai-name-none' }, '아직 추천이 없습니다');
  if (result.status !== 'OK') {
    return h(
      'div',
      { class: 'ai-name-result', 'data-role': 'ai-name-failed', 'data-error': result.error_code ?? '' },
      h('span', { class: 'chip bad' }, '추천 실패'),
      h('span', { class: 'mini' }, ERROR_COPY[result.error_code] ?? '추천을 받지 못했습니다. 다시 시도하세요'),
    );
  }
  const confidence = typeof result.confidence === 'number' ? `${Math.round(result.confidence * 100)}%` : '—';
  const model = result.provenance?.actual_model ?? result.provenance?.requested_model ?? '';
  return h(
    'div',
    { class: 'ai-name-result', 'data-role': 'ai-name-result', 'data-sequence': String(result.sequence), 'data-model': model },
    h('b', { 'data-role': 'ai-name-recommended' }, result.value?.recommended ?? '—'),
    h(
      'div',
      { class: 'row' },
      h('span', { class: 'chip' }, `신뢰도 ${confidence}`),
      result.requires_review ? h('span', { class: 'chip warn', 'data-role': 'ai-name-review' }, '검토 필요') : null,
      result.stale ? staleChip('ai-name-stale', result) : h('span', { class: 'chip good', 'data-role': 'ai-name-fresh' }, '최신'),
    ),
  );
}

// `✨ AI 추천` with the recommendation under it. `onResult` hears every result read, so a host
// (the editor) can offer its own action on it.
export function aiNamePanel(productGroupId, { onResult } = {}) {
  const body = h('div', { class: 'ai-name-body' }, h('span', { class: 'mini' }, '불러오는 중…'));
  const button = h('button', { type: 'button', class: 'btn ai-btn', 'data-action': 'ai-name-request' }, '✨ AI 추천');
  const host = h('div', { class: 'ai-name', 'data-role': 'ai-name', 'data-product': productGroupId }, button, body);
  let busy = false;

  function show(view) {
    const result = nameResult(view);
    const ready = view.ai_capability?.status === 'READY';
    button.dataset.ready = ready ? 'true' : 'false';
    if (ready) {
      delete button.dataset.inert;
      button.removeAttribute('aria-disabled');
    } else {
      markInert(button, `AI 추천 · ${sellerStateText(view.ai_capability ?? {})}`);
    }
    body.replaceChildren(recommendation(result));
    onResult?.(result);
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
      const before = nameResult(await getJson(enrichmentUrl(productGroupId)))?.sequence ?? 0;
      const sent = await sendJson('POST', enrichmentUrl(productGroupId), { actor: OPERATOR, tasks: [NAME_TASK] });
      if (sent.tasks.every((task) => task.decision === 'REUSED')) {
        toast('이미 최신 추천입니다', '상품 정보가 바뀌지 않아 다시 묻지 않았습니다.');
        await read();
        return;
      }
      body.replaceChildren(h('span', { class: 'mini', 'data-role': 'ai-name-running' }, 'AI가 상품명을 추천하는 중…'));
      for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        const view = await getJson(enrichmentUrl(productGroupId));
        if ((nameResult(view)?.sequence ?? 0) > before) {
          const result = show(view);
          toast(result?.status === 'OK' ? '상품명 추천을 받았습니다' : '상품명 추천에 실패했습니다');
          return;
        }
      }
      toast('추천이 아직 끝나지 않았습니다', '잠시 후 다시 열어 확인하세요.');
      await read();
    } catch (error) {
      toast('AI 추천을 요청하지 못했습니다', errorCopy(error));
      await read().catch(() => {});
    } finally {
      busy = false;
      button.disabled = false;
    }
  });

  read().catch((error) => {
    markInert(button, 'AI 추천 · 상태를 읽지 못했습니다');
    body.replaceChildren(h('div', { class: 'note' }, `AI 추천을 읽지 못했습니다 · ${errorCopy(error)}`));
  });
  return host;
}
