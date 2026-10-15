// ADR-0042 S1a (§0 a1–a2, b1; §5.4): the connected store's catalog sync, beside 계정 확인.
// Read-only towards the marketplace: the card shows what the server's StoreCatalog owner reports —
// the running sync and its progress (스토어 상품 가져오는 중 340 / 1,200), the last sync whatever
// its outcome, and always the last complete one (마지막 동기화, SCP-07). A running or failed sync
// is shown as such, never as an old success. 지금 동기화 asks the server for one read-only sync.
// The listing table itself is S1b (통합DB 「스토어 상품」).
// 「문제 해결용 정보」 (ADR-0042 §5.1 item 3) is collapsed and shows only whether a response
// shape was recorded and how many paths it has; the shape itself is never rendered here.

import { ApiError, getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { dotDateTime } from '../../core/format.js';
import { toast } from '../../core/toast.js';

const STATUS = '/api/v1/operate/store-catalog';
const SYNC = '/api/v1/operate/store-catalog/sync';
// While a sync runs the card re-reads its progress at this pace.
const POLL_MS = 3000;

const OUTCOME_LABEL = {
  COMPLETE: '완료',
  INCOMPLETE: '일부만 확인됨 (결론 없음)',
  UNAVAILABLE: '지금은 확인할 수 없음',
  ERROR: '확인 실패',
  INTERRUPTED: '중단됨',
};
const ERROR_LABEL = {
  SMARTSTORE_SESSION_UNAVAILABLE: '스마트스토어 연결 필요 (위의 계정 확인)',
  'GW.RATE_LIMIT': '호출 한도로 잠시 멈춤',
  'GW.QUOTA_LIMIT': '호출 한도로 잠시 멈춤',
  SMARTSTORE_CATALOG_TOO_LARGE: '상품이 100,000개를 넘어 가져올 수 없음',
  STORE_CATALOG_DETAIL_READ_FAILED: '일부 상품을 읽지 못함',
  STORE_CATALOG_DETAILS_STALE: '하루 넘게 다시 읽지 못한 상품이 있음 · 지금 동기화로 모두 다시 읽기',
  STORE_CATALOG_LISTING_OUTSIDE_ENUMERATION: '목록에 없는 판매 상품이 있음',
  STORE_CATALOG_SYNC_INTERRUPTED: '앱이 중간에 종료됨',
};

function count(value) {
  return typeof value === 'number' ? value.toLocaleString('ko-KR') : '—';
}

function line(status) {
  const running = status.running;
  if (running) {
    if (running.details_target > 0) {
      const done = running.details_done + running.details_failed;
      return `스토어 상품 가져오는 중 ${count(done)} / ${count(running.details_target)}`;
    }
    return '스토어 상품 확인 중…';
  }
  const complete = status.last_complete;
  if (complete) {
    return `스토어 상품 ${count(status.listing_count)}개 · 마지막 동기화 ${dotDateTime(complete.as_of)}`;
  }
  return '아직 동기화한 적 없음';
}

function lastRun(status) {
  const last = status.last_run;
  if (!last || last.state === 'RUNNING' || last.outcome === 'COMPLETE') return null;
  const reason = ERROR_LABEL[last.error_code] ?? last.error_code ?? '';
  return h(
    'div',
    { class: 'kv', 'data-role': 'store-catalog-last-run' },
    h('span', {}, `최근 동기화 ${dotDateTime(last.as_of)}`),
    h('b', {}, [OUTCOME_LABEL[last.outcome] ?? last.outcome, reason].filter(Boolean).join(' · ')),
  );
}

export function storeCatalogPanel() {
  const summary = h('div', { class: 'kv', 'data-role': 'store-catalog-status' }, h('span', {}, '스토어 상품'), h('b', {}, '확인 중'));
  const failure = h('div');
  const shapeLine = h('div', { class: 'kv', 'data-role': 'store-catalog-shape' });
  const troubleshooting = h(
    'details',
    { class: 'store-catalog-troubleshooting' },
    h('summary', {}, '문제 해결용 정보'),
    shapeLine,
  );
  const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'sync-store-catalog' }, '지금 동기화');
  let timer = null;

  const render = (status) => {
    if (!status.marketplace_account_id) {
      summary.replaceChildren(h('span', {}, '스토어 상품'), h('b', {}, '스마트스토어 연결 후 자동으로 가져옵니다'));
      failure.replaceChildren();
      button.disabled = true;
      return;
    }
    summary.replaceChildren(h('span', {}, '스토어 상품'), h('b', {}, line(status)));
    const shown = lastRun(status);
    failure.replaceChildren(...(shown ? [shown] : []));
    button.disabled = Boolean(status.running);
    shapeLine.replaceChildren(
      h('span', {}, '응답 구조'),
      h(
        'b',
        {},
        typeof status.shape_path_count === 'number'
          ? `응답 구조 기록됨 · 경로 ${count(status.shape_path_count)}개 · ${dotDateTime(status.shape_recorded_at)}`
          : '아직 기록 없음 (지금 동기화 때 기록)',
      ),
    );
  };

  const reload = async () => {
    clearTimeout(timer);
    try {
      const found = await getJson(STATUS);
      render(found.status);
      if (found.status.running && summary.isConnected) timer = setTimeout(reload, POLL_MS);
    } catch (error) {
      summary.replaceChildren(h('span', {}, '스토어 상품'), h('b', {}, '상태를 읽지 못함'));
    }
  };

  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await sendJson('POST', SYNC, {});
      toast('스토어 상품', '동기화를 시작했습니다.');
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('스토어 상품', code === 'STORE_CATALOG_STORE_NOT_CONNECTED' ? '스마트스토어를 먼저 연결하세요.' : String(error?.message ?? error));
    }
    await reload();
  });

  reload();
  return h(
    'div',
    { class: 'store-catalog' },
    summary,
    failure,
    h('div', { class: 'api-action-row' }, button),
    troubleshooting,
  );
}
