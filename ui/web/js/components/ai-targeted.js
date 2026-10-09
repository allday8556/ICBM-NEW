// One recommendation of a SmartStore-targeted enrichment task (ADR-0028 tags, ADR-0029 category).
//
// The panel reads the PRODUCT DB enrichment owner's results, shows the task's result for one
// SmartStore account (the account bound to the connection unless one is given), and offers its
// own `✨` button, live only while the `ai` capability is READY and such an account exists. A
// press asks the server once; the server's job reads what it needs and records a result, or
// records nothing when its inputs are unchanged (ADR-0028 §4), so the panel waits for a newer
// result or for its own job to end. The page decides nothing and computes nothing.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { markInert } from '../core/inert.js';
import { toast } from '../core/toast.js';
import { enrichmentUrl } from './ai-name.js';
import { stateText } from './ai-provider.js';

const DRAFT_TARGETS = '/api/v1/register/draft-targets';
const OPERATOR = 'operator';
const POLL_MS = 2000;
const POLL_LIMIT = 90;

// The SmartStore account bound to the connection now, or null (the server checks again).
export async function boundSmartStoreTarget() {
  const listed = await getJson(DRAFT_TARGETS);
  const target = listed.targets.find((candidate) => candidate.marketplace_key === 'smartstore' && candidate.binding === 'BOUND');
  return target ? { marketplace_key: target.marketplace_key, marketplace_account_id: target.marketplace_account_id } : null;
}

export function targetedResult(view, task, resultKey, target) {
  return (
    view.results.find(
      (result) =>
        result.task_key === task &&
        result.result_key === resultKey &&
        result.target?.marketplace_key === target.marketplace_key &&
        result.target?.marketplace_account_id === target.marketplace_account_id,
    ) ?? null
  );
}

async function jobSettled(jobId) {
  if (!jobId) return true;
  const job = await getJson(`/api/v1/system/jobs/${encodeURIComponent(jobId)}`).catch(() => null);
  return job !== null && ['SUCCEEDED', 'DEAD'].includes(job.state);
}

// options: productGroupId, target (or null), task, resultKey, role, label, running, render(result),
// errorCopy (code → text), onResult(result, target).
export function targetedPanel(options) {
  const { productGroupId, task, resultKey, role, label, running, render, errorCopy = {}, onResult } = options;
  const body = h('div', { class: `${role}-body` }, h('span', { class: 'mini' }, '불러오는 중…'));
  const button = h('button', { type: 'button', class: 'btn ai-btn', 'data-action': `${role}-request` }, `✨ ${label}`);
  const host = h('div', { class: role, 'data-role': role, 'data-product': productGroupId }, button, body);
  let account = options.target ?? null;
  let busy = false;
  const copy = (error) => (error instanceof ApiError ? errorCopy[error.error?.code] ?? error.message : String(error));

  function show(view) {
    const ready = view.ai_capability?.status === 'READY' && account !== null;
    button.dataset.ready = ready ? 'true' : 'false';
    if (ready) {
      delete button.dataset.inert;
      button.removeAttribute('aria-disabled');
    } else if (account === null) {
      markInert(button, `${label} · 연결된 스마트스토어 판매 계정이 없습니다`);
    } else {
      markInert(button, `${label} · ${stateText(view.ai_capability ?? {})}`);
    }
    const result = account ? targetedResult(view, task, resultKey, account) : null;
    body.replaceChildren(account ? render(result) : h('div', { class: 'mini no-data' }, '연결된 스마트스토어 판매 계정이 없습니다'));
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
      const before = targetedResult(await getJson(enrichmentUrl(productGroupId)), task, resultKey, account)?.sequence ?? 0;
      const sent = await sendJson('POST', enrichmentUrl(productGroupId), { actor: OPERATOR, tasks: [task], target: account });
      body.replaceChildren(h('span', { class: 'mini', 'data-role': `${role}-running` }, running));
      for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        const view = await getJson(enrichmentUrl(productGroupId));
        const result = targetedResult(view, task, resultKey, account);
        const newer = (result?.sequence ?? 0) > before;
        if (newer || (await jobSettled(sent.job_id))) {
          show(view);
          toast(newer ? (result.status === 'OK' ? `${label}을 받았습니다` : `${label}에 실패했습니다`) : '입력이 그대로라 이전 추천을 그대로 씁니다');
          return;
        }
      }
      toast(`${label}이 아직 끝나지 않았습니다`, '잠시 후 다시 열어 확인하세요.');
      await read();
    } catch (error) {
      toast(`${label}을 요청하지 못했습니다`, copy(error));
      await read().catch(() => {});
    } finally {
      busy = false;
      button.disabled = false;
    }
  });

  (async () => {
    if (account === null) account = await boundSmartStoreTarget().catch(() => null);
    await read();
  })().catch((error) => {
    markInert(button, `${label} · 상태를 읽지 못했습니다`);
    body.replaceChildren(h('div', { class: 'note' }, `${label}을 읽지 못했습니다 · ${copy(error)}`));
  });
  return host;
}
