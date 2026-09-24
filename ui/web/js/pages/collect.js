// 수집관리: 수집 / 공급처 관리 views. The supplier view renders canonical SupplierConnection
// state (M1 CONNECT) in the v28/v29 supplier-card shape: login, connection test and resume are
// live; site analysis and collection rules arrive with COLLECT (M3) and stay inert.
// The UI never decides a supplier is connected — it shows the server's protected-read verdict.
// The 수집 view submits one product and follows its durable run (Gate 1 G1-E, below).

import { getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDate, dotDateTime } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { closeModal, openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';
import { datePill, pageHead } from '../components/page-head.js';

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

// ---------------------------------------------------------------- 수집 (Gate 1 G1-E)
//
// One product URL, submitted through POST /api/v1/collect/collections and then only read back.
// Every state shown is the durable run's own outcome: the page decides no outcome, never submits
// again on its own, and keeps nothing in browser storage. A reload or a return to this view
// follows the same durable runs — the run named in the route, and the newest runs the server
// lists — so leaving the screen neither cancels nor repeats a collection.

const RUNS = '/api/v1/collect/collections';
const RUN_POLL_MS = 1500;
// Bounded: about three minutes of reads. After that the operator asks for a recheck; the page
// never keeps reading on its own and never submits again.
const RUN_POLL_LIMIT = 120;

const OUTCOME_CHIP = {
  PENDING: ['진행 중', 'info'],
  RECORDED: ['기록됨', 'good'],
  NO_REVISION: ['기록할 식별자 없음', 'warn'],
  FAILED: ['실패', 'bad'],
};
const FACTS_CHIP = {
  CONFIRMED: ['원천 확정', 'good'],
  REVIEW_REQUIRED: ['원천 확인 필요', 'warn'],
};
const JOB_STATE_LABEL = {
  QUEUED: '대기 중',
  RUNNING: '실행 중',
  RETRY_SCHEDULED: '재시도 예정',
  SUCCEEDED: '작업 종료',
  DEAD: '작업 중단',
};
// Server codes rendered as copy. The code itself is always shown beside it.
const COLLECT_COPY = {
  COLLECT_URL_REFUSED: '이 공급처의 상품 상세 페이지 URL이 아닙니다.',
  COLLECT_SUPPLIER_UNKNOWN: '수집 정의가 없는 공급처입니다.',
  COLLECT_SAME_PRODUCT_TOO_SOON: '같은 상품을 너무 최근에 읽어 지금은 요청할 수 없습니다.',
  COLLECT_RUN_UNKNOWN: '해당 수집 기록을 찾을 수 없습니다.',
  COLLECT_RUN_NOT_RECORDED: '기록된 수집만 통합DB 상품과 연결됩니다.',
  COLLECT_RUN_LIMIT_INVALID: '최근 수집 목록 개수가 올바르지 않습니다.',
};
const HANDOFF_COPY = {
  NOT_YET_VISIBLE: '원천 리비전은 기록됐지만 통합DB 상품에는 아직 반영되지 않았습니다.',
  CURRENT_REVISION_DIFFERS: '통합DB 상품이 이 수집이 아닌 다른 원천 리비전을 현재로 가리키고 있습니다.',
};
// Gate 2 G2-B (ADR-0016): the ReviewItems of the run's source product, as the server holds them.
// The page renders server states and the server's coverage verdict; it counts nothing and decides
// nothing. A resolution names the scope and generation it was shown, and the server decides whether
// the item closes: it stays open while the source still states the condition.
const REVIEW = '/api/v1/review/items';
const OPERATOR = 'operator';
const REVIEW_KIND = { COLLECT_EVIDENCE: '원천 증거', STOCK: '재고' };
const REVIEW_STATE = {
  OPEN: ['검토 필요', 'warn'],
  RESOLVED: ['해결됨', 'good'],
  SUPERSEDED: ['새 리비전으로 대체', 'info'],
};
const REVIEW_OUTCOME = {
  RESOLVED: '원천이 더 이상 확인 필요 상태가 아니어서 항목이 해결되었습니다.',
  CONDITION_PERSISTS: '원천이 아직 확인 필요 상태라 항목은 열린 채로 남습니다. 해결 기록은 남았습니다.',
  SUPERSEDED: '원천이 새 리비전으로 바뀌어 새 검토 항목이 열렸습니다.',
};
const DISPOSITION_LABEL = {
  OWNER_ACTION_TAKEN: '원천에서 조치함',
  FOLLOW_UP_REQUIRED: '후속 조치 필요',
  NO_ACTION_TAKEN: '조치하지 않음',
};
const COVERAGE_COPY = {
  REVIEW_COVERAGE_NO_PASS_THIS_RUN: '검토 목록의 전체 대조가 아직 끝나지 않았습니다.',
  REVIEW_COVERAGE_STALE: '검토 목록의 마지막 전체 대조가 오래되었습니다.',
  REVIEW_INDEX_FAILURE_UNRECOVERED: '검토 목록 색인에 실패한 뒤 아직 복구되지 않았습니다.',
};
const SUBMIT_HELP =
  '상품 상세 URL 하나만 받습니다. 목록·카테고리 수집은 하지 않으며, 요청은 서버가 URL과 같은 상품 재수집 간격을 확인한 뒤 수집 작업 하나로 접수합니다.';
const RUNS_HELP =
  '서버에 기록된 최근 수집을 최신순으로 보여줍니다. 진행 중인 수집은 화면을 벗어났다 돌아와도 같은 기록을 다시 읽어 이어서 표시합니다.';

function short(id) {
  return id ? id.slice(0, 8) : '—';
}

function chip(label, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, label);
}

function outcomeChip(outcome) {
  const [label, tone] = OUTCOME_CHIP[outcome] ?? [outcome, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-outcome': outcome }, label);
}

function factsChip(status) {
  if (!status) return null;
  const [label, tone] = FACTS_CHIP[status] ?? [status, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-facts-status': status }, label);
}

function codeCopy(code, message) {
  const copy = COLLECT_COPY[code] ?? ERROR_COPY[code] ?? message ?? code;
  return code && copy !== code ? `${copy} (${code})` : copy;
}

// A run's terminal answer as the server recorded it: RECORDED names its revision and facts status,
// NO_REVISION and FAILED their durable detail, shown exactly as stored.
function runResult(run) {
  if (run.outcome === 'RECORDED') {
    return fragment(h('span', { class: 'mono', 'data-role': 'revision' }, short(run.revision_id)), ' ', factsChip(run.facts_status));
  }
  if (run.detail) return h('span', { class: 'mini', 'data-role': 'run-detail-text' }, run.detail);
  return '—';
}

function supplierLabel(key, suppliers) {
  return suppliers.find((s) => s.supplier_key === key)?.display_name ?? key;
}

function submitCard(view, ctx) {
  const keys = view.collection_supplier_keys;
  const select = h(
    'select',
    { id: 'collect-supplier', name: 'supplier_key', disabled: !keys.length },
    ...keys.map((key) => h('option', { value: key }, supplierLabel(key, view.suppliers))),
  );
  const url = h('input', {
    id: 'collect-url',
    type: 'url',
    name: 'product_url',
    required: true,
    maxlength: '2048',
    autocomplete: 'off',
    placeholder: '상품 상세 페이지 URL 하나',
  });
  const button = h('button', { type: 'submit', class: 'btn blue', 'data-action': 'submit-collection', disabled: !keys.length }, '수집 요청');
  const connection = h('div', { 'data-role': 'supplier-connection' });
  const refusal = h('div', { 'data-role': 'submit-refusal' });

  // CONNECT's own verdict for the chosen supplier, shown as it is. It never decides whether the
  // form may be sent: the server does.
  const showConnection = () => {
    const summary = view.suppliers.find((s) => s.supplier_key === select.value);
    if (!summary) {
      connection.replaceChildren();
      return;
    }
    const [label, tone] = CAPABILITY_CHIP[summary.capability_status] ?? [summary.capability_status, null];
    // fragment() skips an absent part; the DOM's own replaceChildren would print "null".
    connection.replaceChildren(fragment(
      h('div', { class: 'kv' }, h('span', {}, '공급처 연결'), h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-capability': summary.capability_status }, label)),
      summary.credentials_stored
        ? null
        : h(
            'div',
            { class: 'note', 'data-role': 'credentials-missing' },
            '이 공급처의 로그인 정보가 저장되어 있지 않습니다. ',
            h('button', { type: 'button', class: 'btn', onclick: () => ctx.navigate('collect', { view: 'suppliers' }) }, '공급처 관리'),
          ),
    ));
  };
  select.addEventListener('change', showConnection);
  showConnection();

  let inFlight = false;
  const form = h(
    'form',
    { class: 'panel collect-submit', 'data-role': 'collect-submit', novalidate: true },
    h('div', { class: 'supplier-head-row' }, withHelp(h('h3', { class: 'panel-title' }, '상품 수집'), SUBMIT_HELP)),
    keys.length ? null : h('div', { class: 'note', 'data-role': 'no-collection-supplier' }, '수집 정의가 있는 공급처가 없습니다.'),
    h('div', { class: 'form-row' }, h('label', { for: 'collect-supplier' }, '공급처'), select),
    connection,
    h('div', { class: 'form-row' }, h('label', { for: 'collect-url' }, '상품 URL'), url),
    refusal,
    h('div', { class: 'supplier-actions' }, button),
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    // One submit, one request: a second click or Enter while the first is on the wire does nothing.
    if (inFlight) return;
    const productUrl = url.value.trim();
    if (!productUrl || !select.value) {
      refusal.replaceChildren(h('div', { class: 'note' }, '공급처와 상품 URL을 입력하세요.'));
      return;
    }
    inFlight = true;
    button.disabled = true;
    refusal.replaceChildren();
    try {
      const submitted = await sendJson('POST', RUNS, { supplier_key: select.value, product_url: productUrl });
      url.value = '';
      toast('수집 요청 접수', `수집 ${short(submitted.collection_run_id)} · 작업 ${short(submitted.job_id)}`);
      ctx.navigate('collect', { view: 'jobs', run: submitted.collection_run_id });
    } catch (error) {
      // A refused request made no run: nothing local is shown as if one existed.
      const code = error?.error?.code ?? null;
      refusal.replaceChildren(
        h('div', { class: 'note', 'data-reason': code ?? '' }, codeCopy(code, error?.error?.message ?? String(error?.message ?? error))),
      );
      inFlight = false;
      button.disabled = false;
    }
  });
  return form;
}

function jobsView(view, ctx) {
  const focusId = ctx.params.get('run');
  const root = h('div', { class: 'collect-jobs', 'data-role': 'collect-jobs' });
  const focus = h('section', { class: 'panel collect-run', 'data-role': 'run-focus', hidden: !focusId });
  const listBody = h('tbody', {});
  const recheck = h('button', { type: 'button', class: 'btn', 'data-action': 'recheck-runs', hidden: true }, '상태 다시 확인');
  const runsPanel = h(
    'section',
    { class: 'panel collect-runs', 'data-role': 'recent-runs' },
    h('div', { class: 'supplier-head-row' }, withHelp(h('h3', { class: 'panel-title' }, '최근 수집'), RUNS_HELP), recheck),
    h(
      'table',
      { class: 'table' },
      h('thead', {}, h('tr', {}, ...['요청 시각', '공급처', '상태', '결과', ''].map((label) => h('th', {}, label)))),
      listBody,
    ),
  );
  let polls = 0;
  let timer = null;
  // The newest review render owns the block: a slower, older response never replaces it.
  let reviewSeq = 0;

  function runRow(run) {
    return h(
      'tr',
      { 'data-run': run.collection_run_id, 'data-outcome': run.outcome, 'aria-current': run.collection_run_id === focusId ? 'true' : null },
      h('td', {}, dotDateTime(run.requested_at)),
      h('td', {}, supplierLabel(run.supplier_key, view.suppliers)),
      h('td', {}, outcomeChip(run.outcome)),
      h('td', {}, runResult(run)),
      h(
        'td',
        {},
        h('button', { type: 'button', class: 'btn', 'data-action': 'open-run', onclick: () => ctx.navigate('collect', { view: 'jobs', run: run.collection_run_id }) }, '보기'),
      ),
    );
  }

  async function handoffBlock(run) {
    const holder = h('div', { class: 'collect-handoff', 'data-role': 'handoff' });
    let handoff;
    try {
      handoff = await getJson(`${RUNS}/${encodeURIComponent(run.collection_run_id)}/product`);
    } catch (error) {
      const code = error?.error?.code ?? null;
      holder.append(h('div', { class: 'note', 'data-reason': code ?? '' }, codeCopy(code, error?.error?.message)));
      return holder;
    }
    holder.dataset.state = handoff.state;
    const open = handoff.product_group_id
      ? h(
          'button',
          { type: 'button', class: 'btn blue', 'data-action': 'open-product', onclick: () => ctx.navigate('db', { product: handoff.product_group_id }) },
          '통합DB에서 보기',
        )
      : null;
    // Read-only follow-through: a recheck reads again, and nothing here ever collects again.
    const again = handoff.state === 'NOT_YET_VISIBLE'
      ? h('button', { type: 'button', class: 'btn', 'data-action': 'recheck-product', onclick: () => refresh() }, '다시 확인')
      : null;
    holder.append(fragment(
      h('div', { class: 'kv' }, h('span', {}, '원천 상품'), h('b', {}, `${handoff.supplier_key} · ${handoff.source_product_id}`)),
      handoff.product_group_id ? h('div', { class: 'kv' }, h('span', {}, '통합DB 상품'), h('b', { class: 'mono' }, short(handoff.product_group_id))) : null,
      HANDOFF_COPY[handoff.state] ? h('div', { class: 'note', 'data-reason': handoff.state }, HANDOFF_COPY[handoff.state]) : null,
      h('div', { class: 'supplier-actions' }, open, again),
    ));
    holder.source = { supplier_key: handoff.supplier_key, source_product_id: handoff.source_product_id };
    return holder;
  }

  function reviewRow(item, onResolved) {
    const [label, tone] = REVIEW_STATE[item.state] ?? [item.state, null];
    const row = h(
      'div',
      { class: 'collect-review-item', 'data-review-item': item.review_item_id, 'data-state': item.state, 'data-generation': String(item.generation) },
      h(
        'div',
        { class: 'supplier-head-row' },
        h('b', {}, REVIEW_KIND[item.kind] ?? item.kind),
        h('span', { class: 'mono', 'data-role': 'review-subject' }, item.subject),
        h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-review-state': item.state }, label),
      ),
      h('div', { class: 'mini', 'data-reason': item.reason_code }, item.reason_code),
    );
    if (item.state !== 'OPEN') return row;
    const disposition = h(
      'select',
      { name: 'disposition', 'data-role': 'review-disposition' },
      ...Object.entries(DISPOSITION_LABEL).map(([value, text]) => h('option', { value }, text)),
    );
    const note = h('input', { type: 'text', name: 'note', maxlength: '500', placeholder: '메모 (선택)', 'data-role': 'review-note' });
    const button = h('button', { type: 'submit', class: 'btn', 'data-action': 'resolve-review' }, '해결 기록');
    const answer = h('div', { 'data-role': 'review-answer' });
    let inFlight = false;
    const form = h('form', { class: 'supplier-actions', novalidate: true }, disposition, note, button);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      // One resolution per click: a second click while the first is on the wire does nothing.
      if (inFlight) return;
      inFlight = true;
      button.disabled = true;
      answer.replaceChildren();
      try {
        const resolved = await sendJson('POST', `${REVIEW}/${encodeURIComponent(item.review_item_id)}/resolve`, {
          expected_scope: item.scope,
          expected_generation: item.generation,
          disposition: disposition.value,
          note: note.value.trim() || null,
          actor: OPERATOR,
        });
        // The server's answer is shown on the redrawn block, which reads the item back.
        onResolved(resolved.outcome);
      } catch (error) {
        const code = error?.error?.code ?? null;
        answer.replaceChildren(h('div', { class: 'note', 'data-reason': code ?? '' }, codeCopy(code, error?.error?.message)));
        inFlight = false;
        button.disabled = false;
      }
    });
    row.append(form, answer);
    return row;
  }

  async function reviewBlock(source, lastOutcome = null) {
    const seq = ++reviewSeq;
    const holder = h('div', { class: 'collect-review', 'data-role': 'review-items' });
    let listed;
    try {
      const query = new URLSearchParams({ supplier_key: source.supplier_key, source_product_id: source.source_product_id });
      listed = await getJson(`${REVIEW}?${query}`);
    } catch (error) {
      const code = error?.error?.code ?? null;
      holder.append(h('div', { class: 'note', 'data-reason': code ?? '' }, codeCopy(code, error?.error?.message)));
      return holder;
    }
    if (seq !== reviewSeq) return null; // a newer render owns the block now
    const stale = listed.coverage.filter((c) => !c.current);
    holder.dataset.coverage = stale.length ? 'NOT_CURRENT' : 'CURRENT';
    const open = listed.items.filter((item) => item.state === 'OPEN');
    const rest = listed.items.filter((item) => item.state !== 'OPEN');
    const redraw = async (outcome) => {
      const next = await reviewBlock(source, outcome);
      if (next && holder.isConnected) holder.replaceWith(next);
    };
    holder.append(fragment(
      h('div', { class: 'supplier-head-row' }, h('b', {}, '검토 항목')),
      lastOutcome
        ? h('div', { class: 'note', 'data-role': 'review-outcome', 'data-outcome': lastOutcome }, REVIEW_OUTCOME[lastOutcome] ?? lastOutcome)
        : null,
      ...stale.map((c) => h('div', { class: 'note', 'data-coverage': c.producer, 'data-reason': c.reason ?? '' }, COVERAGE_COPY[c.reason] ?? c.reason)),
      open.length || rest.length
        ? null
        : h(
            'div',
            { class: 'note', 'data-role': 'review-empty' },
            stale.length
              ? '검토 목록이 최신이 아니어서, 비어 있어도 검토할 것이 없다는 뜻은 아닙니다.'
              : '이 원천 상품에 기록된 검토 항목이 없습니다.',
          ),
      ...open.map((item) => reviewRow(item, redraw)),
      ...rest.map((item) => reviewRow(item, redraw)),
    ));
    return holder;
  }

  async function renderFocus() {
    if (!focusId) return false;
    let run;
    try {
      run = await getJson(`${RUNS}/${encodeURIComponent(focusId)}`);
    } catch (error) {
      const code = error?.error?.code ?? null;
      focus.dataset.state = 'error';
      focus.replaceChildren(h('div', { class: 'note', 'data-reason': code ?? '' }, codeCopy(code, error?.error?.message)));
      return false;
    }
    let job = null;
    if (run.outcome === 'PENDING') {
      try {
        job = await getJson(`/api/v1/system/jobs/${encodeURIComponent(run.job_id)}`);
      } catch {
        job = null;
      }
    }
    const handoff = run.outcome === 'RECORDED' ? await handoffBlock(run) : null;
    const review = handoff?.source ? await reviewBlock(handoff.source) : null;
    if (!root.isConnected && polls > 0) return false; // the operator left; nothing to render into
    focus.dataset.run = run.collection_run_id;
    focus.dataset.outcome = run.outcome;
    focus.dataset.state = 'ready';
    focus.replaceChildren(fragment(
      h('div', { class: 'supplier-head-row' }, h('h3', { class: 'panel-title' }, `수집 ${short(run.collection_run_id)}`), outcomeChip(run.outcome)),
      kv('수집 ID', run.collection_run_id),
      kv('작업 ID', run.job_id),
      kv('상관 ID', run.correlation_id),
      kv('공급처', supplierLabel(run.supplier_key, view.suppliers)),
      kv('상품 URL', run.source_url),
      kv('요청 시각', dotDateTime(run.requested_at)),
      run.finished_at ? kv('종료 시각', dotDateTime(run.finished_at)) : null,
      job
        ? h(
            'div',
            { class: 'kv', 'data-role': 'job-state', 'data-job-state': job.state },
            h('span', {}, '작업 상태'),
            h('b', {}, `${JOB_STATE_LABEL[job.state] ?? job.state} · 시도 ${job.attempt_count}/${job.max_attempts}`, job.next_attempt_at && job.state === 'RETRY_SCHEDULED' ? ` · 다음 ${dotDateTime(job.next_attempt_at)}` : ''),
          )
        : null,
      run.outcome === 'RECORDED' ? h('div', { class: 'kv' }, h('span', {}, '원천 리비전'), h('b', { class: 'mono', 'data-role': 'revision-id' }, run.revision_id)) : null,
      run.outcome === 'RECORDED' ? h('div', { class: 'kv' }, h('span', {}, '사실 상태'), factsChip(run.facts_status)) : null,
      run.outcome === 'NO_REVISION' || run.outcome === 'FAILED'
        ? h('div', { class: 'kv' }, h('span', {}, run.outcome === 'FAILED' ? '실패 사유' : '사유'), h('b', { 'data-role': 'run-detail' }, run.detail ?? '—'))
        : null,
      handoff,
      review,
    ));
    return run.outcome === 'PENDING';
  }

  async function renderList() {
    try {
      const listed = await getJson(`${RUNS}?limit=10`);
      listBody.replaceChildren(
        ...(listed.runs.length
          ? listed.runs.map(runRow)
          : [h('tr', {}, h('td', { class: 'table-empty', colspan: '5' }, '아직 수집 기록이 없습니다.'))]),
      );
      return listed.runs.some((run) => run.outcome === 'PENDING');
    } catch (error) {
      listBody.replaceChildren(h('tr', {}, h('td', { class: 'table-empty', colspan: '5' }, codeCopy(error?.error?.code ?? null, error?.error?.message))));
      return false;
    }
  }

  // Reads only. While a run is still PENDING the same reads repeat, a bounded number of times and
  // only while this view is on screen; nothing here ever sends a collection.
  async function refresh() {
    window.clearTimeout(timer);
    const [focusPending, listPending] = await Promise.all([renderFocus(), renderList()]);
    const pending = focusPending || listPending;
    if (!root.isConnected && polls > 0) return;
    if (pending && polls < RUN_POLL_LIMIT) {
      polls += 1;
      recheck.hidden = true;
      timer = window.setTimeout(() => {
        if (root.isConnected) refresh();
      }, RUN_POLL_MS);
    } else {
      recheck.hidden = !pending;
    }
  }
  recheck.addEventListener('click', () => {
    polls = 0;
    refresh();
  });

  root.append(submitCard(view, ctx), focus, runsPanel);
  refresh();
  return root;
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
