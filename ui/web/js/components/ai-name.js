// The product-name recommendation (ADR-0027 §7; AIS-2): 통합DB detail › ✨ AI 추천 and the
// editor's 기본정보.
//
// Everything shown is the PRODUCT DB enrichment owner's read (GET …/enrichment): the target-free
// `product_name` result of the v29 bundle task, its confidence, review state, staleness and the
// model that answered. `✨ AI 추천` is live only while the `ai` capability is READY, and inert with
// the capability's reason otherwise. A request is reused by the server when today's inputs are
// unchanged; otherwise it is one job, and this panel reads the owner again until a newer result is
// recorded. The page decides nothing and computes no recommendation.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { markInert } from '../core/inert.js';
import { toast } from '../core/toast.js';
import { stateText } from './ai-provider.js';

export const NAME_TASK = 'TASK_PRODUCT_RECOMMEND_BUNDLE_V1';
export const NAME_RESULT = 'product_name';
const OPERATOR = 'operator';
const POLL_MS = 2000;
const POLL_LIMIT = 90; // three minutes

const STALE_COPY = { facts: '상품 정보', prompt: '프롬프트', policy: '정책', provider: 'AI 공급자', schema: '결과 형식' };
const ERROR_COPY = {
  AI_OUTPUT_SCHEMA_INVALID: 'AI 답변이 결과 형식(근거 · 신뢰도 · 검토 필요)을 지키지 않았습니다',
  AI_OUTPUT_FIELD_MISSING: 'AI 답변에 상품명 추천이 없습니다',
  AI_OUTPUT_NOT_JSON: 'AI 답변이 JSON이 아닙니다',
  AI_SIDECAR_UNREACHABLE: 'CLIProxyAPI에 연결하지 못했습니다',
  AI_SIDECAR_AUTH: 'CLIProxyAPI 로그인이 필요합니다',
  AI_SIDECAR_RATE_LIMITED: 'AI 사용량 제한에 걸렸습니다',
  AI_SIDECAR_HTTP_ERROR: 'CLIProxyAPI가 오류로 답했습니다',
  AI_EXECUTABLE_MISMATCH: '지금 실행 중인 CLIProxyAPI가 승인한 파일과 다릅니다',
  AI_EXECUTABLE_NOT_SERVING: '승인한 CLIProxyAPI가 실행 중이 아닙니다',
  AI_ROUTING_MISMATCH: 'CLIProxyAPI 라우팅 설정이 승인한 설정과 다릅니다',
  AI_ROUTING_UNREADABLE: 'CLIProxyAPI 설정 파일을 읽을 수 없습니다',
  AI_SIDECAR_KEY_UNREADABLE: 'CLIProxyAPI 설정에서 접속 키를 읽을 수 없습니다',
  AI_PROVENANCE_MISMATCH: '답한 AI가 요청한 공급자와 다릅니다',
  AI_DAILY_CAP_REACHED: '오늘 호출 상한에 도달했습니다',
  AI_PROVIDER_NOT_CONFIGURED: 'AI 공급자가 준비되지 않았습니다',
};

export function enrichmentUrl(productGroupId) {
  return `/api/v1/products/${encodeURIComponent(productGroupId)}/enrichment`;
}

// The target-free product-name result of the bundle task, or null.
export function nameResult(view) {
  return view.results.find((result) => result.task_key === NAME_TASK && result.result_key === NAME_RESULT && result.target === null) ?? null;
}

function errorCopy(error) {
  if (error instanceof ApiError) return ERROR_COPY[error.error?.code] ?? error.message;
  return String(error);
}

// One recorded result as the operator reads it: the recommendation and its state chips.
export function recommendation(result) {
  if (!result) return h('div', { class: 'mini no-data', 'data-role': 'ai-name-none' }, '아직 추천이 없습니다');
  if (result.status !== 'OK') {
    return h(
      'div',
      { class: 'ai-name-result', 'data-role': 'ai-name-failed', 'data-error': result.error_code ?? '' },
      h('span', { class: 'chip bad' }, '추천 실패'),
      h('span', { class: 'mini' }, ERROR_COPY[result.error_code] ?? result.error_code ?? '알 수 없는 오류'),
    );
  }
  const confidence = typeof result.confidence === 'number' ? `${Math.round(result.confidence * 100)}%` : '—';
  const model = result.provenance?.actual_model ?? result.provenance?.requested_model ?? '—';
  return h(
    'div',
    { class: 'ai-name-result', 'data-role': 'ai-name-result', 'data-sequence': String(result.sequence) },
    h('b', { 'data-role': 'ai-name-recommended' }, result.value?.recommended ?? '—'),
    h(
      'div',
      { class: 'row' },
      h('span', { class: 'chip' }, `신뢰도 ${confidence}`),
      result.requires_review ? h('span', { class: 'chip warn', 'data-role': 'ai-name-review' }, '검토 필요') : null,
      result.stale
        ? h('span', { class: 'chip warn', 'data-role': 'ai-name-stale' }, `입력 바뀜 · ${result.stale_reasons.map((key) => STALE_COPY[key] ?? key).join(', ')}`)
        : h('span', { class: 'chip good', 'data-role': 'ai-name-fresh' }, '최신'),
      h('span', { class: 'mini', 'data-role': 'ai-name-model' }, `모델 ${model}`),
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
      markInert(button, `AI 추천 · ${stateText(view.ai_capability ?? {})}`);
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
        toast('이미 최신 추천입니다', '상품 정보와 프롬프트가 그대로라 다시 묻지 않았습니다.');
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
