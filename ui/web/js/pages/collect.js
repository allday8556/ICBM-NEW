// 수집관리: 수집 / 공급처 관리 views. The supplier view renders canonical SupplierConnection
// state (M1 CONNECT) in the v28/v29 supplier-card shape: login, connection test and resume are
// live; site analysis and collection rules arrive with COLLECT (M3) and stay inert.
// The UI never decides a supplier is connected — it shows the server's protected-read verdict.

import { getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDate, dotDateTime } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { closeModal, openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';
import { datePill, pageHead } from '../components/page-head.js';
import { emptyState, unsupportedState } from '../components/states.js';

const ENDPOINT = '/api/v1/screens/collect';
const CONNECT = '/api/v1/connect/suppliers';
const TITLE = '수집관리';
const VIEWS = [
  ['jobs', '수집'],
  ['suppliers', '공급처 관리'],
];
const JOB_POLL_MS = 500;
const JOB_POLL_LIMIT = 120;

const CAPABILITY_CHIP = {
  READY: ['로그인 성공', 'good'],
  NOT_CONFIGURED: ['로그인 정보 필요', null],
  DISCONNECTED: ['로그인 확인 필요', 'warn'],
  CONNECTING: ['연결 확인 중', 'info'],
  AUTH_EXPIRED: ['세션 만료', 'warn'],
  DEGRADED: ['연결 오류', 'bad'],
  PAUSED: ['자동 로그인 중지', 'bad'],
};

const AUTH_LABEL = {
  NOT_CONFIGURED: '미연결',
  CREDENTIALS_STORED: '자격증명 저장됨',
  AUTHENTICATING: '인증 확인 중',
  AUTHENTICATED: '인증됨 · 보호 페이지 확인',
  AUTH_EXPIRED: '세션 만료',
  PAUSED: '일시 중지 · 재개 필요',
};

const ERROR_COPY = {
  SUPPLIER_LOGIN_REJECTED: '공급처가 로그인 정보를 거부했습니다. ID와 비밀번호를 확인하세요.',
  SUPPLIER_LOGIN_NOT_EFFECTIVE: '로그인은 제출됐지만 보호 페이지에서 인증이 확인되지 않았습니다.',
  SUPPLIER_AUTH_PAUSED: '연속 로그인 실패로 자동 로그인을 멈췄습니다. 정보를 확인한 뒤 재개하세요.',
  SUPPLIER_SESSION_EXPIRED: '저장된 세션이 만료되었습니다. 다음 확인 때 한 번만 재인증합니다.',
  SUPPLIER_TARGET_NOT_AUTH_GATED: '확인 대상 페이지가 비로그인에도 열려 인증 증거가 되지 않습니다.',
  SUPPLIER_PROTECTED_READ_UNRECOGNIZED: '보호 페이지를 판별하지 못했습니다(점검 중이거나 구조 변경).',
  SUPPLIER_TIMEOUT: '공급처 응답이 늦어 확인하지 못했습니다.',
  SUPPLIER_NETWORK_ERROR: '공급처에 연결하지 못했습니다.',
  SUPPLIER_LOGIN_TIMEOUT: '로그인이 제한 시간 안에 끝나지 않았습니다.',
  SUPPLIER_BROWSER_ERROR: '로그인 브라우저를 실행하지 못했습니다.',
};

let fieldSequence = 0;

function field(label, attrs = {}) {
  fieldSequence += 1;
  const id = `supplier-field-${fieldSequence}`;
  const input = h('input', { id, type: 'text', autocomplete: 'off', ...attrs });
  return { input, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), input) };
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

function errorMessage(error) {
  const code = error?.error?.code;
  return ERROR_COPY[code] ?? error?.error?.message ?? String(error?.message ?? error);
}

async function waitForJob(jobId) {
  for (let i = 0; i < JOB_POLL_LIMIT; i += 1) {
    const job = await getJson(`/api/v1/system/jobs/${jobId}`);
    if (['SUCCEEDED', 'DEAD', 'RETRY_SCHEDULED'].includes(job.state)) return job;
    await new Promise((resolve) => window.setTimeout(resolve, JOB_POLL_MS));
  }
  return null;
}

async function runConnectionTest(supplier, ctx) {
  try {
    const queued = await sendJson('POST', `${CONNECT}/${supplier.supplier_key}/test`);
    toast('연결 테스트', '저장된 세션을 먼저 재사용하고, 필요할 때만 한 번 로그인합니다.');
    const job = await waitForJob(queued.job_id);
    if (job?.state === 'SUCCEEDED') toast('연결 테스트', '보호 페이지 확인 완료 · 연결됨');
    else if (job?.state === 'RETRY_SCHEDULED') toast('연결 테스트', '일시적 오류 · 잠시 후 자동으로 한 번 더 확인합니다.');
    else if (job) toast('연결 테스트', ERROR_COPY[job.last_error_code] ?? job.last_error_code ?? '실패');
  } catch (error) {
    toast('연결 테스트', errorMessage(error));
  }
  ctx.navigate('collect', { view: 'suppliers' });
}

async function resume(supplier, ctx) {
  try {
    await sendJson('POST', `${CONNECT}/${supplier.supplier_key}/resume`);
    toast('자동 로그인 재개', '다음 연결 테스트부터 인증을 다시 시도합니다.');
  } catch (error) {
    toast('자동 로그인 재개', errorMessage(error));
  }
  ctx.navigate('collect', { view: 'suppliers' });
}

function autoConnectToggle(supplier) {
  let enabled = supplier.auto_connect;
  const control = h('button', {
    type: 'button',
    role: 'switch',
    class: enabled ? 'toggle' : 'toggle off',
    'aria-checked': String(enabled),
    'aria-label': '자동 연결',
    disabled: !supplier.connection_id,
  });
  control.addEventListener('click', async () => {
    try {
      const updated = await sendJson('PUT', `${CONNECT}/${supplier.supplier_key}/auto-connect`, { enabled: !enabled });
      enabled = updated.auto_connect;
      control.className = enabled ? 'toggle' : 'toggle off';
      control.setAttribute('aria-checked', String(enabled));
    } catch (error) {
      toast('자동 연결', errorMessage(error));
    }
  });
  return control;
}

// The stored password is only ever represented by this UI-only indicator. It is not an input
// value, is never sent, and the server rejects it if it ever arrives as a password.
const STORED_PASSWORD_MASK = '•••••••• · 저장됨';

async function storedLogin(supplier) {
  try {
    return await getJson(`${CONNECT}/${supplier.supplier_key}/credentials`);
  } catch {
    return { username: null, password_stored: false };
  }
}

function passwordControl(passwordStored) {
  // Until the operator chooses to change it, the stored password is left untouched.
  const state = { changing: !passwordStored, input: null };
  const row = h('div', { class: 'form-row' });
  const render = () => {
    fieldSequence += 1;
    const id = `supplier-field-${fieldSequence}`;
    state.input = state.changing
      ? h('input', { id, type: 'password', autocomplete: 'new-password', placeholder: passwordStored ? '새 비밀번호' : '공급처 비밀번호' })
      : null;
    const control = state.changing
      ? [
          state.input,
          passwordStored
            ? h('button', { type: 'button', class: 'btn', onclick: () => { state.changing = false; render(); } }, '변경 취소')
            : null,
        ]
      : [
          h('span', { class: 'chip good', 'data-password-state': 'stored' }, STORED_PASSWORD_MASK),
          h('button', { type: 'button', class: 'btn', onclick: () => { state.changing = true; render(); state.input?.focus(); } }, '비밀번호 변경'),
        ];
    row.replaceChildren(h('label', { for: id }, 'Password'), h('div', { class: 'password-control' }, control));
  };
  render();
  return { row, state };
}

async function openCredentialModal(supplier, ctx) {
  const saved = await storedLogin(supplier);
  const stored = saved.password_stored;
  const username = field('ID', { value: saved.username ?? '', placeholder: '공급처 로그인 ID' });
  const password = passwordControl(stored);
  const save = async () => {
    const values = { username: username.input.value.trim() };
    if (password.state.changing) {
      const replacement = password.state.input?.value ?? '';
      if (password.state.input) password.state.input.value = '';
      if (!replacement) {
        toast('로그인 정보', stored ? '새 비밀번호를 입력하거나 변경을 취소하세요.' : '비밀번호를 입력하세요.');
        return;
      }
      values.password = replacement;
    }
    if (!values.username) {
      toast('로그인 정보', 'ID를 입력하세요.');
      return;
    }
    try {
      await sendJson('PUT', `${CONNECT}/${supplier.supplier_key}/credentials`, values);
      toast('로그인 정보', 'OS 보안 저장소에 저장했습니다. 연결 테스트로 확인하세요.');
      closeModal();
      ctx.navigate('collect', { view: 'suppliers' });
    } catch (error) {
      toast('로그인 정보', errorMessage(error));
    }
  };
  const test = h('button', { type: 'button', class: 'btn', disabled: !stored }, '연결 테스트');
  test.addEventListener('click', () => {
    closeModal();
    runConnectionTest(supplier, ctx);
  });
  const budget =
    `로그인 ${supplier.real_login_attempts}회 · 세션 재사용 ${supplier.session_reuse_count}회 · ` +
    `재인증 ${supplier.reauth_count}회 · 연속 실패 ${supplier.consecutive_auth_failures}/${supplier.auth_retry_limit}`;
  openModal({
    narrow: true,
    eyebrow: 'CONNECT · Supplier Credential',
    title: '공급처 로그인 정보',
    help: '저장된 비밀번호는 화면에 다시 표시하지 않고 저장 여부만 표시합니다.',
    body: h(
      'div',
      { class: 'editor-card' },
      field('공급처명', { value: supplier.display_name, readonly: true, 'aria-readonly': 'true' }).row,
      field('도메인', { value: supplier.base_url, readonly: true, 'aria-readonly': 'true' }).row,
      username.row,
      password.row,
      h('div', { class: 'kv' }, h('span', {}, '기존 자격증명'), h('span', { class: stored ? 'chip good' : 'chip' }, stored ? '저장됨' : '미저장')),
      h('div', { class: 'kv' }, withHelp(h('span', {}, '자동 연결'), '필요할 때 저장된 세션을 먼저 재사용하고, 만료된 경우에만 한 번 재인증합니다. 앱 시작 시에는 로그인하지 않습니다.'), autoConnectToggle(supplier)),
      kv('로그인 기록', budget),
      h('div', { class: 'note' }, '비밀번호와 로그인 세션은 OS 보안 저장소와 암호화 파일에만 보관되며 화면·로그·DB에 남지 않습니다.'),
    ),
    footer: [test, h('button', { type: 'button', class: 'btn blue', onclick: save }, '저장')],
  });
}

function supplierCard(supplier, ctx) {
  const [chipLabel, tone] = CAPABILITY_CHIP[supplier.capability_status] ?? [supplier.capability_status, null];
  const failing = supplier.state !== 'READY' && supplier.last_error_code;
  const primary =
    supplier.state === 'PAUSED'
      ? h('button', { type: 'button', class: 'btn blue', onclick: () => resume(supplier, ctx) }, '자동 로그인 재개')
      : h(
          'button',
          { type: 'button', class: 'btn', disabled: !supplier.credentials_stored, onclick: () => runConnectionTest(supplier, ctx) },
          '연결 테스트',
        );
  return h(
    'div',
    { class: 'supplier-card', 'data-supplier': supplier.supplier_key },
    h(
      'div',
      { class: 'supplier-card-head' },
      h('div', {}, h('b', {}, supplier.display_name), h('div', { class: 'supplier-domain' }, supplier.base_url)),
      h('span', { class: tone ? `chip ${tone}` : 'chip' }, chipLabel),
    ),
    h(
      'div',
      { class: 'supplier-status-grid' },
      kv('인증 상태', AUTH_LABEL[supplier.auth_state] ?? supplier.auth_state),
      kv('수집 프로필', '분석 전 · M3'),
      kv('마지막 확인', supplier.last_verified_at ? dotDateTime(supplier.last_verified_at) : '-'),
      kv('마지막 수집', '-'),
    ),
    failing ? h('div', { class: 'note' }, ERROR_COPY[supplier.last_error_code] ?? supplier.last_error_code) : null,
    h(
      'div',
      { class: 'supplier-actions' },
      h('button', { type: 'button', class: 'btn', onclick: () => openCredentialModal(supplier, ctx) }, '로그인 정보'),
      primary,
      markInert(h('button', { type: 'button', class: 'btn' }, '사이트 분석')),
      markInert(h('button', { type: 'button', class: 'btn' }, '수집 규칙')),
    ),
  );
}

function jobsView(view, ctx) {
  if (view.meta.state !== 'EMPTY') return unsupportedState(view.meta);
  return emptyState({
    title: '아직 수집 작업이 없습니다',
    copy: '공급처를 연결한 뒤 첫 상품을 수집하세요.',
    // Collection needs a connected supplier first, so the CTA leads to supplier management.
    action: { label: '첫 상품 수집하기', onSelect: () => ctx.navigate('collect', { view: 'suppliers' }) },
  });
}

function suppliersView(view, ctx) {
  const header = h(
    'div',
    { class: 'panel supplier-head' },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(
        h('h3', { class: 'panel-title' }, '공급처 연결 관리'),
        '공급처 인증·세션·수집 프로필을 한 곳에서 확인합니다. 저장된 비밀번호는 다시 표시하지 않습니다.',
      ),
      markInert(h('button', { type: 'button', class: 'btn blue' }, '+ 공급처 추가')),
    ),
  );
  // New suppliers need their own site definition; M1 ships KM통상 only.
  const addCard = markInert(
    h(
      'button',
      { type: 'button', class: 'supplier-card supplier-add-card' },
      h(
        'div',
        {},
        h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '＋'),
        h('b', {}, '새 공급처 추가'),
        h('div', { class: 'mini' }, '도메인과 로그인 정보를 등록한 뒤 연결 테스트와 사이트 분석을 진행합니다.'),
      ),
    ),
    '새 공급처 추가',
  );
  return fragment(header, h('div', { class: 'supplier-grid' }, view.suppliers.map((s) => supplierCard(s, ctx)), addCard));
}

export default {
  key: 'collect',
  title: TITLE,
  navLabel: TITLE,
  icon: '◌',
  async render(ctx) {
    const view = await getJson(ENDPOINT);
    const active = ctx.params.get('view') === 'suppliers' ? 'suppliers' : 'jobs';
    const tabs = h(
      'div',
      { class: 'inner-tabs', role: 'tablist', 'aria-label': '수집관리 보기' },
      VIEWS.map(([key, label]) =>
        h(
          'button',
          {
            type: 'button',
            role: 'tab',
            'aria-selected': String(key === active),
            onclick: () => ctx.navigate('collect', { view: key }),
          },
          label,
        ),
      ),
    );
    return fragment(
      pageHead({
        title: TITLE,
        help: '국내외 도매몰 상품을 수집하고 자동 분석·검증합니다.',
        actions: [datePill(`${dotDate(view.meta.generated_at)}　오늘`)],
      }),
      tabs,
      active === 'suppliers' ? suppliersView(view, ctx) : jobsView(view, ctx),
    );
  },
};
