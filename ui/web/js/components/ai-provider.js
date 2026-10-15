// Settings › AI / Prompt › AI 공급자 (ADR-0027 §7; AIS-1).
//
// The operator's CLIProxyAPI profile and its approvals, as the server holds them (GET
// /api/v1/ai/provider). The executable and routing approvals name what is actually serving now,
// and each is a protected, audited server action; the page computes no approval itself. The
// ICBM-dedicated client key is typed once into a password field and written to the OS secret store
// by the server (ADR-0012 §2); it is never shown again, and the field is cleared after the save.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { errorText } from '../core/errors.js';
import { toast } from '../core/toast.js';

const ENDPOINT = '/api/v1/ai/provider';

const DETAIL_COPY = {
  'no AI provider is configured': 'AI 공급자가 설정되지 않았습니다',
  AI_EXECUTABLE_NOT_SERVING: '승인한 실행 파일이 지금 실행 중이 아닙니다',
  AI_EXECUTABLE_MISMATCH: '지금 실행 중인 파일이 승인한 파일과 다릅니다',
  AI_ROUTING_MISMATCH: '지금 라우팅 설정이 승인한 설정과 다릅니다',
  AI_ROUTING_UNREADABLE: 'CLIProxyAPI를 -config <설정 파일의 절대 경로>로 실행해야 설정을 확인할 수 있습니다',
  AI_DAILY_CAP_REACHED: '오늘 호출 상한에 도달했습니다',
  AI_PROFILE_UNREADABLE: 'AI 공급자 설정을 읽을 수 없습니다',
};
const RUNTIME_LABEL = {
  NOT_CONFIGURED: '설정이 끝나지 않음',
  AVAILABLE: '사용 가능',
  UNAVAILABLE: '실행 중이 아니거나 설정을 읽을 수 없음',
  VERSION_MISMATCH: '승인한 파일과 다른 프로그램이 실행 중',
  ROUTING_CONFIG_MISMATCH: '승인한 연결 설정과 다름',
};
const BILLING_LABEL = { SUBSCRIPTION: '구독', METERED_API: '사용량 과금', UNKNOWN: '모름' };
const MISSING_COPY = { model: '모델', executable: '실행 파일', routing: '라우팅', data_transfer: '상품 정보 전송', credential: '접속 키' };
const ERROR_COPY = {
  AI_PROFILE_INVALID: '주소는 http://127.0.0.1:<포트> 형식의 내 PC 주소만 쓸 수 있습니다.',
  AI_PROFILE_CURRENT_MOVED: '다른 곳에서 먼저 바뀌었습니다. 다시 불러온 뒤 저장하세요.',
  AI_EXECUTABLE_MISMATCH: '지금 실행 중인 파일만 승인할 수 있습니다. 새로고침 후 다시 확인하세요.',
  AI_ROUTING_MISMATCH: '지금 라우팅 설정만 승인할 수 있습니다. 새로고침 후 다시 확인하세요.',
  AI_ROUTING_UPDATES_ON: 'CLIProxyAPI를 -config <절대 경로>와 -local-model 옵션, disable-auto-update-panel: true 설정으로 실행해야 승인할 수 있습니다.',
  AI_PROFILE_MISSING: '먼저 공급자 설정을 저장하세요.',
  AI_MODEL_NOT_APPROVED: '모델은 소유자가 정한 gpt-5.6-sol만 쓸 수 있습니다.',
  AI_CREDENTIAL_INVALID: '접속 키는 공백 없는 영문·숫자·기호 16~200자입니다.',
};

// The capability's state for a button outside 설정 › AI: one plain line that points there.
export function sellerStateText(capability) {
  if (capability.status === 'READY') return '연결됨';
  if (capability.detail === 'no AI provider is configured') return 'AI 공급자가 설정되지 않았습니다 (설정 › AI)';
  return 'AI 설정을 확인해야 합니다 (설정 › AI)';
}

export function stateText(capability) {
  if (capability.status === 'READY') return '연결됨';
  const detail = capability.detail ?? '';
  if (detail.startsWith('not approved: ')) {
    return `승인 필요 · ${detail.slice(14).split(', ').map((key) => MISSING_COPY[key] ?? key).join(', ')}`;
  }
  return DETAIL_COPY[detail] ?? '확인 필요';
}


function errorCopy(error) {
  return errorText(error, ERROR_COPY);
}

function row(label, ...value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, ...value));
}

export function aiProviderPanel() {
  const host = h('div', { class: 'ai-provider', 'data-role': 'ai-provider' }, h('span', { class: 'mini' }, '불러오는 중…'));

  async function send(path, payload, done) {
    try {
      await sendJson('POST', path, payload);
      toast(done);
    } catch (error) {
      toast('저장하지 못했습니다', errorCopy(error));
    }
    draw();
  }

  async function draw() {
    let view;
    // ADR-0028 §3: the search_signal capability, as readiness reports it.
    const signalState = h('span', { 'data-role': 'search-signal-state' }, '확인 중…');
    getJson('/api/ready')
      .then((ready) => {
        const signal = (ready.capabilities ?? []).find((capability) => capability.key === 'search_signal');
        signalState.dataset.status = signal?.status ?? 'UNKNOWN';
        signalState.textContent = !signal ? '확인할 수 없음' : signal.status === 'READY' ? '연결됨' : '연결된 출처 없음';
      })
      .catch(() => {
        signalState.textContent = '확인할 수 없음';
      });
    try {
      view = await getJson(ENDPOINT);
    } catch (error) {
      host.replaceChildren(h('div', { class: 'note' }, `AI 공급자 상태를 읽지 못했습니다 · ${errorCopy(error)}`));
      return;
    }
    const content = view.content ?? {};
    const observed = view.observed;
    const revision = view.current_revision;
    const ready = view.capability.status === 'READY';
    const field = (name, value, type = 'text') => h('input', { name, type, value: value ?? '', class: 'field' });
    const endpoint = field('endpoint', content.endpoint ?? 'http://127.0.0.1:8317');
    // The owner's model (ADR-0027 AIS-09): shown, never chosen here.
    const model = field('requested_model', content.requested_model ?? 'gpt-5.6-sol');
    model.readOnly = true;
    const cap = field('daily_call_cap', content.daily_call_cap ?? 200, 'number');
    const billing = h(
      'select',
      { name: 'billing_mode', class: 'field' },
      ...['SUBSCRIPTION', 'METERED_API', 'UNKNOWN'].map((mode) =>
        h('option', { value: mode, selected: (content.billing_mode ?? 'SUBSCRIPTION') === mode }, BILLING_LABEL[mode]),
      ),
    );
    const approvedExe = content.approved_executable;
    const approvedRouting = content.approved_routing;
    // The served executable is the approved one only at the same path and with the same bytes.
    const exeMatches = Boolean(
      approvedExe && observed?.sha256 === approvedExe.sha256 && observed?.path?.toLowerCase() === approvedExe.path?.toLowerCase(),
    );
    const routingMatches = Boolean(approvedRouting && observed?.routing_fingerprint === approvedRouting.fingerprint);
    host.replaceChildren(
      h(
        'div',
        { class: 'kv' },
        h('span', {}, '상태'),
        h('span', { class: `chip ${ready ? 'good' : 'warn'}`, 'data-role': 'ai-provider-state', 'data-status': view.capability.status }, stateText(view.capability)),
      ),
      row('실행 상태', h('span', { 'data-role': 'ai-provider-runtime', 'data-runtime-state': view.runtime_state }, RUNTIME_LABEL[view.runtime_state] ?? '확인 필요')),
      row('오늘 호출', `${view.calls_today} / ${content.daily_call_cap ?? '—'}`),
      // ADR-0028 §3: the SearchSignal port, provider-zero until a source has its own owner decision.
      row('외부 검색 신호', signalState),
      h(
        'form',
        {
          class: 'ai-provider-form',
          'data-role': 'ai-provider-form',
          onsubmit: (event) => {
            event.preventDefault();
            send(
              `${ENDPOINT}/profile`,
              {
                actor: 'operator',
                expected_current_revision: revision,
                endpoint: endpoint.value.trim(),
                requested_model: model.value.trim(),
                billing_mode: billing.value,
                daily_call_cap: Number(cap.value),
              },
              '공급자 설정을 저장했습니다',
            );
          },
        },
        h('label', { class: 'kv' }, h('span', {}, '주소 (내 PC)'), endpoint),
        h('label', { class: 'kv' }, h('span', {}, '모델'), model),
        h('label', { class: 'kv' }, h('span', {}, '과금 방식'), billing),
        h('label', { class: 'kv' }, h('span', {}, '하루 호출 상한'), cap),
        h('div', { class: 'note' }, '주소나 모델을 바꾸면 상품 정보 전송을 다시 승인해야 합니다.'),
        h('button', { type: 'submit', class: 'btn blue', 'data-action': 'ai-provider-save' }, '공급자 설정 저장'),
      ),
      h('h4', {}, '접속 키'),
      row('상태', content.credential_set_at ? `저장됨 · ${content.credential_set_at.slice(0, 16).replace('T', ' ')}` : '없음'),
      h(
        'form',
        {
          class: 'ai-provider-form',
          'data-role': 'ai-credential-form',
          onsubmit: (event) => {
            event.preventDefault();
            const input = event.currentTarget.querySelector('input[name=client_key]');
            const key = input.value;
            input.value = '';
            send(`${ENDPOINT}/credential`, { actor: 'operator', expected_current_revision: revision, key }, '접속 키를 저장했습니다');
          },
        },
        h('label', { class: 'kv' }, h('span', {}, 'ICBM 전용 접속 키'), h('input', { name: 'client_key', type: 'password', autocomplete: 'off', class: 'field', disabled: !revision })),
        h('div', { class: 'note' }, 'CLIProxyAPI 설정 파일의 api-keys에 넣은 ICBM 전용 키입니다. 이 PC에 안전하게 저장되며 다시 보여주지 않습니다.'),
        h('button', { type: 'submit', class: 'btn', 'data-action': 'ai-credential-save', disabled: !revision }, '접속 키 저장'),
      ),
      h('h4', {}, '실행 파일'),
      row('승인됨', approvedExe ? h('span', { title: approvedExe.sha256, 'data-sha256': approvedExe.sha256 }, approvedExe.path) : '없음'),
      row('지금 실행 중', observed?.serving ? h('span', { title: observed.sha256, 'data-sha256': observed.sha256 }, observed.path) : '실행 중이 아님'),
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'ai-approve-executable',
          disabled: !revision || !observed?.serving || exeMatches,
          onclick: () => send(`${ENDPOINT}/approve-executable`, { actor: 'operator', expected_current_revision: revision, observed: observed.sha256, observed_path: observed.path }, '실행 파일을 승인했습니다'),
        },
        exeMatches ? '승인한 파일이 실행 중' : '지금 실행 중인 파일 승인',
      ),
      h('h4', {}, '연결 설정 (라우팅)'),
      row('승인됨', approvedRouting ? h('span', { 'data-fingerprint': approvedRouting.fingerprint }, routingMatches ? '승인한 설정으로 실행 중' : '승인됨') : '없음'),
      row(
        '지금 설정',
        observed?.routing_fingerprint
          ? h(
              'span',
              { 'data-fingerprint': observed.routing_fingerprint },
              `읽음 · 모델 목록 고정 ${observed.local_model ? '켜짐' : '꺼짐'} · 자동 업데이트 ${observed.panel_auto_update_disabled ? '꺼짐' : '켜짐'}`,
            )
          : '읽을 수 없음',
      ),
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'ai-approve-routing',
          disabled: !revision || !observed?.routing_fingerprint || routingMatches,
          onclick: () => send(`${ENDPOINT}/approve-routing`, { actor: 'operator', expected_current_revision: revision, observed: observed.routing_fingerprint }, '라우팅을 승인했습니다'),
        },
        routingMatches ? '승인한 라우팅으로 실행 중' : '지금 라우팅 승인',
      ),
      h('h4', {}, '상품 정보 전송'),
      row('승인', content.data_transfer_approved ? '승인됨' : '승인 안 됨'),
      h(
        'button',
        {
          type: 'button',
          class: content.data_transfer_approved ? 'btn' : 'btn blue',
          'data-action': 'ai-data-transfer',
          disabled: !revision,
          onclick: () =>
            send(
              `${ENDPOINT}/data-transfer`,
              { actor: 'operator', expected_current_revision: revision, approved: !content.data_transfer_approved },
              content.data_transfer_approved ? '전송 승인을 거뒀습니다' : '상품 정보 전송을 승인했습니다',
            ),
        },
        content.data_transfer_approved ? '전송 승인 거두기' : '상품 정보를 이 공급자로 보내는 것을 승인',
      ),
    );
  }

  draw();
  return host;
}
