// 수집관리 › 공급처 관리 › 「사이트 분석」 (ADR-0036 §5, slice O3).
//
// Live on an onboarded supplier's card while it is 분석 중 (RECON). The operator gives up to 5
// sample product URLs of the supplier's own storefront; the server reads each once, ever, as one
// paced trial (10 s between reads, and COLLECT's 60 s for the same product), keeps the page as a
// local capture and
// reports, per sample:
//   - each field: 읽음 / 없음 / 못 읽음, with the values the operator will verify;
//   - the unread rows and phrases, each with the slot a rule proposes (never AI);
//   - structural problems as "템플릿 수정 필요" (words cannot fix them).
// The observed hosts are listed once with 「확인」 for an image or login host. 「다시 분석」 re-reads
// the stored captures under the current words; it reads nothing from the supplier.
// The page decides nothing: every status, count and refusal is the server's. Each verifiable
// sample takes the operator's verdict on exactly the facts shown (「값이 맞음」/「값이 틀림」), and
// 「사용 시작」 asks the server's gate to move the supplier to 사용 중 (ACTIVE) once 3 distinct
// samples are confirmed with no structural problem (O5); the words are edited in 「수집 규칙」 (O4).
// No credential is ever shown.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { openModal } from '../../core/modal.js';
import { toast } from '../../core/toast.js';
import { ONBOARDING } from './onboarding.js';

export const FIELD_LABEL = {
  original_name: '상품명',
  prices: '가격',
  options: '옵션',
  images: '이미지',
  stock: '재고',
  shipping: '배송',
  minimum_sale_price: '최저판매가',
  quantity_tiers: '수량별 가격',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  notice: '상품정보고시',
  detail_description: '상세설명',
  sales_channels: '판매채널',
};

export const STATUS = {
  CONFIRMED: ['읽음', 'good'],
  ABSENT: ['없음', null],
  REVIEW_REQUIRED: ['못 읽음', 'warn'],
};

export const SLOT_LABEL = {
  name: '상품명',
  price: '가격',
  purchase_price: '매입가',
  list_price: '소비자가',
  minimum_price: '최저판매가',
  shipping_method: '배송방법',
  shipping_fee: '배송비',
  shipping_region_fee: '추가배송비',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  sales_channel_row: '판매채널 행',
  channel_all_allowed: '판매채널: 모두 허용',
  channel_closed_only: '판매채널: 폐쇄몰 전용',
  channel_forbid_coupang: '판매채널: 쿠팡 금지',
  channel_forbid_smartstore: '판매채널: 스마트스토어 금지',
  minimum_price_phrase: '최저판매가 문구',
  sold_out: '품절 표시',
  purchase_controls: '구매 버튼',
  title_suffix: '상품명 꼬리말',
};

const RULE_LABEL = {
  contains_word: '기본 단어 포함',
  similar_word: '기본 단어와 비슷함',
  money_row: '금액 한 개',
  channel_words: '판매채널 문구',
};

const STRUCTURAL_COPY = {
  SCRIPT_RENDERED: '상품 정보가 페이지 HTML에 없습니다(스크립트로 그려지는 페이지).',
  PRODUCT_BLOCK_NOT_FOUND: '상품 정보 영역을 찾지 못했습니다.',
  PRICE_BLOCK_NOT_FOUND: '가격 영역을 찾지 못했습니다.',
  PRICE_BLOCK_UNREADABLE: '가격 칸이 금액 하나로 읽히지 않습니다.',
  IDENTITY_UNRESOLVED: '페이지가 상품 번호를 일관되게 밝히지 않습니다.',
  DUPLICATED_ORDER_LIST: '상품 영역이나 주문 목록이 중복되어 있습니다.',
  CAPTURE_DIVERGES: '저장본이 페이지와 다르게 읽힙니다(상품 영역 밖의 내용을 템플릿이 읽음). 이 샘플로는 승인할 수 없습니다.',
};

const ROLE_LABEL = { image: '이미지', login: '로그인', asset: '자산(수집에 쓰이지 않음)' };

const ERROR_COPY = {
  ONBOARDING_SAMPLE_REFUSED: '이 공급처 쇼핑몰의 상품 페이지 주소만 샘플로 읽습니다.',
  ONBOARDING_SAMPLES_FULL: '샘플은 실패한 읽기를 포함해 최대 5개까지입니다.',
  ONBOARDING_SAMPLE_ALREADY_READ: '이미 읽은 샘플입니다. 샘플은 한 번만 읽고, 수집 규칙이 바뀌면 다시 분석으로 저장본을 다시 봅니다.',
  ONBOARDING_TRIAL_PENDING: '이 샘플은 이미 읽기 대기 중입니다.',
  ONBOARDING_TRIAL_TOO_SOON: '너무 이른 읽기입니다. 읽기 사이에는 10초를 둡니다.',
  ONBOARDING_NOT_RECON: '분석 중(RECON)인 공급처만 사이트 분석을 합니다.',
  ONBOARDING_SAMPLE_UNREAD: '상품 페이지 대신 다른 응답(로그인 이동 등)을 받았습니다. 연결 테스트 후 다시 읽으세요.',
  ONBOARDING_SITE_UNBOUND: '이 공급처는 지금 연결할 수 없습니다.',
  ONBOARDING_HOST_ROLE_UNOBSERVED: '분석에서 그 역할로 관찰된 호스트만 확인할 수 있습니다.',
  ONBOARDING_HOST_REFUSED: '허용되지 않는 호스트입니다(IP·포트·사설 주소 불가).',
  ONBOARDING_HOST_UNCHANGED: '이미 확인된 호스트입니다.',
  ONBOARDING_WORDS_MOVED: '분석 중에 수집 규칙이 바뀌었습니다. 다시 분석하세요.',
  ONBOARDING_TRIAL_NOT_VERIFIABLE: '핵심 필드를 모두 읽고 템플릿 수정이 필요 없는 샘플만 맞다고 확인할 수 있습니다.',
  ONBOARDING_TRIAL_NOT_CURRENT: '지금 수집 규칙의 결과가 아닙니다. 「다시 분석」 후 확인하세요.',
  ONBOARDING_VERIFICATION_STALE: '확인한 값이 지금 결과와 다릅니다. 창을 닫았다 다시 여세요.',
  ONBOARDING_SAMPLE_RETIRED: '이미 분석에서 제외한 샘플입니다.',
  ONBOARDING_RETIREMENT_REASON_INVALID: '제외 사유를 1~200자로 적어 주세요.',
  ONBOARDING_ACTIVATION_NOT_PROVEN: '아직 사용 시작 조건(서로 다른 샘플 3개 값 확인, 템플릿 수정 필요 없음)을 채우지 못했습니다.',
};

export function message(error, fallback) {
  const code = error?.error?.code;
  return ERROR_COPY[code] ?? fallback?.(error) ?? error?.error?.message ?? String(error?.message ?? error);
}

const IMAGE_ROLE = { PRIMARY: '대표', THUMBNAIL: '추가', DETAIL: '상세', PRODUCT_AUX: '보조' };

function won(amount) {
  return `${Number(amount).toLocaleString('ko-KR')}원`;
}

// ADR-0038 §1: a source-currency price is the page's own decimal text, in its currency.
const CURRENCY_FORMAT = { CNY: (amount) => `¥${amount}`, JPY: (amount) => `${amount}円` };

function price(p) {
  if (!p.currency) return won(p.amount_krw);
  return CURRENCY_FORMAT[p.currency]?.(p.amount) ?? `${p.amount} ${p.currency}`;
}

function valueText(field, summary, note) {
  if (!summary) return note ?? '-';
  switch (field) {
    case 'original_name':
      return summary.name ?? note ?? '-';
    case 'prices':
      return summary.prices?.length ? summary.prices.map((p) => `${p.label} ${price(p)}`).join(' · ') : note ?? '-';
    case 'options': {
      if (summary.options?.status === 'ABSENT') return '옵션 없음';
      const value = summary.options?.value;
      if (value?.axes?.length) {
        const axes = value.axes.map((a) => `${a.name}: ${a.values.join('/')}`).join(' · ');
        return `${axes} (조합 ${value.configurations?.length ?? 0}개)`;
      }
      return summary.options?.observed?.length ? summary.options.observed.join(' · ') : note ?? '-';
    }
    case 'stock':
      return summary.stock === 'ON_SALE' ? '판매 중' : summary.stock === 'SOLD_OUT' ? '품절' : note ?? '-';
    case 'images': {
      const items = summary.images?.items ?? [];
      const roles = {};
      for (const item of items) roles[item.role] = (roles[item.role] ?? 0) + 1;
      const detail = Object.entries(roles)
        .map(([role, count]) => `${IMAGE_ROLE[role] ?? role} ${count}`)
        .join(' · ');
      return detail ? `${summary.images?.count ?? 0}장 (${detail})` : `${summary.images?.count ?? 0}장`;
    }
    default:
      return note ?? '-';
  }
}

function statusChip(status) {
  const [label, tone] = STATUS[status] ?? [status, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-status': status }, label);
}

function fieldTable(sample) {
  return h(
    'table',
    { class: 'table analysis-fields', 'data-role': 'field-table' },
    h('thead', {}, h('tr', {}, h('th', {}, '필드'), h('th', {}, '상태'), h('th', {}, '값 요약'))),
    h(
      'tbody',
      {},
      sample.fields.map((f) =>
        h(
          'tr',
          { 'data-field': f.field },
          h('td', {}, FIELD_LABEL[f.field] ?? f.field, f.core ? h('span', { class: 'mini' }, ' 핵심') : null),
          h('td', {}, statusChip(f.status)),
          h('td', { class: 'analysis-value' }, valueText(f.field, sample.summary, f.note)),
        ),
      ),
    ),
  );
}

function unreadTable(sample) {
  if (!sample.unread_rows.length) return h('div', { class: 'mini', 'data-role': 'unread-none' }, '읽지 못한 행이 없습니다.');
  return h(
    'table',
    { class: 'table analysis-unread', 'data-role': 'unread-rows' },
    h('thead', {}, h('tr', {}, h('th', {}, '읽지 못한 행/문구'), h('th', {}, '값'), h('th', {}, '제안 슬롯'))),
    h(
      'tbody',
      {},
      sample.unread_rows.map((u) =>
        h(
          'tr',
          { 'data-unread': u.label, 'data-kind': u.kind },
          h('td', {}, u.kind === 'phrase' ? `${u.label} (문구)` : u.label),
          h('td', { class: 'analysis-value' }, u.value || '-'),
          h(
            'td',
            { 'data-candidate': u.candidate ?? '' },
            u.candidate ? `${SLOT_LABEL[u.candidate] ?? u.candidate} · ${RULE_LABEL[u.rule] ?? u.rule}` : '제안 없음',
          ),
        ),
      ),
    ),
  );
}

// ADR-0036 §7 as amended (6160586049, O5b): a sample may be retired from the analysis with the
// operator's reason; it then neither proves nor blocks and stays shown, with its warning.
function retirementLine(sample, retire) {
  if (sample.retired_reason) {
    return h(
      'div',
      { class: 'note', 'data-role': 'retired' },
      h('span', { class: 'chip', 'data-retired': 'true' }, '제외됨'),
      ` 분석에서 제외했습니다(사유: ${sample.retired_reason}). 승인에 쓰이지도, 막지도 않습니다.`,
    );
  }
  if (sample.verdict === 'CORRECT') return null;
  return h(
    'div',
    { class: 'analysis-add' },
    h('button', { type: 'button', class: 'btn', 'data-action': 'retire-sample', onclick: () => retire(sample) }, '분석에서 제외'),
    h('span', { class: 'mini' }, '이 샘플만 읽기 어려운 상품이면 사유를 남기고 제외할 수 있습니다.'),
  );
}

// ADR-0036 §7 (O5): the operator's verdict on exactly the facts this sample showed.
function verdictLine(sample, verify) {
  if (sample.retired_reason) return null;
  if (sample.verdict === 'CORRECT') return h('div', { 'data-role': 'verdict' }, h('span', { class: 'chip good', 'data-verdict': 'CORRECT' }, '값 확인함'));
  if (sample.verdict === 'WRONG') {
    return h(
      'div',
      { 'data-role': 'verdict' },
      h('span', { class: 'chip bad', 'data-verdict': 'WRONG' }, '값 틀림'),
      h('span', { class: 'mini' }, ' 이 샘플로는 승인할 수 없습니다. 수집 규칙을 고쳐 다시 분석하세요.'),
    );
  }
  if (!sample.verifiable) return null;
  return h(
    'div',
    { class: 'analysis-add', 'data-role': 'verdict' },
    h('span', { class: 'mini' }, '위 값이 실제 상품 페이지와 같습니까?'),
    h('button', { type: 'button', class: 'btn blue', 'data-action': 'verify-correct', onclick: () => verify(sample, 'CORRECT') }, '값이 맞음'),
    h('button', { type: 'button', class: 'btn', 'data-action': 'verify-wrong', onclick: () => verify(sample, 'WRONG') }, '값이 틀림'),
  );
}

function sampleBlock(sample, verify, retire) {
  const images = sample.summary?.images?.urls ?? [];
  return h(
    'div',
    { class: sample.retired_reason ? 'analysis-sample retired' : 'analysis-sample', 'data-sample': sample.sample_url },
    h(
      'div',
      { class: 'analysis-sample-head' },
      h('b', { class: 'analysis-url' }, sample.sample_url),
      h(
        'span',
        { class: 'supplier-chips' },
        sample.summary?.product_id ? h('span', { class: 'chip' }, `상품번호 ${sample.summary.product_id}`) : null,
        sample.verifiable
          ? h('span', { class: 'chip good', 'data-role': 'verifiable' }, '확인 가능')
          : h('span', { class: 'chip warn', 'data-role': 'not-verifiable' }, '확인 불가'),
      ),
    ),
    sample.structural.map((code) =>
      h(
        'div',
        { class: 'note', 'data-structural': code },
        h('span', { class: 'chip bad' }, '템플릿 수정 필요'),
        ' ',
        STRUCTURAL_COPY[code] ?? code,
      ),
    ),
    sample.summary === null && !sample.structural.length
      ? h('div', { class: 'note' }, '저장된 캡처로 이 결과를 다시 만들 수 없습니다. 「다시 분석」 또는 다시 읽으세요.')
      : null,
    fieldTable(sample),
    images.length ? h('div', { class: 'mini analysis-images', 'data-role': 'image-urls' }, images.join('\n')) : null,
    verdictLine(sample, verify),
    retirementLine(sample, retire),
    unreadTable(sample),
  );
}

function hostTable(view, act) {
  if (!view.observed_hosts.length) return h('div', { class: 'mini' }, '관찰된 호스트가 없습니다.');
  return h(
    'table',
    { class: 'table analysis-hosts', 'data-role': 'observed-hosts' },
    h('thead', {}, h('tr', {}, h('th', {}, '호스트'), h('th', {}, '역할'), h('th', {}, '횟수'), h('th', {}, ''))),
    h(
      'tbody',
      {},
      view.observed_hosts.map((host) => {
        let action = h('span', { class: 'mini' }, host.refusal ? `허용 불가(${host.refusal})` : '-');
        if (host.confirmed) {
          action = h(
            'span',
            { class: 'supplier-chips' },
            h('span', { class: 'chip good' }, '확인됨'),
            h('button', { type: 'button', class: 'btn', 'data-action': 'disable-host', onclick: () => act('disable', host) }, '해제'),
          );
        } else if (host.confirmable) {
          action = h('button', { type: 'button', class: 'btn', 'data-action': 'confirm-host', onclick: () => act('confirm', host) }, '확인');
        }
        return h(
          'tr',
          { 'data-host': host.host, 'data-role-name': host.role },
          h('td', { class: 'analysis-value' }, host.host),
          h('td', {}, ROLE_LABEL[host.role] ?? host.role),
          h('td', {}, String(host.count)),
          h('td', {}, action),
        );
      }),
    ),
  );
}

export async function openSiteAnalysis(record, { waitForJob, errorMessage, onChanged }) {
  const key = record.supplier_key;
  const base = `${ONBOARDING}/suppliers/${key}`;
  const url = h('input', {
    type: 'url',
    autocomplete: 'off',
    'aria-label': '샘플 상품 URL',
    'data-role': 'sample-url',
    placeholder: `${record.origin}/… 상품 페이지 주소`,
  });
  const add = h('button', { type: 'button', class: 'btn blue', 'data-action': 'add-sample' }, '샘플 읽기');
  const reanalyse = h('button', { type: 'button', class: 'btn', 'data-action': 'reanalyse' }, '다시 분석');
  // 「사용 시작」 (O5): enabled only while the server's gate says the site can start.
  const start = h('button', { type: 'button', class: 'btn blue', 'data-action': 'activate', disabled: true }, '사용 시작');
  const status = h('div', { class: 'note', 'data-role': 'analysis-status', hidden: true });
  const progress = h('div', { class: 'analysis-progress', 'data-role': 'analysis-progress' });
  const samples = h('div', { class: 'analysis-samples', 'data-role': 'samples' });
  const hosts = h('div', { 'data-role': 'hosts' });
  let busy = false;
  let view = null;

  const say = (text) => {
    status.hidden = !text;
    status.textContent = text ?? '';
  };
  const refreshButtons = () => {
    const full = (view?.samples_taken ?? 0) >= (view?.sample_limit ?? 5);
    add.disabled = busy || full;
    add.title = full ? '샘플 5개를 모두 썼습니다(실패한 읽기 포함). 수집 규칙이 바뀌면 다시 분석으로 저장본을 다시 봅니다.' : '';
    reanalyse.disabled = busy || !view?.stale.length;
    start.disabled = busy || !view?.can_activate;
  };

  async function act(kind, host) {
    busy = true;
    refreshButtons();
    try {
      if (kind === 'confirm') {
        await sendJson('POST', `${base}/hosts`, { host: host.host, role: host.role, trial_id: host.trial_id });
        toast('사이트 분석', `${host.host} 호스트를 ${ROLE_LABEL[host.role] ?? host.role} 호스트로 확인했습니다.`);
      } else {
        await sendJson('POST', `${base}/hosts/disable`, { host: host.host, role: host.role });
        toast('사이트 분석', `${host.host} 호스트 확인을 해제했습니다.`);
      }
      await load();
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  }

  function render() {
    const ready = view.verifiable_samples;
    const required = view.activation.required_samples;
    const proven = view.activation.proven_samples;
    start.title = view.can_activate
      ? '확인한 샘플로 이 공급처의 수집을 시작합니다.'
      : `서로 다른 샘플 ${required}개의 값을 확인해야 하고, 템플릿 수정이 필요한 샘플이 없어야 합니다.`;
    progress.replaceChildren(
      h('b', { 'data-role': 'ready-count' }, `샘플 ${ready}/${required} 확인 가능`),
      h('b', { 'data-role': 'proven-count' }, ` · 값 확인 ${proven}/${required}`),
      h(
        'span',
        { class: 'mini' },
        ` · 현재 수집 규칙 w${view.words_seq} · 템플릿 ${view.template_revision} · 샘플 ${view.samples_taken}/${view.sample_limit}`,
      ),
      view.state === 'RECON' ? start : h('span', { class: 'chip good', 'data-role': 'active' }, '사용 중'),
    );
    samples.replaceChildren(
      ...view.samples.map((sample) => sampleBlock(sample, verify, retire)),
      ...view.stale.map((s) =>
        s.retired_trial
          ? h(
              'div',
              { 'data-stale': 'true', 'data-stale-sample': s.sample_url },
              h('div', { class: 'note' }, `이전 수집 규칙(w${s.words_seq})의 결과입니다. 분석에서 제외한 샘플이라 그때의 값과 경고를 그대로 보여 줍니다.`),
              sampleBlock(s.retired_trial, verify, retire),
            )
          : h(
              'div',
              { class: 'analysis-sample', 'data-sample': s.sample_url, 'data-stale': 'true' },
              h('b', { class: 'analysis-url' }, s.sample_url),
              h('div', { class: 'note' }, `이전 수집 규칙(w${s.words_seq})의 결과입니다. 「다시 분석」으로 저장된 캡처를 다시 읽습니다.`),
            ),
      ),
    );
    if (!view.samples.length && !view.stale.length) {
      samples.replaceChildren(h('div', { class: 'mini', 'data-role': 'no-samples' }, '아직 읽은 샘플이 없습니다. 상품 페이지 주소를 넣고 「샘플 읽기」를 누르세요.'));
    }
    hosts.replaceChildren(hostTable(view, act));
    refreshButtons();
  }

  async function load() {
    view = await getJson(`${base}/trials`);
    render();
  }

  async function verify(sample, verdict) {
    busy = true;
    refreshButtons();
    try {
      view = await sendJson('POST', `${base}/trials/${sample.trial_id}/verification`, {
        verdict,
        shown_fingerprint: sample.facts_fingerprint,
      });
      render();
      say(verdict === 'CORRECT' ? '값이 맞다고 기록했습니다.' : '값이 틀리다고 기록했습니다. 이 샘플은 승인에 쓰이지 않습니다.');
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  }

  start.addEventListener('click', () => activate());

  async function retire(sample) {
    const reason = window.prompt(
      `${sample.sample_url}\n이 샘플을 분석에서 제외할까요? 사유를 적어 주세요(되돌릴 수 없고, 화면에는 계속 남습니다).`,
    );
    if (reason === null) return;
    if (!reason.trim()) return say('제외 사유를 적어 주세요.');
    busy = true;
    refreshButtons();
    try {
      view = await sendJson('POST', `${base}/samples/retire`, { sample_url: sample.sample_url, reason: reason.trim() });
      render();
      say('샘플을 분석에서 제외했습니다. 남은 샘플로 승인을 판단합니다.');
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  }

  async function activate() {
    if (!window.confirm(`${record.display_name}의 수집을 시작할까요?\n확인한 샘플과 지금 수집 규칙으로 일반 수집에 쓰입니다.`)) return;
    busy = true;
    refreshButtons();
    try {
      view = await sendJson('POST', `${base}/activate`);
      render();
      say('사용 중으로 바꿨습니다. 이제 이 공급처의 상품을 일반 수집으로 가져올 수 있습니다.');
      toast('사이트 분석', `${record.display_name} 사용 시작`);
      onChanged?.();
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  }

  add.addEventListener('click', async () => {
    const sample = url.value.trim();
    if (!sample) return say('샘플 상품 URL을 입력하세요.');
    busy = true;
    refreshButtons();
    say('샘플 상품 페이지를 한 번 읽는 중입니다…');
    try {
      const queued = await sendJson('POST', `${base}/trials`, { sample_url: sample });
      const job = await waitForJob(queued.job_id);
      if (job?.state !== 'SUCCEEDED') {
        say(job ? message({ error: { code: job.last_error_code } }, errorMessage) : '읽기 결과를 받지 못했습니다.');
      } else {
        url.value = '';
        say(null);
      }
      await load();
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  });

  reanalyse.addEventListener('click', async () => {
    busy = true;
    refreshButtons();
    try {
      const result = await sendJson('POST', `${base}/reanalyse`);
      view = result.analysis;
      render();
      say(`저장된 캡처 ${result.analysed}개를 현재 수집 규칙으로 다시 분석했습니다. 공급처에는 요청하지 않았습니다.`);
    } catch (error) {
      say(message(error, errorMessage));
    } finally {
      busy = false;
      refreshButtons();
    }
  });

  openModal({
    eyebrow: 'COLLECT · Site Analysis',
    title: `사이트 분석 · ${record.display_name}`,
    help: '샘플 상품 페이지를 한 번만 읽어 각 필드를 읽었는지 보여 줍니다. 읽기 사이에는 10초를 두고, 같은 상품은 일반 수집과 합쳐 60초 안에 다시 읽지 않으며, 이미지는 내려받지 않습니다.',
    body: h(
      'div',
      { class: 'analysis', 'data-role': 'site-analysis' },
      progress,
      h('div', { class: 'analysis-add' }, url, add, reanalyse),
      status,
      samples,
      h('h3', { class: 'panel-title analysis-hosts-title' }, '관찰된 호스트'),
      h('div', { class: 'mini' }, '이미지·로그인 호스트를 확인하면 이 공급처의 수집 허용 호스트에 추가됩니다. 언제든 해제할 수 있습니다.'),
      hosts,
    ),
  });
  refreshButtons();
  try {
    await load();
  } catch (error) {
    say(message(error, errorMessage));
  }
}
