// ADR-0038 §3, §5: 설정 › 공통 › 해외구매대행. Two live panels over their own contracts:
// - the 해외 구매대행 settings: the server holds every revision and its defaults; the card shows the
//   current values and saves them with the revision it was read at (a moved revision is refused by
//   the server and read again). Every value is validated by the server; nothing is priced here (O2);
// - 환율: the Korea Eximbank key is write-only (the server answers only 설정됨/미설정, and the input
//   is cleared after every request), and the newest TTS of each currency is the server's snapshot
//   with its publication date and staleness. 「지금 갱신」 asks the server to read the publication.

import { getJson, sendJson } from '../../core/api.js';
import { dotDate, dotDateTime } from '../../core/format.js';
import { h } from '../../core/dom.js';
import { toast } from '../../core/toast.js';

const SETTINGS = '/api/v1/settings/overseas';
const FX = '/api/v1/settings/overseas/fx';
const FX_KEY = '/api/v1/settings/overseas/fx-key';
const FX_REFRESH = '/api/v1/settings/overseas/fx/refresh';

const COPY = {
  OVERSEAS_SETTINGS_REVISION_MOVED: '다른 곳에서 설정이 바뀌었습니다. 새로 불러온 값을 확인한 뒤 다시 저장하세요.',
  OVERSEAS_SETTINGS_UNCHANGED: '바뀐 설정이 없습니다.',
  REQUEST_INVALID: '입력값을 확인하세요. 비율은 0~100(소수 둘째 자리까지), 배송기간은 1~60일이며 최소가 최대보다 클 수 없습니다.',
  FX_API_KEY_INVALID: 'API 키 형식이 올바르지 않습니다 (영문·숫자 16자 이상).',
  FX_API_KEY_MISSING: '환율 API 키를 먼저 저장하세요.',
  FX_REFRESH_BUSY: '이미 환율을 받는 중입니다. 잠시 후 다시 시도하세요.',
  FX_SOURCE_AUTHKEY_INVALID: '수출입은행이 API 키를 거부했습니다. 키를 확인하세요.',
  FX_SOURCE_DAILY_LIMIT: '오늘 API 호출 한도를 넘었습니다. 내일 다시 시도하세요.',
  FX_SOURCE_DATA_CODE_ERROR: '수출입은행 API가 요청을 거부했습니다.',
  FX_SOURCE_MALFORMED: '수출입은행 응답을 읽을 수 없어 아무것도 기록하지 않았습니다.',
  FX_SOURCE_CURRENCY_MISSING: '응답에 필요한 통화(CNH, JPY(100), USD)가 없어 기록하지 않았습니다.',
  FX_SOURCE_TRANSPORT_FAILED: '수출입은행에 연결하지 못했습니다.',
  FX_SOURCE_EGRESS_BLOCKED: '외부 연결이 차단되었습니다.',
};
const OUTCOME_COPY = {
  RECORDED: '새 환율을 받았습니다.',
  ALREADY_RECORDED: '이미 가장 최근 고시 환율을 받아 두었습니다.',
  NOT_PUBLISHED: '아직 고시된 환율이 없습니다. 영업일 11시 이후 다시 시도하세요.',
};
const CURRENCY_LABEL = { CNY: '중국 위안 (CNH)', JPY: '일본 엔 (JPY 100엔당)', USD: '미국 달러 (USD)' };
const REASON_LABEL = { PRICING_FX_STALE: '오래됨 · 가격 계산 안 함', PRICING_FX_UNAVAILABLE: '환율 없음 · 가격 계산 안 함' };

function errorText(error) {
  const code = error?.error?.code;
  return COPY[code] ?? error?.error?.message ?? String(error?.message ?? error);
}

function grouped(decimal) {
  const [whole, fraction] = String(decimal).split('.');
  const digits = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return fraction === undefined ? digits : `${digits}.${fraction}`;
}

// A whole number as typed, or the text itself so the server refuses it; blank is unset (null).
function wholeOrText(value) {
  const text = value.trim();
  if (!text) return null;
  return /^\d+$/.test(text) ? Number(text) : text;
}

function textOrNull(value) {
  const text = value.trim();
  return text ? text : null;
}

function inputRow(id, label, value, extra = {}) {
  const input = h('input', { id, type: 'text', autocomplete: 'off', value: value ?? '', ...extra });
  return { input, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), input) };
}

function changedLine(view) {
  if (view.revision_no === null || view.revision_no === undefined) return '기본값 (변경 기록 없음)';
  return `${view.changed_by ?? '-'} · ${view.changed_at ? dotDateTime(view.changed_at) : '-'} · ${view.revision_no}차 변경`;
}

export function overseasSettingsPanel() {
  const host = h('div', { class: 'overseas-settings', 'data-role': 'overseas-settings', 'data-state': 'loading' }, h('span', { class: 'mini' }, '설정을 불러오는 중…'));

  const render = (view) => {
    host.dataset.state = 'ready';
    host.dataset.revision = view.revision_no === null || view.revision_no === undefined ? '' : String(view.revision_no);
    const margin = inputRow('overseas-fx-margin', '환율 마진 (%)', view.fx_margin_percent, { inputmode: 'decimal', 'data-field': 'fx_margin_percent' });
    const shipping = inputRow('overseas-shipping-default', '기본 해외배송비 (원)', view.overseas_shipping_default_krw, {
      inputmode: 'numeric',
      placeholder: '미설정 · 상품마다 입력',
      'data-field': 'overseas_shipping_default_krw',
    });
    const duty = inputRow('overseas-duty-rate', '기본 관세율 (%)', view.duty_rate_percent, { inputmode: 'decimal', 'data-field': 'duty_rate_percent' });
    const address = inputRow('overseas-address-id', '해외 출고지 번호', view.smartstore_overseas_shipping_address_id, {
      inputmode: 'numeric',
      placeholder: '미설정 · 스마트스토어 주소록 번호',
      'data-field': 'smartstore_overseas_shipping_address_id',
    });
    const daysMin = inputRow('overseas-days-min', '배송기간 최소 (일)', view.overseas_delivery_days_min, { inputmode: 'numeric', 'data-field': 'overseas_delivery_days_min' });
    const daysMax = inputRow('overseas-days-max', '배송기간 최대 (일)', view.overseas_delivery_days_max, { inputmode: 'numeric', 'data-field': 'overseas_delivery_days_max' });
    const importer = inputRow('overseas-importer', '수입자 표기', view.importer_label, { maxlength: '50', placeholder: '미설정', 'data-field': 'importer_label' });
    let duties = view.duties_included_over_usd150;
    const dutiesSwitch = h('button', {
      type: 'button',
      role: 'switch',
      class: duties ? 'toggle' : 'toggle off',
      'aria-checked': String(duties),
      'aria-label': '150달러 초과 시 관부가세 포함',
      'data-switch': 'duties_included_over_usd150',
    });
    dutiesSwitch.addEventListener('click', () => {
      duties = !duties;
      dutiesSwitch.className = duties ? 'toggle' : 'toggle off';
      dutiesSwitch.setAttribute('aria-checked', String(duties));
    });
    const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-overseas-settings' }, '설정 저장');
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const saved = await sendJson('PUT', SETTINGS, {
          expected_revision_no: view.revision_no ?? null,
          fx_margin_percent: margin.input.value.trim(),
          overseas_shipping_default_krw: wholeOrText(shipping.input.value),
          duties_included_over_usd150: duties,
          duty_rate_percent: duty.input.value.trim(),
          smartstore_overseas_shipping_address_id: textOrNull(address.input.value),
          overseas_delivery_days_min: wholeOrText(daysMin.input.value),
          overseas_delivery_days_max: wholeOrText(daysMax.input.value),
          importer_label: textOrNull(importer.input.value),
        });
        toast('해외 구매대행', '저장했습니다.');
        render(saved);
      } catch (error) {
        toast('해외 구매대행', errorText(error));
        save.disabled = false;
        // A moved revision is read again: a refused save never leaves a stale view behind.
        if (error?.error?.code === 'OVERSEAS_SETTINGS_REVISION_MOVED') load();
      }
    });
    host.replaceChildren(
      margin.row,
      shipping.row,
      h('div', { class: 'kv' }, h('span', {}, '150달러 초과 시 관부가세 포함'), dutiesSwitch),
      duty.row,
      address.row,
      daysMin.row,
      daysMax.row,
      importer.row,
      h('div', { class: 'kv', 'data-role': 'overseas-changed' }, h('span', {}, '변경'), h('b', {}, changedLine(view))),
      h('div', { class: 'note' }, '150달러 기준은 ICBM이 관리합니다. 관부가세를 빼면 구매자가 부담한다고 표시됩니다. 판매 가능 여부는 판매자가 판단합니다.'),
      h('div', { class: 'api-action-row' }, save),
    );
  };
  const load = () =>
    getJson(SETTINGS)
      .then(render)
      .catch((error) => {
        host.dataset.state = 'error';
        host.replaceChildren(h('span', { class: 'mini' }, errorText(error)));
      });
  load();
  return host;
}

function rateRow(rate) {
  const value = rate.tts
    ? `${grouped(rate.tts)}원 / ${rate.unit === 100 ? '100' : '1'} ${rate.currency} · ${dotDate(rate.publication_date)} 고시`
    : '받은 환율 없음';
  const chip = rate.reason
    ? h('span', { class: 'chip warn', 'data-reason': rate.reason }, REASON_LABEL[rate.reason] ?? rate.reason)
    : h('span', { class: 'chip good' }, '사용 가능');
  return h(
    'div',
    { class: 'kv', 'data-role': 'fx-rate', 'data-currency': rate.currency },
    h('span', {}, CURRENCY_LABEL[rate.currency] ?? rate.currency),
    h('b', {}, value, ' ', chip),
  );
}

export function fxRatesPanel() {
  const keyChip = h('span', { class: 'chip', 'data-role': 'fx-key-status' }, '확인 중');
  const keyInput = h('input', {
    id: 'fx-api-key',
    type: 'password',
    autocomplete: 'new-password',
    spellcheck: 'false',
    placeholder: '한국수출입은행 Open API 인증키',
  });
  const saveKey = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-fx-key' }, '키 저장');
  const deleteKey = h('button', { type: 'button', class: 'btn', 'data-action': 'delete-fx-key', disabled: true }, '키 삭제');
  const rates = h('div', { class: 'fx-rates', 'data-role': 'fx-rates', 'data-state': 'loading' }, h('span', { class: 'mini' }, '환율을 불러오는 중…'));
  const refresh = h('button', { type: 'button', class: 'btn blue', 'data-action': 'refresh-fx' }, '지금 갱신');

  const showKey = (view) => {
    keyChip.className = view.configured ? 'chip good' : 'chip';
    keyChip.textContent = view.configured ? '설정됨' : '미설정';
    deleteKey.disabled = !view.configured;
  };
  const showRates = (status) => {
    rates.dataset.state = 'ready';
    rates.replaceChildren(...status.rates.map(rateRow));
    showKey({ configured: status.key_configured });
  };
  const loadRates = () =>
    getJson(FX)
      .then(showRates)
      .catch((error) => {
        rates.dataset.state = 'error';
        rates.replaceChildren(h('span', { class: 'mini' }, errorText(error)));
      });

  getJson(FX_KEY)
    .then(showKey)
    .catch(() => {
      keyChip.className = 'chip warn';
      keyChip.textContent = '확인 불가';
    });
  loadRates();

  saveKey.addEventListener('click', async () => {
    saveKey.disabled = true;
    try {
      showKey(await sendJson('PUT', FX_KEY, { key: keyInput.value.trim() }));
      toast('환율 API 키', 'Windows 보안 저장소에 저장했습니다.');
    } catch (error) {
      toast('환율 API 키', errorText(error));
    } finally {
      keyInput.value = '';
      saveKey.disabled = false;
    }
  });
  deleteKey.addEventListener('click', async () => {
    deleteKey.disabled = true;
    try {
      showKey(await sendJson('DELETE', FX_KEY));
      toast('환율 API 키', '삭제했습니다.');
    } catch (error) {
      toast('환율 API 키', errorText(error));
      deleteKey.disabled = false;
    }
  });
  refresh.addEventListener('click', async () => {
    refresh.disabled = true;
    try {
      const result = await sendJson('POST', FX_REFRESH);
      showRates(result.status);
      toast('환율', result.outcome === 'FAILED' ? COPY[result.code] ?? result.code : OUTCOME_COPY[result.outcome] ?? result.outcome);
    } catch (error) {
      toast('환율', errorText(error));
    } finally {
      refresh.disabled = false;
    }
  });

  return h(
    'div',
    { class: 'fx-panel', 'data-role': 'fx-panel' },
    h('div', { class: 'form-row' }, h('label', { for: keyInput.id }, '환율 API 키'), keyInput),
    h('div', { class: 'kv' }, h('span', {}, '키 저장 상태'), keyChip),
    h('div', { class: 'api-action-row' }, saveKey, deleteKey),
    rates,
    h('div', { class: 'api-action-row' }, refresh),
  );
}
