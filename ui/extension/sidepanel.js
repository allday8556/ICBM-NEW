// The side panel: capture UX only (ADR-0019 §12.1, §12.4), laid out as the approved Collector
// prototype (documents/contracts/ui/UI_SOURCE_OF_TRUTH.md, Extension Collector visual source).
//
// It shows the connection, starts one capture and shows what came back. The axes stay apart and
// are never merged into one label: the transport state of this extension, the run outcome ICBM
// read back, the code of a refusal or failure, and the field truth of a recorded revision. Before
// ICBM has answered, only a transport state is shown. Every outcome, status and value is ICBM's own
// word, written into the page as it came; nothing here saves or exports anything.

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
  row.append(
    element("span", "image-role", IMAGE_ROLES[image.role] || image.role),
    element("span", "image-ordinal", String(image.ordinal)),
    element("span", "image-ref", withoutScheme(image.locator)),
  );
  row.title = image.locator || "";
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
    role("source-url").textContent = withoutScheme(result.source_url);
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
  show("pairing-card", Boolean(status) && !paired);
  show("unsupported-card", paired && !supplier && !answered);
  show("disconnected-card", disconnected);
  show("context", paired && (supplier || answered) && !disconnected);
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
  document.querySelector(".bottom").hidden = Boolean(status) && (!paired || (!supplier && !answered));
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

action("capture").addEventListener("click", capture);
action("recapture").addEventListener("click", capture);

// An answer belongs to the page it was captured from: another tab or a reload clears it.
function pageChanged() {
  if (!inFlight) clearResult();
  refresh();
}

chrome.tabs.onActivated.addListener(pageChanged);
chrome.tabs.onUpdated.addListener((_tabId, change, tab) => {
  if (change.status === "complete" && tab.active) pageChanged();
});
refresh(true);
