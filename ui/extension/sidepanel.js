// The side panel: capture UX only (ADR-0019 §12.1, §12.4), laid out as the approved Collector
// prototype (documents/contracts/ui/UI_SOURCE_OF_TRUTH.md, Extension Collector visual source).
//
// It shows the connection, starts one capture and shows what came back. The axes stay apart and
// are never merged into one label: the transport state of this extension, the run outcome ICBM
// read back, the code of a refusal or failure, and the field truth of a recorded revision. Before
// ICBM has answered, only a transport state is shown. Every outcome, status and value is ICBM's own
// word, written into the page as it came; nothing here saves or exports anything.
//
// On a list page it shows the list queue (ADR-0019 §8.1): the products found on the operator's own
// page, the bounds the operator declares, and each queued product's own state beside its run's own
// outcome. ICBM decides every read; the panel only starts, pauses and cancels.

const TRANSPORT_LABELS = {
  IDLE: "대기",
  CAPTURING: "캡처 중",
  SENT: "전송됨",
  PROCESSING: "처리 중",
  READ_BACK: "ICBM에 전달됨",
  SENT_UNKNOWN: "전송 결과 확인 불가 · ICBM 수집관리에서 확인",
  REFUSED: "수집하지 않음",
  REFUSED_DISCONNECTED: "ICBM 연결 안 됨 · 저장하지 않음",
};

// The field-truth axis (ADR-0019 §12.3), as the approved board writes its chips. This is the only
// place the panel names a field status; a status ICBM adds later is shown as it came.
const FIELD_TRUTH = { CONFIRMED: "CONFIRMED", ABSENT: "ABSENT", REVIEW_REQUIRED: "REVIEW" };

const FIELD_LABELS = {
  original_name: "상품명",
  prices: "가격",
  options: "옵션",
  images: "이미지",
  stock: "재고",
  shipping: "배송",
  minimum_sale_price: "최저판매가",
  quantity_tiers: "수량별 가격",
  brand: "브랜드",
  manufacturer: "제조사",
  origin: "원산지",
  notice: "고시정보",
  detail_description: "상세설명",
};

const IMAGE_ROLES = { REPRESENTATIVE: "대표", DETAIL: "상세", THUMBNAIL: "썸네일", OPTION: "옵션" };

const role = (name) => document.querySelector(`[data-role="${name}"]`);
const action = (name) => document.querySelector(`[data-action="${name}"]`);

// The one capture this panel is waiting for, or null. While it is set the capture controls stay
// disabled whatever the tabs do, and a message from any other port is ignored.
let inFlight = null;
// The answer on screen, or null. It belongs to the page it was captured from: a tab change clears it.
let shown = null;
// What the service worker last said of the connection.
let connection = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function show(name, visible) {
  role(name).hidden = !visible;
}

const withoutScheme = (url) => (typeof url === "string" ? url.replace(/^https?:\/\//, "") : "—");

// A locator as a person reads it: the same URL, its percent-escapes shown as the characters they are.
function readable(url) {
  const bare = withoutScheme(url);
  try {
    return decodeURI(bare);
  } catch {
    return bare;
  }
}

function parsed(json) {
  if (typeof json !== "string") return null;
  try {
    return JSON.parse(json);
  } catch {
    return null;
  }
}

const won = (amount) => (typeof amount === "number" ? amount.toLocaleString("ko-KR") : String(amount));

// How a recorded value reads in a row. It formats ICBM's value; it never derives one.
function displayValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "object") return String(value);
  if (typeof value.text === "string") return value.text;
  if (Array.isArray(value.prices)) {
    return value.prices.map((price) => `${price.label} ${won(price.amount_krw)}`).join(" · ");
  }
  if ("amount_krw" in value) return won(value.amount_krw);
  if (typeof value.policy_text === "string") return value.policy_text;
  if (typeof value.availability === "string") return value.availability;
  if (Array.isArray(value.references)) return `참조 ${value.references.length}개`;
  return JSON.stringify(value);
}

function showTransport(state) {
  role("transport-state").textContent = TRANSPORT_LABELS[state] || state;
  role("transport-state").dataset.state = state;
  role("result").dataset.state = state;
}

function fieldRow(field) {
  const row = element("div", "field");
  row.dataset.status = field.status;
  const line = element("div", "field-line");
  line.append(
    element("div", "field-label", FIELD_LABELS[field.key] || field.key),
    element("div", "field-value", displayValue(parsed(field.value_json))),
  );
  const chip = element("span", "chip", FIELD_TRUTH[field.status] || field.status);
  chip.dataset.status = field.status;
  line.append(chip);
  row.append(line);
  for (const evidence of field.evidence || []) {
    row.append(element("div", "evidence", `${evidence.kind} · ${evidence.locator}`));
  }
  return row;
}

function imageRow(image) {
  const row = element("div", "image");
  row.dataset.status = image.status;
  // A reference the server did not fetch has no locator; ICBM names its host and why it refused it.
  const reference = image.locator
    ? readable(image.locator)
    : [image.host, image.target_refusal || image.issue].filter(Boolean).join(" · ") || "—";
  row.append(
    element("span", "image-role", IMAGE_ROLES[image.role] || image.role),
    element("span", "image-ordinal", String(image.ordinal)),
    element("span", "image-ref", reference),
  );
  row.title = image.locator || reference;
  return row;
}

// The recorded revision ICBM read back: the product it names, its field truth and its image
// references. A run without one shows its outcome and nothing else.
function showRevision(revision) {
  const fields = revision ? revision.fields || [] : [];
  const images = revision ? revision.images || [] : [];
  const name = fields.find((field) => field.key === "original_name");
  const title = name ? displayValue(parsed(name.value_json)) : "—";
  role("product-name").textContent = title;
  show("product-name", Boolean(revision) && title !== "—");
  role("product-id").textContent = revision ? revision.source_product_id : "—";
  const tally = {};
  for (const field of fields) tally[field.status] = (tally[field.status] || 0) + 1;
  let review = 0;
  for (const card of role("summary").querySelectorAll("[data-status]")) {
    const count = tally[card.dataset.status] || 0;
    card.querySelector('[data-role="count"]').textContent = String(count);
    if (card.hasAttribute("data-review")) review = count;
  }
  role("review-count").textContent = String(review);
  show("review-banner", Boolean(revision) && review > 0);
  show("summary", Boolean(revision));
  role("fields").replaceChildren(...fields.map(fieldRow));
  show("fields-section", fields.length > 0);
  role("images").replaceChildren(...images.map(imageRow));
  show("images-section", images.length > 0);
}

function showResult(result) {
  shown = result;
  showTransport(result.state);
  // A run outcome is shown only when ICBM read it back; a refusal has a code and no outcome.
  role("run-outcome").textContent = result.state === "READ_BACK" ? result.outcome : "—";
  role("result").dataset.outcome = result.state === "READ_BACK" ? result.outcome : "";
  role("run-code").textContent =
    result.state === "READ_BACK" ? result.detail || result.revision_code || "—" : result.code || "—";
  show("result-code", role("run-code").textContent !== "—");
  role("run-id").textContent = result.collection_run_id || "—";
  if (result.state === "REFUSED_DISCONNECTED") showIcbm({ icbm: "DISCONNECTED" });
  if (result.state === "READ_BACK") {
    role("supplier-key").textContent = result.supplier_key || role("supplier-key").textContent;
    role("source-url").textContent = readable(result.source_url);
    role("source-url").title = result.source_url || "";
  }
  showRevision(result.state === "READ_BACK" ? result.revision : null);
  render();
}

function clearResult() {
  shown = null;
  showTransport("IDLE");
  role("run-outcome").textContent = "—";
  role("result").dataset.outcome = "";
  role("run-code").textContent = "—";
  show("result-code", false);
  role("run-id").textContent = "—";
  role("source-url").textContent = "—";
  role("source-url").title = "";
  showRevision(null);
}

// Which surface the panel shows, from the connection and the answer on screen.
function render() {
  const status = connection;
  const paired = Boolean(status && status.paired);
  const supplier = Boolean(status && status.supplier_key);
  const answered = Boolean(shown) && shown.state !== "IDLE";
  const disconnected = Boolean(shown) && shown.state === "REFUSED_DISCONNECTED";
  const listing = mode === "list";
  show("pairing-card", Boolean(status) && !paired);
  show("unsupported-card", paired && !supplier && !answered && !listing);
  show("disconnected-card", disconnected);
  show("context", paired && (supplier || answered) && !disconnected && !listing);
  show("context-meta", answered || Boolean(inFlight));
  role("supplier-key").textContent =
    (shown && shown.supplier_key) || (status && status.supplier_key) || "—";
  const capturable = paired && supplier;
  action("capture").disabled = Boolean(inFlight) || !capturable;
  action("recapture").disabled = Boolean(inFlight) || !capturable;
  action("capture").hidden = answered && !inFlight && !disconnected;
  action("recapture").hidden = !answered || disconnected;
  action("open-icbm").hidden = !answered || !paired || disconnected;
  action("check").disabled = !capturable;
  action("discover").disabled = Boolean(inFlight) || !capturable;
  document.querySelector(".bottom").hidden =
    listing || (Boolean(status) && (!paired || (!supplier && !answered)));
  show("queue-footer", listing && paired);
  if (listing && paired) {
    renderList();
  } else {
    for (const name of ["list-context", "queue-bounds", "queue-section"]) show(name, false);
  }
}

// `probe` asks ICBM whether it is reachable. A tab change only re-reads what this browser knows.
async function refresh(probe = false) {
  const status = await chrome.runtime.sendMessage({ type: "status", probe });
  connection = status;
  role("extension-id").textContent = status.extension_id;
  role("pairing-state").textContent = status.paired ? "페어링됨" : "페어링 안 됨";
  role("supplier-state").textContent = status.supplier_key || "지원 페이지 아님";
  role("supplier-state").title = status.supplier_key
    ? `검토된 공급처 상품 페이지 (${status.supplier_key})`
    : "검토된 공급처 상품 페이지가 아님";
  role("supplier-dot").dataset.tone = status.supplier_key ? "ok" : "";
  role("reviewed-hosts").replaceChildren(
    ...(status.reviewed_hosts || []).map((host) => element("li", "", host)),
  );
  if (probe || !(status.paired && status.supplier_key)) showIcbm(status);
  render();
}

// The ICBM half of the strip. The policy the check proved stays on the value, out of the way.
function showIcbm(status) {
  const reachable = status.icbm === "CONNECTED";
  const state = role("icbm-state");
  state.textContent = reachable
    ? "연결됨"
    : status.icbm === "DISCONNECTED"
      ? "연결 안 됨"
      : status.icbm === "UNKNOWN"
        ? "확인 전"
        : `거부됨 · ${status.icbm}`;
  state.dataset.policy = reachable ? status.policy_revision : "";
  state.title = reachable ? `정책 ${status.policy_revision}` : state.textContent;
  state.dataset.tone = reachable || status.icbm === "UNKNOWN" ? "" : "bad";
  role("icbm-dot").dataset.tone = reachable ? "ok" : status.icbm === "UNKNOWN" ? "" : "bad";
}

role("pairing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const field = document.getElementById("pairing-code");
  let answer = null;
  try {
    answer = await chrome.runtime.sendMessage({ type: "pair", code: field.value });
  } catch {
    answer = null;
  } finally {
    // The code is a secret: it is cleared from the field whatever happened to it.
    field.value = "";
  }
  if (!answer || !answer.ok) role("pairing-state").textContent = "페어링 코드가 올바르지 않음";
  else await refresh(true);
});

action("check").addEventListener("click", () => refresh(true));

action("unpair").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "unpair" });
  clearResult();
  await refresh();
});

// ICBM Collection Management owns runs and the DIRECT_URL fallback (ADR-0019 §12.2).
action("open-icbm").addEventListener("click", () =>
  chrome.runtime.sendMessage({
    type: "open-icbm",
    collection_run_id: shown ? shown.collection_run_id || null : null,
  }),
);
action("open-direct").addEventListener("click", () =>
  chrome.runtime.sendMessage({ type: "open-icbm", collection_run_id: null }),
);

function capture() {
  if (inFlight) return;
  clearResult();
  const port = chrome.runtime.connect({ name: "capture" });
  const flight = { port, runId: null };
  inFlight = flight;
  render();
  const finish = () => {
    if (inFlight !== flight) return false;
    inFlight = null;
    refresh();
    return true;
  };
  port.onMessage.addListener((message) => {
    if (inFlight !== flight) return;
    if (message.type === "progress") {
      if (message.collection_run_id) {
        flight.runId = message.collection_run_id;
        role("run-id").textContent = flight.runId;
      }
      showTransport(message.state);
    }
    if (message.type === "result") {
      showResult(message.result);
      finish();
      port.disconnect();
    }
  });
  // The worker ended before it answered. Whatever was accepted is in ICBM; nothing is claimed
  // about it here beyond the run this panel was already told of.
  port.onDisconnect.addListener(() => {
    if (!finish()) return;
    showResult(
      flight.runId
        ? { state: "PROCESSING", collection_run_id: flight.runId, code: "EXTENSION_WORKER_ENDED" }
        : { state: "SENT_UNKNOWN", code: "EXTENSION_WORKER_ENDED" },
    );
  });
  port.postMessage({ type: "capture" });
}

// ---------------------------------------------------------------- the list queue (E3)

// A queue item's own state axis (ADR-0019 §8.1, AC-31), as the approved board writes its chips. It
// is never a run outcome: a captured item shows its run's own outcome, the word ICBM returned.
const ITEM_STATES = {
  DISCOVERED: "발견",
  WAITING: "대기",
  ISSUED: "수집 중",
  SKIPPED: "건너뜀",
  EXPIRED: "만료",
  CANCELLED: "취소됨",
};
// A captured item whose run ICBM has not settled yet.
const RUN_PENDING = "PENDING";
const QUEUE_LABELS = {
  RUNNING: "진행 중",
  WAITING: "ICBM이 정한 간격을 기다리는 중",
  PAUSED: "일시정지됨 · 새 상품을 요청하지 않습니다",
  FINISHED: "대기열을 마쳤습니다",
  STOPPED: "진행을 멈췄습니다 · 재개할 수 있습니다",
  CANCELLED: "대기열을 취소했습니다",
  OPEN: "열려 있는 대기열이 있습니다 · 재개하거나 취소합니다",
};

// Which surface the panel shows: the product capture, or a list page's queue.
let mode = "product";
// The products found on the operator's loaded list page, or null. Product URLs only.
let discovery = null;
// Which of them the operator chose to queue (A-UX2 F-9). A choice narrows the list ICBM is asked
// to queue; it is no authorization and never the queue's bound, which the operator types.
let selected = new Set();
// The worker port of the list mode, open while it is shown.
let queuePort = null;
// The queue as ICBM last returned it, or null before one is declared.
let queue = null;
// This panel's own control of the worker: IDLE, RUNNING, PAUSED or ENDED.
let control = "IDLE";
// The last word of the queue's progress, and the code it stopped with, if any.
let queueLine = null;
let queueCode = null;

function queueRow(item) {
  const row = element("div", "queue-row");
  const line = element("div", "queue-line");
  const name = element("span", "queue-name", readable(item.source_url));
  name.title = item.source_url;
  const settled = item.state === "CAPTURED" && item.run_outcome && item.run_outcome !== RUN_PENDING;
  const chip = element(
    "span",
    "chip",
    item.state === "CAPTURED"
      ? settled
        ? item.run_outcome
        : TRANSPORT_LABELS.PROCESSING
      : ITEM_STATES[item.state] || item.state,
  );
  chip.dataset.state = item.state === "CAPTURED" ? (settled ? "SETTLED" : "PROCESSING") : item.state;
  if (settled) chip.dataset.outcome = item.run_outcome;
  if (item.state === "DISCOVERED") {
    const box = element("input");
    box.type = "checkbox";
    box.dataset.select = item.source_url;
    box.checked = selected.has(item.source_url);
    box.setAttribute("aria-label", `${readable(item.source_url)} 선택`);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(item.source_url);
      else selected.delete(item.source_url);
      render();
    });
    const pick = element("label", "row-select");
    pick.append(box, name);
    line.append(pick, chip);
  } else {
    line.append(name, chip);
  }
  const note = element("div", "queue-note");
  note.append(element("span", "mono", item.product_key || "—"), ` · ${itemNote(item)}`);
  row.append(line, note);
  // A captured item names its run: open that run in ICBM Collection Management, which owns runs
  // and their history (ADR-0019 §12.2). Nothing is sent to the supplier and nothing is read again.
  if (item.collection_run_id) {
    const open = element("button", "row-open", "수집관리에서 결과 보기");
    open.type = "button";
    open.dataset.action = "open-run";
    open.dataset.run = item.collection_run_id;
    open.addEventListener("click", () =>
      chrome.runtime.sendMessage({ type: "open-icbm", collection_run_id: item.collection_run_id }),
    );
    row.append(open);
  }
  return row;
}

function itemNote(item) {
  if (item.state === "DISCOVERED") return "선언 전";
  if (item.state === "WAITING") return `대기열 ${item.position}번`;
  if (item.state === "SKIPPED") return "이미 수집한 상품";
  if (item.state === "ISSUED") return "상세 페이지 캡처 중";
  if (item.state === "EXPIRED") return "읽지 못했습니다 · 다시 읽지 않습니다";
  if (item.state === "CANCELLED") return "취소됨";
  return item.run_detail || `run ${item.collection_run_id || "—"}`;
}

function intervalChoices(minimum) {
  const select = document.getElementById("queue-interval");
  const choices = [1, 1.5, 2, 3, 6].map((factor) => Math.round(minimum * factor));
  const blank = element("option", "", "선택");
  blank.value = "";
  select.replaceChildren(
    blank,
    ...[...new Set(choices)].map((seconds) => {
      const option = element("option", "", `${seconds}초`);
      option.value = String(seconds);
      return option;
    }),
  );
}

function renderList() {
  const items = queue
    ? queue.items
    : (discovery ? discovery.links : []).map((url, index) => ({
        source_url: url,
        state: "DISCOVERED",
        position: index + 1,
        product_key: null,
      }));
  show("list-context", true);
  role("list-supplier-key").textContent = discovery ? discovery.supplier_key : "—";
  role("list-title").textContent = (discovery && discovery.title) || "—";
  role("list-found").textContent = String(discovery ? discovery.found : 0);
  role("list-selected").textContent = String(discovery && !queue ? chosenLinks().length : 0);
  show("selection-actions", Boolean(discovery) && !queue && discovery.links.length > 0);
  show("queue-bounds", Boolean(discovery) && !queue);
  show("queue-section", items.length > 0 || Boolean(queueLine));
  role("queue-rows").replaceChildren(...items.map(queueRow));
  const done = items.filter(
    (item) => item.state === "CAPTURED" && item.run_outcome && item.run_outcome !== RUN_PENDING,
  ).length;
  const running = items.filter(
    (item) => item.state === "ISSUED" || (item.state === "CAPTURED" && !(item.run_outcome && item.run_outcome !== RUN_PENDING)),
  ).length;
  const total = queue ? items.length : 0;
  role("queue-count").textContent = queue ? `${done} / ${total}` : `${items.length}개 발견`;
  role("queue-done").style.width = total ? `${(100 * done) / total}%` : "0";
  role("queue-running").style.width = total ? `${(100 * running) / total}%` : "0";
  const status = [queueLine, queueCode ? `코드 ${queueCode}` : null].filter(Boolean).join(" · ");
  role("queue-status").textContent = status;
  show("queue-status", Boolean(status));
  action("queue-start").disabled = control !== "IDLE" || !discovery || chosenLinks().length === 0;
  action("queue-start").hidden = control === "PAUSED";
  action("queue-pause").disabled = control !== "RUNNING" && control !== "PAUSED";
  action("queue-pause").textContent = control === "PAUSED" ? "재개" : "일시정지";
  action("queue-cancel").hidden = !queue || queue.state !== "OPEN" || control === "RUNNING";
  action("product-mode").disabled = control === "RUNNING" || control === "PAUSED";
  // Recovery (A-UX3 F-10) is explained once this queue has ended and ICBM holds no open one: the
  // operator goes back to the list page and finds it again, and ICBM skips what it recorded. The
  // ended queue is never reissued, and nothing here navigates the supplier's pages.
  show("queue-recovery", control === "ENDED" && Boolean(queue) && queue.state !== "OPEN");
}

// The chosen links in the page's own order.
function chosenLinks() {
  return discovery ? discovery.links.filter((link) => selected.has(link)) : [];
}

function listMessage(message) {
  if (message.type === "discovered") {
    const found = message.discovery;
    if (!found || !found.ok) {
      discovery = null;
      queueLine = "상품 링크를 찾지 못했습니다";
      queueCode = found ? found.code : "DISCOVERY_UNAVAILABLE";
    } else {
      discovery = found;
      // Every found product starts chosen; the operator removes what should not be read.
      selected = new Set(found.links);
      queueLine = null;
      queueCode = null;
      const max = document.getElementById("queue-max");
      max.max = String(found.max_queue_products);
      max.placeholder = `최대 ${found.max_queue_products}`;
      max.value = "";
      intervalChoices(found.min_queue_interval_s);
      // A queue that is still open in ICBM — this panel was closed, or the worker stopped — is
      // shown as ICBM holds it, to resume or cancel. A new one is never declared beside it.
      if (found.open_queue) {
        queue = found.open_queue;
        control = "PAUSED";
        queueLine = QUEUE_LABELS.OPEN;
      }
    }
  } else if (message.type === "queue") {
    queue = message.queue;
    if (queue.state !== "OPEN" && control === "PAUSED") {
      control = "ENDED";
      queueLine = QUEUE_LABELS[queue.state] || queueLine;
    }
  } else if (message.type === "waiting") {
    queueLine = `${QUEUE_LABELS.WAITING} · ${message.seconds}초`;
  } else if (message.type === "issued" || message.type === "progress") {
    queueLine = QUEUE_LABELS.RUNNING;
  } else if (message.type === "result") {
    queueLine = QUEUE_LABELS.RUNNING;
    if (message.result.state !== "READ_BACK") queueCode = message.result.code || message.result.state;
  } else if (message.type === "paused") {
    control = "PAUSED";
    queueLine = QUEUE_LABELS.PAUSED;
  } else if (message.type === "finished") {
    control = "ENDED";
    queueLine = queue && queue.state === "CANCELLED" ? QUEUE_LABELS.CANCELLED : QUEUE_LABELS.FINISHED;
  } else if (message.type === "stopped") {
    control = queue ? "ENDED" : "IDLE";
    queueLine = QUEUE_LABELS.STOPPED;
    queueCode = message.code || queueCode;
  }
  render();
}

function enterList() {
  if (inFlight || mode === "list") return;
  clearResult();
  mode = "list";
  discovery = null;
  queue = null;
  control = "IDLE";
  queueLine = "상품 링크를 찾는 중";
  queueCode = null;
  queuePort = chrome.runtime.connect({ name: "queue" });
  const port = queuePort;
  port.onMessage.addListener((message) => {
    if (queuePort === port) listMessage(message);
  });
  port.onDisconnect.addListener(() => {
    if (queuePort !== port) return;
    queuePort = null;
    if (control === "RUNNING") {
      control = "ENDED";
      queueLine = QUEUE_LABELS.STOPPED;
      queueCode = "EXTENSION_WORKER_ENDED";
    }
    render();
  });
  port.postMessage({ type: "discover" });
  render();
}

function leaveList() {
  if (control === "RUNNING" || control === "PAUSED") return;
  if (queuePort) {
    const port = queuePort;
    queuePort = null;
    port.disconnect();
  }
  mode = "product";
  discovery = null;
  queue = null;
  control = "IDLE";
  queueLine = null;
  queueCode = null;
  refresh();
}

action("discover").addEventListener("click", enterList);
action("product-mode").addEventListener("click", leaveList);
action("select-all").addEventListener("click", () => {
  if (!discovery || queue) return;
  selected = new Set(discovery.links);
  render();
});
action("select-none").addEventListener("click", () => {
  if (!discovery || queue) return;
  selected = new Set();
  render();
});

// The operator's own bounds go to ICBM as they were entered: an empty one is sent as missing, and
// ICBM refuses it. Nothing here fills one in.
action("queue-start").addEventListener("click", () => {
  if (!queuePort || !discovery || control !== "IDLE") return;
  const max = document.getElementById("queue-max").value;
  const interval = document.getElementById("queue-interval").value;
  control = "RUNNING";
  queueLine = QUEUE_LABELS.RUNNING;
  queueCode = null;
  queuePort.postMessage({
    type: "start",
    tab_id: discovery.tab_id,
    declaration: {
      supplier_key: discovery.supplier_key,
      // Only the chosen links. ICBM checks each one again and applies its own bounds.
      links: chosenLinks(),
      max_products: max === "" ? null : Number(max),
      interval_s: interval === "" ? null : Number(interval),
      skip_collected: document.getElementById("queue-skip").checked,
    },
  });
  render();
});

action("queue-pause").addEventListener("click", () => {
  if (!queuePort) return;
  if (control === "RUNNING") {
    queuePort.postMessage({ type: "pause" });
  } else if (control === "PAUSED" && queue) {
    control = "RUNNING";
    queueLine = QUEUE_LABELS.RUNNING;
    queuePort.postMessage({ type: "resume", queue_id: queue.queue_id });
  }
  render();
});

action("queue-cancel").addEventListener("click", () => {
  if (!queuePort || !queue) return;
  queuePort.postMessage({ type: "cancel", queue_id: queue.queue_id });
});

action("capture").addEventListener("click", capture);
action("recapture").addEventListener("click", capture);

// An answer belongs to the page it was captured from: another tab or a reload clears it. A list
// belongs to its page too, unless its queue is running: then the worker itself moves the tab.
function pageChanged() {
  if (mode === "list") {
    if (control === "IDLE" || control === "ENDED") leaveList();
    else refresh();
    return;
  }
  if (!inFlight) clearResult();
  refresh();
}

chrome.tabs.onActivated.addListener(pageChanged);
chrome.tabs.onUpdated.addListener((_tabId, change, tab) => {
  if (change.status === "complete" && tab.active) pageChanged();
});
refresh(true);
