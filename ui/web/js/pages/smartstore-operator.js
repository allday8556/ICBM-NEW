// SmartStore operator actions (M2 PR-E; instructions §8A, Issue #44). Every action is an explicit
// operator request to this origin's own API through the one client helper (X-ICBM-Client). After
// each one the page re-reads capability truth from the server (onChanged) and renders that; it
// never promotes anything locally.
//
// First binding is two separate actions (ACCOUNT_IDENTITY §5). 계정 확인 observes the seller
// account the current session reads and shows it. Only the separate, explicitly confirmed
// 이 계정을 연결 대상으로 확정 sends that shown identity to bind, and the server reads the
// account again before it commits. The observed identity lives only in this closure and its
// display: never in storage or the URL, and a reload forgets it.

import { getJson, sendJson } from '../core/api.js';
import { scopeLabel } from '../core/capability.js';
import { h } from '../core/dom.js';
import { toast } from '../core/toast.js';

const BASE = '/api/v1/connect/marketplaces';
const SMARTSTORE = `${BASE}/smartstore`;

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

function errorText(error) {
  const body = error?.error;
  return body?.message ? `${body.message} (${body.code})` : String(error?.message ?? error);
}

// ---------------------------------------------------------------- credentials

export function credentialsPanel(onChanged) {
  const title = '스마트스토어 API 인증';
  const status = h('span', { class: 'chip' }, '확인 중');
  const clientId = h('input', {
    id: 'smartstore-client-id',
    type: 'text',
    autocomplete: 'off',
    spellcheck: 'false',
    placeholder: 'Application ID',
  });
  const secret = h('input', {
    id: 'smartstore-client-secret',
    type: 'password',
    autocomplete: 'new-password',
    placeholder: 'Application Secret',
  });
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-credentials' }, 'API 정보 저장');

  const show = (view) => {
    status.className = view.configured ? 'chip good' : 'chip';
    status.textContent = view.configured ? `저장됨 · 자격증명 세대 ${view.credential_generation}` : '미설정';
  };
  getJson(`${SMARTSTORE}/credentials`)
    .then(show)
    .catch(() => {
      status.className = 'chip warn';
      status.textContent = '확인 불가';
    });

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      // A save is always a whole replacement bundle; the stored secret is never read back.
      const view = await sendJson('PUT', `${SMARTSTORE}/credentials`, {
        client_id: clientId.value,
        client_secret: secret.value,
      });
      show(view);
      toast(title, `새 자격증명 세대 ${view.credential_generation}(으)로 저장했습니다. 이전 세션과 계정 확인은 무효입니다.`);
      await onChanged();
    } catch (error) {
      toast(title, errorText(error));
    } finally {
      secret.value = '';
      save.disabled = false;
    }
  });

  return h(
    'div',
    { class: 'smartstore-credentials' },
    h('div', { class: 'form-row' }, h('label', { for: clientId.id }, '애플리케이션 ID'), clientId),
    h('div', { class: 'form-row' }, h('label', { for: secret.id }, '애플리케이션 Secret'), secret),
    h('div', { class: 'kv' }, h('span', {}, '저장 상태'), status),
    h('div', { class: 'api-action-row' }, save),
  );
}

// ---------------------------------------------------------------- observation, then first binding

function comparison(capability) {
  if (capability.auth === 'READY') return '일치 · 현재 세션으로 확인됨';
  if (capability.auth === 'AUTH_MISMATCH') return '불일치 · 연결 대상은 바뀌지 않습니다 (검토 필요)';
  return '확인 필요';
}

export function accountPanel(onChanged) {
  const observe = h('button', { type: 'button', class: 'btn', 'data-action': 'observe' }, '계정 확인');
  const result = h('div', { class: 'account-observation' });

  const forget = () => result.replaceChildren();

  // The separate committing action: it is only offered for an unbound account, only enabled once
  // the operator ticks the confirmation, and it sends exactly the identity shown above it.
  const confirmation = (shownUid) => {
    const box = h('input', { type: 'checkbox', id: 'smartstore-bind-confirm' });
    const bind = h(
      'button',
      { type: 'button', class: 'btn blue', 'data-action': 'bind' },
      '이 계정을 연결 대상으로 확정',
    );
    bind.disabled = true;
    box.addEventListener('change', () => {
      bind.disabled = !box.checked;
    });
    bind.addEventListener('click', async () => {
      bind.disabled = true;
      box.disabled = true;
      observe.disabled = true;
      try {
        await sendJson('POST', `${SMARTSTORE}/bind`, { confirmed_account_uid: shownUid });
        toast('연결 대상 확정', '확인한 판매자 계정을 연결 대상 계정으로 확정했습니다.');
      } catch (error) {
        toast('연결 대상 확정', errorText(error));
      } finally {
        forget(); // success or failure: any later binding needs a fresh observation
        observe.disabled = false;
        await onChanged();
      }
    });
    return [
      h(
        'label',
        { class: 'kv bind-confirm', for: box.id },
        h('span', {}, '위 판매자 계정을 이 ICBM의 연결 대상 스마트스토어 계정으로 확정합니다'),
        box,
      ),
      h('div', { class: 'api-action-row' }, bind),
    ];
  };

  observe.addEventListener('click', async () => {
    observe.disabled = true;
    forget();
    try {
      const view = await sendJson('POST', `${SMARTSTORE}/connect`);
      result.replaceChildren(
        kv('확인된 판매자 계정 (accountUid)', view.observed_account_uid),
        kv('판매자 계정 ID (accountId)', view.observed_account_id ?? '없음'),
        ...(view.bound
          ? [kv('연결된 계정과 비교', comparison(view.capability))]
          : confirmation(view.observed_account_uid)),
      );
    } catch (error) {
      toast('계정 확인', errorText(error));
    } finally {
      observe.disabled = false;
      await onChanged();
    }
  });

  return h('div', { class: 'smartstore-account' }, h('div', { class: 'api-action-row' }, observe), result);
}

// ---------------------------------------------------------------- contract review record

// The operator's review of ICBM's adopted contract (CAPABILITY_MAPPING F8), never provider
// verification. Nothing returns to UNRECORDED, so it is not offered; the server enforces the rest
// of the closed transition graph.
const REVIEW_OPTIONS = [
  ['CURRENT', '현재 채택 계약과 일치 (ICBM 검토)'],
  ['STALE', '재검토 필요'],
  ['REVIEW_REQUIRED', '계약과 불일치 · 검토 필요'],
];

export function contractReviewPanel(key, onChanged) {
  const title = 'API 계약 검토 기록';
  const select = h(
    'select',
    { id: `contract-review-${key}` },
    REVIEW_OPTIONS.map(([value, label]) => h('option', { value }, label)),
  );
  const save = h('button', { type: 'button', class: 'btn', 'data-action': 'record-freshness' }, '검토 기록 저장');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await sendJson('POST', `${BASE}/${key}/contract-freshness`, { contract_freshness: select.value });
      toast(title, 'ICBM 계약 검토 기록을 남겼습니다. NAVER가 확인한 것이 아닙니다.');
      await onChanged();
    } catch (error) {
      toast(title, errorText(error));
    } finally {
      save.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'contract-review' },
    h('div', { class: 'form-row' }, h('label', { for: select.id }, '검토 결과'), select),
    h('div', { class: 'api-action-row' }, save),
  );
}

// ---------------------------------------------------------------- review / pause resolution

const RESOLUTION_LABEL = {
  REVIEW_RESOLVED: '검토 완료로 기록',
  PROVIDER_REAUTH_COMPLETED: '네이버 재인증 완료로 기록',
  RESUME: '재개',
};

// One row per overlay, beside the PR-D 조치 필요 truth. Only the resolution the server names for
// that overlay is offered; an overlay no operator action lifts (SCOPE_INSUFFICIENT) gets none.
export function workflowActions(key, view, onChanged) {
  if (!view) return [];
  return view.workflow.map((overlay) => {
    const label = `${scopeLabel(overlay.workflow_scope)} 조치`;
    if (!overlay.resolution) {
      return h(
        'div',
        { class: 'kv workflow-action', 'data-scope': overlay.workflow_scope },
        h('span', {}, label),
        h('span', { class: 'mini' }, '등록 권한 확인에서 새 권한 증거를 기록해야 해제됩니다'),
      );
    }
    const button = h(
      'button',
      { type: 'button', class: 'btn', 'data-resolution': overlay.resolution },
      RESOLUTION_LABEL[overlay.resolution] ?? overlay.resolution,
    );
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await sendJson('POST', `${BASE}/${key}/workflow-resolution`, {
          workflow_scope: overlay.workflow_scope,
          resolution: overlay.resolution,
        });
        toast('조치 기록', '조치를 기록했습니다. 연결 상태는 새 증거로만 바뀝니다.');
      } catch (error) {
        toast('조치 기록', errorText(error));
      } finally {
        await onChanged();
      }
    });
    return h('div', { class: 'kv workflow-action', 'data-scope': overlay.workflow_scope }, h('span', {}, label), button);
  });
}
