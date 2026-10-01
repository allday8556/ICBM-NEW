// The registration status card (ADR-0014 §28.5): the lower-right summary of the server's one
// read-state partition, beside the transient toasts. It renders the server's labels and counts as
// given and never derives a state from an Intent's own fields. 재확인필요 stays on the card for as
// long as the server counts it; only 등록실패 is red.

import { getJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { navigate } from '../core/router.js';

const STATUS = '/api/v1/register/status';
// While anything is 등록중 the card re-reads the server; otherwise it refreshes on navigation.
const POLL_MS = 15000;
const ORDER = [
  ['RECHECK_REQUIRED', 'recheck_required'],
  ['REGISTERING', 'registering'],
  ['REGISTERED', 'registered'],
  ['FAILED', 'failed'],
];

// Tone only, by the server's state: the words are the server's own labels.
export const READ_STATE_TONE = {
  REGISTERING: 'info',
  REGISTERED: 'good',
  RECHECK_REQUIRED: 'warn',
  FAILED: 'bad',
};

let timer = null;
let sequence = 0;

function card() {
  return document.getElementById('registrationStatus');
}

function hide(root) {
  root.hidden = true;
  root.replaceChildren();
  document.body.classList.remove('has-registration-status');
}

function render(root, status) {
  const counts = status.counts;
  if (!counts.total) {
    hide(root);
    return;
  }
  const chips = ORDER.filter(([, key]) => counts[key] > 0).map(([state, key]) =>
    h(
      'span',
      { class: `chip ${READ_STATE_TONE[state]}`, 'data-read-state': state },
      `${status.labels[state] ?? state} ${counts[key]}`,
    ),
  );
  if (counts.unclassified > 0) {
    chips.push(h('span', { class: 'chip bad', 'data-reason': 'REGISTER_READ_STATE_UNCLASSIFIED' }, `분류 오류 ${counts.unclassified}`));
  }
  root.replaceChildren(
    h(
      'div',
      { class: 'registration-status-head' },
      h('b', {}, '등록 진행'),
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'open-registration-status',
          onclick: () => navigate('register', { status: 'open' }),
        },
        '자세히',
      ),
    ),
    h('div', { class: 'registration-status-counts' }, ...chips),
  );
  root.hidden = false;
  document.body.classList.add('has-registration-status');
}

// Re-read the server's partition and redraw the card. A failed read leaves the last state shown
// rather than inventing one.
export async function refreshRegistrationStatus() {
  const root = card();
  if (!root) return;
  const mine = ++sequence;
  let status;
  try {
    status = await getJson(STATUS);
  } catch {
    return;
  }
  if (mine !== sequence) return;
  render(root, status);
  window.clearTimeout(timer);
  timer = status.counts.registering > 0 ? window.setTimeout(refreshRegistrationStatus, POLL_MS) : null;
}
