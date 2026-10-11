// 수집관리 › 공급처 관리 › 「+ 공급처 추가」 (ADR-0036 §4, slice O2).
//
// The v29 supplier credential modal (공급처명 · 도메인 · ID · Password) with the add flow's steps:
//   1. 「플랫폼 확인」 asks the server to guard the domain, detect the platform and read robots.txt
//      and the terms page. The page shows what the server found and decides nothing itself.
//   2. The operator confirms the platform and acknowledges the policy reads; 「저장」 creates the
//      draft supplier, then saves the login through CONNECT's own credential route (the OS secret
//      store). The password is read from its field once, sent once and cleared; it is never shown,
//      kept or logged.
//   3. 「연결 테스트」 runs CONNECT's own connection test; when it passes, the server moves the
//      supplier to 분석 중 (RECON). Site analysis and collection rules stay 준비 중 (O3/O4).
// A robots.txt or terms finding is a warning the seller reads, never a refusal.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { closeModal, openModal } from '../../core/modal.js';
import { toast } from '../../core/toast.js';

export const ONBOARDING = '/api/v1/onboarding';
const CONNECT = '/api/v1/connect/suppliers';

export const ONBOARDING_CHIP = {
  DRAFT: ['초안', null],
  RECON: ['분석 중', 'info'],
  ACTIVE: ['사용 중', 'good'],
  DISABLED: ['비활성', 'bad'],
};

const PLATFORM_LABEL = { cafe24: 'Cafe24', godomall: '고도몰', unsupported: '지원하지 않는 플랫폼' };

const UNSUPPORTED_COPY = {
  HOME_UNREAD: '홈 페이지를 읽지 못했습니다. 도메인을 확인하세요.',
  HOME_REDIRECT: '홈 페이지가 다른 주소로 이동합니다.',
  NO_MARKERS: 'Cafe24·고도몰 템플릿의 표시를 찾지 못했습니다. 이 사이트는 템플릿이 필요합니다.',
  AMBIGUOUS: '두 템플릿의 표시가 함께 보여 플랫폼을 정하지 못했습니다. 직접 선택할 수 있습니다.',
};

const WARNING_COPY = {
  ROBOTS_DISALLOWS_ALL: 'robots.txt가 사이트 전체의 자동 수집을 허용하지 않습니다.',
  ROBOTS_DISALLOWS_PRODUCTS: 'robots.txt가 상품 페이지 경로의 자동 수집을 허용하지 않습니다.',
  ROBOTS_ABSENT: 'robots.txt가 없습니다(404).',
  ROBOTS_UNREAD: 'robots.txt를 읽지 못했습니다.',
  TERMS_UNREAD: '이용약관 페이지를 읽지 못했습니다. 사이트에서 직접 확인하세요.',
};

const ERROR_COPY = {
  ONBOARDING_HOST_REFUSED: '공급처 도메인은 https://와 공개 호스트 이름만 입력합니다(IP·포트·경로 불가).',
  ONBOARDING_ORIGIN_REFUSED: '도메인은 https://로 시작하는 주소로 입력하세요.',
  ONBOARDING_HOST_TAKEN: '이미 등록된 공급처의 도메인입니다.',
  ONBOARDING_NAME_INVALID: '공급처명을 짧은 일반 이름으로 입력하세요.',
  ONBOARDING_DETECTION_UNKNOWN: '플랫폼 확인이 만료되었습니다. 다시 확인하세요.',
  ONBOARDING_DETECTION_MISMATCH: '도메인이나 플랫폼이 확인한 내용과 다릅니다. 다시 확인하세요.',
  ONBOARDING_TEMPLATE_NEEDED: '지원 템플릿이 없는 사이트입니다. 템플릿이 필요합니다.',
  ONBOARDING_POLICY_NOT_ACKNOWLEDGED: 'robots.txt·이용약관 확인에 체크하세요.',
  ONBOARDING_POLICY_UNREAD: 'robots.txt와 이용약관을 모두 읽지 못했습니다. 잠시 후 다시 확인하세요.',
  ONBOARDING_RECON_NOT_READY: '로그인 정보 저장과 연결 테스트 성공 뒤에 사이트 분석으로 넘어갑니다.',
};

let sequence = 0;

function field(label, attrs = {}) {
  sequence += 1;
  const id = `onboarding-field-${sequence}`;
  const input = h('input', { id, type: 'text', autocomplete: 'off', ...attrs });
  return { input, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), input) };
}

function serverMessage(error, fallback) {
  const code = error?.error?.code;
  return ERROR_COPY[code] ?? fallback?.(error) ?? error?.error?.message ?? String(error?.message ?? error);
}

export async function loadOnboarding() {
  try {
    const records = await getJson(`${ONBOARDING}/suppliers`);
    return new Map(records.map((record) => [record.supplier_key, record]));
  } catch {
    return new Map();
  }
}

export function onboardingChip(record) {
  const [label, tone] = ONBOARDING_CHIP[record.state] ?? [record.state, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-onboarding-state': record.state }, label);
}

// DRAFT → RECON, once CONNECT's test passed. The server checks every condition itself.
export async function startRecon(key) {
  return sendJson('POST', `${ONBOARDING}/suppliers/${key}/recon`);
}

function statusText(document) {
  if (document.status === null) return '읽지 못함';
  if (document.status === 404) return '없음 (404)';
  return `HTTP ${document.status}`;
}

function policyBlock(document) {
  const title = document.document === 'ROBOTS' ? 'robots.txt' : '이용약관';
  return h(
    'div',
    { class: 'onboarding-policy-doc', 'data-policy': document.document },
    h(
      'div',
      { class: 'kv' },
      h('span', {}, title),
      h(
        'span',
        { class: 'onboarding-policy-status' },
        h('b', {}, statusText(document)),
        h('a', { href: document.url, target: '_blank', rel: 'noopener noreferrer' }, '열기'),
      ),
    ),
    document.warnings.map((code) =>
      h('div', { class: 'note', 'data-warning': code }, h('span', { class: 'chip warn' }, '주의'), ' ', WARNING_COPY[code] ?? code),
    ),
    document.robots?.lines?.length
      ? h('pre', { class: 'onboarding-robots', 'data-role': 'robots-lines' }, document.robots.lines.join('\n'))
      : null,
    document.robots?.crawl_delay ? h('div', { class: 'mini' }, `Crawl-delay: ${document.robots.crawl_delay}`) : null,
  );
}

export function openAddSupplier(ctx, { waitForJob, errorMessage }) {
  const name = field('공급처명', { placeholder: '공급처 이름', maxlength: '64' });
  const domain = field('도메인', { placeholder: 'https:// 로 시작하는 쇼핑몰 주소', inputmode: 'url' });
  const username = field('ID', { placeholder: '공급처 로그인 ID' });
  sequence += 1;
  const passwordId = `onboarding-field-${sequence}`;
  const password = h('input', { id: passwordId, type: 'password', autocomplete: 'new-password', placeholder: '공급처 비밀번호' });
  const passwordRow = h('div', { class: 'form-row' }, h('label', { for: passwordId }, 'Password'), password);

  const state = { detection: null, key: null, saved: false, busy: false };
  const result = h('div', { class: 'onboarding-detect-result', 'data-role': 'detection' });
  const status = h('div', { class: 'note', 'data-role': 'onboarding-status', hidden: true });
  const detectButton = h('button', { type: 'button', class: 'btn', 'data-action': 'detect-platform' }, '플랫폼 확인');
  const ack = h('input', { type: 'checkbox', 'data-role': 'policy-ack', disabled: true });
  const ackRow = h(
    'label',
    { class: 'onboarding-ack' },
    ack,
    h('span', {}, 'robots.txt와 이용약관 확인 결과를 읽었습니다. 수집 진행 여부와 그 책임은 판매자가 판단합니다.'),
  );
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-supplier', disabled: true }, '저장');
  const test = h('button', { type: 'button', class: 'btn', 'data-action': 'test-supplier', disabled: true }, '연결 테스트');

  const say = (text) => {
    status.hidden = !text;
    status.textContent = text ?? '';
  };
  const refreshButtons = () => {
    const d = state.detection;
    const confirmable = Boolean(d?.policy_template);
    ack.disabled = !confirmable || state.saved;
    save.disabled = state.busy || state.saved || !confirmable || !ack.checked;
    test.disabled = state.busy || !state.saved;
    detectButton.disabled = state.busy || Boolean(state.key);
  };

  const render = (d) => {
    const confirmed = d.policy_template;
    const select = h(
      'select',
      { 'data-role': 'platform-choice', 'aria-label': '플랫폼', disabled: Boolean(state.key) },
      h('option', { value: '' }, confirmed ? `${PLATFORM_LABEL[confirmed]} (확인됨)` : '선택'),
      d.templates.filter((t) => t !== confirmed).map((t) => h('option', { value: t }, `${PLATFORM_LABEL[t] ?? t}로 직접 선택`)),
    );
    select.addEventListener('change', () => {
      if (select.value) detectNow(select.value);
    });
    const detected = d.detected;
    const tone = detected === 'unsupported' ? 'bad' : 'good';
    const markers = d.evidence.filter((e) => e.platform === detected).flatMap((e) => [...e.home_markers, ...e.login_markers]);
    result.replaceChildren(
      h(
        'div',
        { class: 'kv' },
        h('span', {}, '감지된 플랫폼'),
        h('span', { class: `chip ${tone}`, 'data-detected': detected }, PLATFORM_LABEL[detected] ?? detected),
      ),
      markers.length ? h('div', { class: 'mini onboarding-evidence' }, `근거: ${markers.join(', ')}`) : null,
      detected === 'unsupported' ? h('div', { class: 'note', 'data-role': 'unsupported' }, UNSUPPORTED_COPY[d.reason] ?? '템플릿이 필요합니다.') : null,
      d.suggested_origin
        ? h(
            'button',
            {
              type: 'button',
              class: 'btn',
              'data-action': 'use-suggested-origin',
              onclick: () => {
                domain.input.value = d.suggested_origin;
                detectNow(null);
              },
            },
            `${d.suggested_origin}로 다시 확인`,
          )
        : null,
      h('div', { class: 'form-row' }, h('label', {}, '플랫폼'), select),
      d.chosen ? h('div', { class: 'mini' }, `운영자 선택: ${PLATFORM_LABEL[d.chosen] ?? d.chosen} (감지 결과와 다를 수 있습니다)`) : null,
      d.policy.length
        ? h('div', { class: 'onboarding-policy', 'data-role': 'policy' }, d.policy.map(policyBlock))
        : null,
      confirmed ? ackRow : null,
    );
    refreshButtons();
  };

  async function detectNow(template) {
    const origin = domain.input.value.trim();
    if (!origin) {
      say('도메인을 입력하세요.');
      return;
    }
    state.busy = true;
    state.detection = null;
    ack.checked = false;
    refreshButtons();
    say('공급처 홈·로그인 페이지와 robots.txt·이용약관을 한 번씩 읽는 중입니다…');
    try {
      const body = { origin };
      if (template) body.template = template;
      state.detection = await sendJson('POST', `${ONBOARDING}/detect`, body);
      say(null);
      render(state.detection);
    } catch (error) {
      result.replaceChildren();
      say(serverMessage(error, errorMessage));
    } finally {
      state.busy = false;
      refreshButtons();
    }
  }

  detectButton.addEventListener('click', () => detectNow(null));
  ack.addEventListener('change', refreshButtons);
  domain.input.addEventListener('input', () => {
    if (state.key) return;
    state.detection = null;
    result.replaceChildren();
    refreshButtons();
  });

  save.addEventListener('click', async () => {
    const d = state.detection;
    const secret = password.value;
    const login = username.input.value.trim();
    if (!name.input.value.trim()) return say('공급처명을 입력하세요.');
    if (!login) return say('ID를 입력하세요.');
    if (!secret) return say('비밀번호를 입력하세요.');
    password.value = '';
    state.busy = true;
    refreshButtons();
    try {
      if (!state.key) {
        const created = await sendJson('POST', `${ONBOARDING}/suppliers`, {
          name: name.input.value.trim(),
          origin: d.origin,
          template: d.policy_template,
          detection_id: d.detection_id,
          acknowledged: ack.checked,
        });
        state.key = created.supplier_key;
        name.input.readOnly = true;
        domain.input.readOnly = true;
      }
      await sendJson('PUT', `${CONNECT}/${state.key}/credentials`, { username: login, password: secret });
      state.saved = true;
      say('초안으로 저장했습니다. 로그인 정보는 OS 보안 저장소에만 보관됩니다. 연결 테스트를 진행하세요.');
    } catch (error) {
      say(serverMessage(error, errorMessage));
    } finally {
      state.busy = false;
      refreshButtons();
    }
  });

  test.addEventListener('click', async () => {
    state.busy = true;
    refreshButtons();
    say('저장된 로그인으로 보호 페이지를 확인하는 중입니다…');
    try {
      const queued = await sendJson('POST', `${CONNECT}/${state.key}/test`);
      const job = await waitForJob(queued.job_id);
      if (job?.state !== 'SUCCEEDED') {
        say(job ? `연결 테스트 실패: ${errorMessage({ error: { code: job.last_error_code } })}` : '연결 테스트 결과를 받지 못했습니다.');
        return;
      }
      await startRecon(state.key);
      toast('공급처 추가', '연결이 확인되어 분석 중으로 전환했습니다. 사이트 분석은 다음 단계에서 열립니다.');
      closeModal();
      ctx.navigate('collect', { view: 'suppliers' });
    } catch (error) {
      say(serverMessage(error, errorMessage));
    } finally {
      state.busy = false;
      refreshButtons();
    }
  });

  openModal({
    narrow: true,
    eyebrow: 'CONNECT · Supplier Onboarding',
    title: '새 공급처 추가',
    help: '도메인과 로그인 정보를 등록한 뒤 연결 테스트와 사이트 분석을 진행합니다. 저장된 비밀번호는 다시 표시하지 않습니다.',
    body: h(
      'div',
      { class: 'editor-card', 'data-role': 'onboarding-form' },
      name.row,
      domain.row,
      h('div', { class: 'onboarding-detect' }, detectButton, h('span', { class: 'mini' }, '홈·로그인 페이지와 robots.txt·이용약관을 한 번씩 읽어 플랫폼을 확인합니다.')),
      result,
      username.row,
      passwordRow,
      status,
      h('div', { class: 'note' }, '비밀번호와 로그인 세션은 OS 보안 저장소와 암호화 파일에만 보관되며 화면·로그·DB에 남지 않습니다.'),
    ),
    footer: [test, save],
  });
  refreshButtons();
}
