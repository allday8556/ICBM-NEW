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
import { collectFactsBlock, collectPreviewBlock, readRevisionFacts } from '../components/collect-facts.js';
import { commonImagesSummary, openCommonImages } from '../components/common-images.js';
import { datePill, pageHead } from '../components/page-head.js';
import { reviewItemsBlock } from '../components/review-items.js';

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
  // A-NEXT2a: the supplier's common images (Issue #219's owner), counted by the owner and decided
  // in their own modal. The card line is read again after a decision.
  let commonLine = commonImagesSummary(supplier);
  const refreshCommon = () => {
    const next = commonImagesSummary(supplier);
    commonLine.replaceWith(next);
    commonLine = next;
  };
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
    commonLine,
    failing ? h('div', { class: 'note' }, ERROR_COPY[supplier.last_error_code] ?? supplier.last_error_code) : null,
    h(
      'div',
      { class: 'supplier-actions' },
      h('button', { type: 'button', class: 'btn', onclick: () => openCredentialModal(supplier, ctx) }, '로그인 정보'),
      primary,
      h('button', { type: 'button', class: 'btn', 'data-action': 'open-common-images', onclick: () => openCommonImages(supplier, refreshCommon) }, '공통 이미지'),
      markInert(h('button', { type: 'button', class: 'btn' }, '사이트 분석')),
      markInert(h('button', { type: 'button', class: 'btn' }, '수집 규칙')),
    ),
  );
}

// ---------------------------------------------------------------- 수집 (Gate 1 G1-E)
//
// Product URLs, each submitted on its own through POST /api/v1/collect/collections and then only
// read back. The form takes up to 50 lines (A-UX2 D1); each URL is still one ordinary single-URL
// request and its own run, sent one after another through the one submit call below.
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
  COLLECT_RUN_CURSOR_INVALID: '목록 위치가 올바르지 않아 처음부터 다시 불러와야 합니다.',
  COLLECT_REVISION_UNKNOWN: '해당 원천 리비전을 찾을 수 없습니다.',
};
// The list filters the server applies before it orders and bounds the runs (A-UX1 D3). A run's
// outcome and its facts status stay two axes: 확인 필요 selects by facts status, the others by outcome.
// The first five are v29's state cards (each counts what the server reports for its filter);
// 기록할 식별자 없음 has no v29 card and sits beside the list's count.
const RUN_FILTERS = [
  ['all', '전체 수집', { icon: '▤' }, {}],
  ['pending', '진행 중', { icon: '◌' }, { outcome: 'PENDING' }],
  ['recorded', '기록됨', { icon: '✓' }, { outcome: 'RECORDED' }],
  ['failed', '실패', { icon: '×' }, { outcome: 'FAILED' }],
  ['review', '원천 확인 필요', { icon: '◇' }, { facts_status: 'REVIEW_REQUIRED' }],
  ['no_revision', '기록할 식별자 없음', {}, { outcome: 'NO_REVISION' }],
];
const CARD_FILTERS = RUN_FILTERS.slice(0, 5);
const RUN_PAGE = 20;
const TRANSPORT_LABEL = { DIRECT_URL: '직접 URL', EXTENSION: '확장 수집' };
const HANDOFF_COPY = {
  NOT_YET_VISIBLE: '원천 리비전은 기록됐지만 통합DB 상품에는 아직 반영되지 않았습니다.',
  CURRENT_REVISION_DIFFERS: '통합DB 상품이 이 수집이 아닌 다른 원천 리비전을 현재로 가리키고 있습니다.',
};
// Gate 2 G2-B (ADR-0016): the ReviewItems of the run's source product, as the server holds them.
// The page renders server states and the server's coverage verdict; it counts nothing and decides
// nothing. A resolution names the scope and generation it was shown, and the server decides whether
// the item closes: it stays open while the source still states the condition.
const REVIEW_KIND = { COLLECT_EVIDENCE: '원천 증거', STOCK: '재고' };
const REVIEW_OUTCOME = {
  RESOLVED: '원천이 더 이상 확인 필요 상태가 아니어서 항목이 해결되었습니다.',
  CONDITION_PERSISTS: '원천이 아직 확인 필요 상태라 항목은 열린 채로 남습니다. 해결 기록은 남았습니다.',
  SUPERSEDED: '원천이 새 리비전으로 바뀌어 새 검토 항목이 열렸습니다.',
};
// The operator's own input bound (A-UX2 D1): a convenience for typing URLs in, never the E3 list
// queue's bound and never a batch. Every line is its own single-URL request.
const INTAKE_MAX = 50;
const SUBMIT_HELP =
  '상품 상세 URL을 한 줄에 하나씩, 최대 50개까지 받습니다. 목록·카테고리 수집은 하지 않으며, URL마다 따로 서버가 URL과 같은 상품 재수집 간격을 확인한 뒤 수집 작업 하나로 접수합니다.';
const RUNS_HELP =
  '서버에 기록된 최근 수집을 최신순으로 보여줍니다. 진행 중인 수집은 화면을 벗어났다 돌아와도 같은 기록을 다시 읽어 이어서 표시합니다. ' +
  '필터는 서버가 전체 기록에서 먼저 고른 뒤 최신순으로 나눠 보여줍니다.';

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

// The URL as the run recorded it, shortened to host and path for the list; the full URL is its title.
function shortUrl(sourceUrl) {
  try {
    const parsed = new URL(sourceUrl);
    const text = `${parsed.host}${parsed.pathname}${parsed.search}`;
    return text.length > 36 ? `${text.slice(0, 36)}…` : text;
  } catch {
    return sourceUrl ?? '—';
  }
}

function supplierLabel(key, suppliers) {
  return suppliers.find((s) => s.supplier_key === key)?.display_name ?? key;
}

// The lines of the URL box as the operator typed them: each non-empty line is one URL. A line the
// browser cannot read as an http(s) URL is shown as invalid and never sent; the same URL twice is
// sent once. Whether a URL is a product page of the supplier is the server's to decide.
function intakeLines(text) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const planned = [];
  const invalid = [];
  let duplicates = 0;
  for (const line of lines) {
    let parsed = null;
    try {
      parsed = new URL(line);
    } catch {
      parsed = null;
    }
    if (!parsed || !['http:', 'https:'].includes(parsed.protocol)) {
      invalid.push(line);
    } else if (planned.includes(parsed.href)) {
      duplicates += 1;
    } else {
      planned.push(parsed.href);
    }
  }
  return { lines, planned, invalid, duplicates };
}

function submitCard(view, ctx, onSubmitted = () => {}) {
  const keys = view.collection_supplier_keys;
  const select = h(
    'select',
    { id: 'collect-supplier', class: 'field', name: 'supplier_key', 'aria-label': '공급처', disabled: !keys.length },
    ...keys.map((key) => h('option', { value: key }, supplierLabel(key, view.suppliers))),
  );
  // v29's one-line URL field: it grows with the lines pasted into it (A-UX2: one URL per line).
  const url = h('textarea', {
    id: 'collect-url',
    class: 'field search',
    name: 'product_url',
    rows: '1',
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': '상품 URL',
    placeholder: 'https:// 도매몰 상품 URL을 입력하세요. 여러 개는 줄바꿈 (최대 50개)',
  });
  const button = h('button', { type: 'submit', class: 'btn dark', 'data-action': 'submit-collection', disabled: !keys.length }, '▶ 수집 시작');
  const connection = h('span', { class: 'collect-connection', 'data-role': 'supplier-connection' });
  const credentials = h('div', { 'data-role': 'credentials-slot' });
  const refusal = h('div', { 'data-role': 'submit-refusal' });
  const summary = h('div', { class: 'mini', 'data-role': 'intake-summary' });
  const results = h('div', { 'data-role': 'intake-results' });

  // What will be sent, counted from the box as it stands. It decides nothing about the URLs.
  const showSummary = () => {
    const intake = intakeLines(url.value);
    summary.dataset.lines = String(intake.lines.length);
    summary.dataset.duplicates = String(intake.duplicates);
    summary.dataset.invalid = String(intake.invalid.length);
    summary.dataset.planned = String(intake.planned.length);
    url.rows = String(Math.min(Math.max(intake.lines.length, 1), 6));
    summary.replaceChildren(
      intake.lines.length
        ? `입력 ${intake.lines.length}줄 · 중복 ${intake.duplicates} · 잘못된 URL ${intake.invalid.length} · 수집 예정 ${intake.planned.length}`
        : '',
      ...(intake.invalid.length
        ? [h('ul', { class: 'intake-invalid', 'data-role': 'invalid-lines' }, ...intake.invalid.map((line) => h('li', { class: 'mono' }, line)))]
        : []),
    );
    return intake;
  };
  url.addEventListener('input', showSummary);

  // CONNECT's own verdict for the chosen supplier, shown as it is. It never decides whether the
  // form may be sent: the server does.
  const showConnection = () => {
    const summary = view.suppliers.find((s) => s.supplier_key === select.value);
    if (!summary) {
      connection.replaceChildren();
      credentials.replaceChildren();
      return;
    }
    const [label, tone] = CAPABILITY_CHIP[summary.capability_status] ?? [summary.capability_status, null];
    connection.replaceChildren(
      h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-capability': summary.capability_status, title: '공급처 연결' }, `● ${label}`),
    );
    // fragment() skips an absent part; the DOM's own replaceChildren would print "null".
    credentials.replaceChildren(fragment(
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
  // v29's toolbar row. The collection-option chips, 재검증 and AI 추출 보정 have no contract yet: they
  // keep their v29 place and only say so when pressed (markInert), sending nothing.
  const form = h(
    'form',
    { class: 'panel collect-submit', 'data-role': 'collect-submit', novalidate: true },
    h(
      'div',
      { class: 'toolbar collect-toolbar' },
      url,
      select,
      connection,
      ...['상세페이지 수집', '옵션/색상 수집', '이미지 다운로드'].map((label) => markInert(h('span', { class: 'chip info', 'data-role': 'collect-option' }, label))),
      button,
      markInert(h('button', { type: 'button', class: 'btn', 'data-role': 'revalidate' }, '↻ 재검증'), '재검증'),
      markInert(h('button', { type: 'button', class: 'btn ai-btn', 'data-role': 'ai-correct' }, '✨ AI 추출 보정'), 'AI 추출 보정'),
    ),
    keys.length ? null : h('div', { class: 'note', 'data-role': 'no-collection-supplier' }, '수집 정의가 있는 공급처가 없습니다.'),
    h('div', { class: 'intake-line' }, withHelp(h('span', { class: 'mini' }, '한 줄에 URL 하나 · 최대 50개'), SUBMIT_HELP), summary),
    credentials,
    refusal,
    results,
  );

  // The one submit call: one URL, one request, answered with that run's identity or a refusal.
  const submitOne = (supplierKey, productUrl) => sendJson('POST', RUNS, { supplier_key: supplierKey, product_url: productUrl });

  // More than one URL: each is sent on its own, one after another, and each answer is shown beside
  // its URL. A refused URL made no run; its line stays in the box for the operator to correct.
  const submitEach = async (supplierKey, planned) => {
    const rows = [];
    const refused = [];
    for (const productUrl of planned) {
      try {
        const submitted = await submitOne(supplierKey, productUrl);
        rows.push(
          h(
            'li',
            { 'data-url': productUrl, 'data-state': 'accepted', 'data-run': submitted.collection_run_id },
            h('span', { class: 'mono' }, productUrl),
            ' · ',
            h('button', { type: 'button', class: 'btn', 'data-action': 'open-run', onclick: () => ctx.navigate('collect', { view: 'jobs', run: submitted.collection_run_id }) }, `수집 ${short(submitted.collection_run_id)}`),
          ),
        );
      } catch (error) {
        const code = error?.error?.code ?? null;
        refused.push(productUrl);
        rows.push(
          h(
            'li',
            { 'data-url': productUrl, 'data-state': 'refused', 'data-reason': code ?? '' },
            h('span', { class: 'mono' }, productUrl),
            ' · ',
            codeCopy(code, error?.error?.message ?? String(error?.message ?? error)),
          ),
        );
      }
    }
    const accepted = rows.length - refused.length;
    results.replaceChildren(
      h('div', { class: 'note' }, `접수 ${accepted}건 · 거절 ${refused.length}건. 거절된 URL은 입력란에 남겨 두었습니다.`),
      h('ul', { class: 'intake-results' }, ...rows),
    );
    url.value = refused.join('\n');
    showSummary();
    if (accepted) toast('수집 요청 접수', `${accepted}건을 각각 수집 작업으로 접수했습니다.`);
    onSubmitted();
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    // One submit: a second click while the first is on the wire does nothing.
    if (inFlight) return;
    const intake = showSummary();
    results.replaceChildren();
    if (!intake.lines.length || !select.value) {
      refusal.replaceChildren(h('div', { class: 'note' }, '공급처와 상품 URL을 입력하세요.'));
      return;
    }
    if (intake.lines.length > INTAKE_MAX) {
      refusal.replaceChildren(h('div', { class: 'note', 'data-reason': 'INTAKE_TOO_MANY' }, `한 번에 최대 ${INTAKE_MAX}개 URL까지 입력할 수 있습니다. 지금 ${intake.lines.length}줄입니다.`));
      return;
    }
    if (!intake.planned.length) {
      refusal.replaceChildren(h('div', { class: 'note', 'data-reason': 'INTAKE_NO_VALID_URL' }, '보낼 수 있는 URL이 없습니다. 잘못된 URL을 고쳐 주세요.'));
      return;
    }
    inFlight = true;
    button.disabled = true;
    refusal.replaceChildren();
    if (intake.planned.length > 1) {
      try {
        await submitEach(select.value, intake.planned);
      } finally {
        inFlight = false;
        button.disabled = false;
      }
      return;
    }
    // One URL: the one-product path exactly as before, followed into its run.
    const productUrl = intake.planned[0];
    try {
      const submitted = await submitOne(select.value, productUrl);
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
  const filterKey = RUN_FILTERS.some(([key]) => key === ctx.params.get('filter')) ? ctx.params.get('filter') : 'all';
  const filterQuery = RUN_FILTERS.find(([key]) => key === filterKey)[3];
  const root = h('div', { class: 'collect-jobs', 'data-role': 'collect-jobs' });
  const focus = h('section', { class: 'panel collect-run', 'data-role': 'run-focus', hidden: !focusId });
  // The focused run's 수집 사실 is its own panel below the run, so neither outgrows the screen.
  const factsSlot = h('div', { class: 'collect-facts-slot', 'data-role': 'facts-slot' });
  // v29's 수집 미리보기 beside the work list: the focused run, read back as recorded.
  let previewBody = h('div', { 'data-role': 'preview-body' }, h('div', { class: 'mini' }, '목록에서 수집을 고르면 미리보기가 표시됩니다.'));
  const preview = h('aside', { class: 'side-detail collect-preview', 'data-role': 'run-preview' }, h('h3', {}, '수집 미리보기'), previewBody);
  const listBody = h('tbody', {});
  const recheck = h('button', { type: 'button', class: 'btn', 'data-action': 'recheck-runs', hidden: true }, '상태 다시 확인');
  const listCount = h('span', { class: 'mini', 'data-role': 'runs-count' });
  const more = h('button', { type: 'button', class: 'btn', 'data-action': 'more-runs', hidden: true }, '더 보기');
  // The filter lives in the route, never in browser storage; changing it keeps the focused run.
  const goFilter = (key) => ctx.navigate('collect', { view: 'jobs', ...(focusId ? { run: focusId } : {}), ...(key === 'all' ? {} : { filter: key }) });
  const counts = new Map();
  const cards = h(
    'div',
    { class: 'grid5 page-kpis', role: 'tablist', 'aria-label': '수집 상태', 'data-role': 'run-filters' },
    CARD_FILTERS.map(([key, label, { icon }]) => {
      const count = h('strong', { 'data-role': 'filter-count' }, '—');
      counts.set(key, count);
      return h(
        'button',
        {
          type: 'button',
          role: 'tab',
          class: key === filterKey ? 'kpi state-card active' : 'kpi state-card',
          'data-filter': key,
          'aria-selected': String(key === filterKey),
          onclick: () => goFilter(key),
        },
        h('span', { class: 'kpi-top' }, h('span', { class: 'kicon', 'aria-hidden': 'true' }, icon), label),
        count,
        h('span', { class: 'trend no-data' }, '전일 대비 데이터 없음'),
      );
    }),
  );
  const [noRevisionKey, noRevisionLabel] = RUN_FILTERS[5];
  const noRevisionCount = h('span', { 'data-role': 'filter-count' }, '');
  counts.set(noRevisionKey, noRevisionCount);
  const noRevision = h(
    'button',
    {
      type: 'button',
      class: noRevisionKey === filterKey ? 'chip warn filter-chip' : 'chip filter-chip',
      'data-filter': noRevisionKey,
      'aria-pressed': String(noRevisionKey === filterKey),
      onclick: () => goFilter(noRevisionKey),
    },
    noRevisionLabel,
    noRevisionCount,
  );
  const runsPanel = h(
    'section',
    { class: 'panel collect-runs', 'data-role': 'recent-runs', 'data-filter': filterKey },
    h('div', { class: 'supplier-head-row' }, withHelp(h('h3', { class: 'panel-title' }, '수집 작업 목록'), RUNS_HELP), h('div', { class: 'list-head-meta' }, noRevision, listCount, recheck)),
    h(
      'table',
      { class: 'table' },
      h(
        'thead',
        {},
        h('tr', {}, ...['요청시간', '공급처', 'URL', '상태', '리비전 · 사실', '후보수', '저장수', '가격확인', ''].map((label) => h('th', {}, label))),
      ),
      listBody,
    ),
    h('div', { class: 'supplier-actions' }, more),
  );
  let polls = 0;
  let timer = null;
  // The source product's ReviewItems (shared with 통합DB and 등록관리; newest render wins).
  const reviewBlock = reviewItemsBlock({
    kindLabel: REVIEW_KIND,
    outcomeCopy: REVIEW_OUTCOME,
    emptyCopy: '이 원천 상품에 기록된 검토 항목이 없습니다.',
    errorCopy: codeCopy,
  });

  function runRow(run) {
    return h(
      'tr',
      { 'data-run': run.collection_run_id, 'data-outcome': run.outcome, 'aria-current': run.collection_run_id === focusId ? 'true' : null },
      h('td', {}, dotDateTime(run.requested_at)),
      h('td', {}, supplierLabel(run.supplier_key, view.suppliers)),
      h('td', { class: 'run-url', title: run.source_url }, shortUrl(run.source_url)),
      h('td', {}, outcomeChip(run.outcome)),
      h('td', {}, runResult(run)),
      // v29's 후보수 · 저장수 · 가격확인 have no source on a run: their place stays, without data.
      ...['후보수', '저장수', '가격확인'].map((label) => h('td', { class: 'no-data', 'data-no-data': label }, '데이터 없음')),
      h(
        'td',
        {},
        h('button', { type: 'button', class: 'btn', 'data-action': 'open-run', onclick: () => ctx.navigate('collect', { view: 'jobs', run: run.collection_run_id, ...(filterKey === 'all' ? {} : { filter: filterKey }) }) }, '보기'),
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
    holder.product = { state: handoff.state, productGroupId: handoff.product_group_id ?? null };
    return holder;
  }

  // Recovery (A-UX3 F-10): a FAILED run's own supplier and URL go back into the form, and the
  // operator decides whether to submit. Nothing is sent from here, the failed run stays as it was
  // recorded, and a new submit is an ordinary new run through the one submit path.
  function refill(run) {
    const select = document.getElementById('collect-supplier');
    const input = document.getElementById('collect-url');
    if (!select || !input) return;
    if ([...select.options].some((option) => option.value === run.supplier_key)) {
      select.value = run.supplier_key;
      select.dispatchEvent(new Event('change'));
    }
    input.value = run.source_url;
    input.scrollIntoView({ block: 'center' });
    input.focus();
    toast('다시 수집', 'URL을 입력란에 채웠습니다. 수집 요청을 눌러야 새로 수집합니다.');
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
    const read = run.outcome === 'RECORDED' && run.revision_id ? await readRevisionFacts(run.revision_id, handoff?.product ?? null) : null;
    const facts = read ? collectFactsBlock(read, run, codeCopy) : null;
    const review = handoff?.source ? await reviewBlock(handoff.source) : null;
    if (!root.isConnected && polls > 0) return false; // the operator left; nothing to render into
    previewBody.replaceWith(
      (previewBody = collectPreviewBlock(run, read, {
        supplier: supplierLabel(run.supplier_key, view.suppliers),
        chips: [outcomeChip(run.outcome), factsChip(run.facts_status)].filter(Boolean),
        errorCopy: codeCopy,
      })),
    );
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
      run.transport_kind ? kv('수집 경로', TRANSPORT_LABEL[run.transport_kind] ?? run.transport_kind) : null,
      // The run's own outcome, on its own axis: never merged with the facts status below.
      h('div', { class: 'kv', 'data-role': 'run-outcome' }, h('span', {}, '수집 결과'), outcomeChip(run.outcome)),
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
      run.outcome === 'FAILED'
        ? h(
            'div',
            { class: 'supplier-actions' },
            h('button', { type: 'button', class: 'btn', 'data-action': 'refill-url', onclick: () => refill(run) }, '다시 수집'),
          )
        : null,
      run.outcome === 'NO_REVISION'
        ? h('div', { class: 'note', 'data-reason': 'NO_REVISION' }, '수집은 끝났지만 원천이 상품 식별자를 밝히지 않아 기록할 리비전이 없습니다. 실패가 아니며, 사실 상태도 없습니다.')
        : null,
      handoff,
      review,
    ));
    factsSlot.replaceChildren(...(facts ? [facts] : []));
    return run.outcome === 'PENDING';
  }

  // Each state card's count is the server's own total for that filter (a one-run read). It is
  // never counted in the page.
  async function renderCounts() {
    await Promise.all(
      RUN_FILTERS.map(async ([key, , , query]) => {
        const slot = counts.get(key);
        try {
          const listed = await getJson(`${RUNS}?${new URLSearchParams({ limit: '1', ...query })}`);
          slot.dataset.total = String(listed.total);
          slot.textContent = Number(listed.total).toLocaleString('ko-KR');
        } catch {
          slot.dataset.total = '';
          slot.textContent = '—';
        }
      }),
    );
  }

  function listUrl(before) {
    const query = new URLSearchParams({ limit: String(RUN_PAGE), ...filterQuery });
    if (before) query.set('before', before);
    return `${RUNS}?${query}`;
  }

  // The server's page and its count of everything the filter selects: a page is never shown as
  // the whole list.
  function showPage(listed, shown) {
    const label = RUN_FILTERS.find(([key]) => key === filterKey)[1];
    const total = listed.total ?? null;
    listCount.dataset.total = total === null ? '' : String(total);
    listCount.dataset.shown = String(shown);
    listCount.textContent = total === null ? '' : `${label} ${total}건 중 ${shown}건 표시`;
    more.hidden = !listed.next_before;
    more.dataset.before = listed.next_before ?? '';
  }

  async function renderList() {
    try {
      const listed = await getJson(listUrl(null));
      listBody.replaceChildren(
        ...(listed.runs.length
          ? listed.runs.map(runRow)
          : [h('tr', {}, h('td', { class: 'table-empty', colspan: '9' }, filterKey === 'all' ? '아직 수집 기록이 없습니다.' : '이 조건에 맞는 수집 기록이 없습니다.'))]),
      );
      showPage(listed, listed.runs.length);
      return listed.runs.some((run) => run.outcome === 'PENDING');
    } catch (error) {
      listBody.replaceChildren(h('tr', {}, h('td', { class: 'table-empty', colspan: '9' }, codeCopy(error?.error?.code ?? null, error?.error?.message))));
      more.hidden = true;
      return false;
    }
  }

  // The next page after the last run shown. Read-only, like everything in this list.
  more.addEventListener('click', async () => {
    more.disabled = true;
    try {
      const listed = await getJson(listUrl(more.dataset.before));
      listBody.append(...listed.runs.map(runRow));
      showPage(listed, listBody.querySelectorAll('tr[data-run]').length);
    } catch (error) {
      listBody.append(h('tr', {}, h('td', { class: 'table-empty', colspan: '9' }, codeCopy(error?.error?.code ?? null, error?.error?.message))));
      more.hidden = true;
    }
    more.disabled = false;
  });

  // Reads only. While a run is still PENDING the same reads repeat, a bounded number of times and
  // only while this view is on screen; nothing here ever sends a collection.
  async function refresh() {
    window.clearTimeout(timer);
    const [focusPending, listPending] = await Promise.all([renderFocus(), renderList(), renderCounts()]);
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

  root.append(submitCard(view, ctx, () => refresh()), cards, h('div', { class: 'section-grid' }, runsPanel, preview), focus, factsSlot);
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
