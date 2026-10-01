// The side panel: capture UX only (ADR-0019 §12.1, §12.4).
//
// It shows the connection, starts one capture and shows what came back. Three things are kept
// apart and never merged into one label: the transport state of this extension, the run outcome
// ICBM read back, and the code of a refusal or failure. Before ICBM has answered, only a
// transport state is shown. Nothing here saves or exports anything.

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

const role = (name) => document.querySelector(`[data-role="${name}"]`);
const action = (name) => document.querySelector(`[data-action="${name}"]`);

// The one capture this panel is waiting for, or null. While it is set the capture button stays
// disabled whatever the tabs do, and a message from any other port is ignored.
let inFlight = null;

function showTransport(state) {
  role("transport-state").textContent = TRANSPORT_LABELS[state] || state;
  role("transport-state").dataset.state = state;
}

function showResult(result) {
  showTransport(result.state);
  // A run outcome is shown only when ICBM read it back; a refusal has a code and no outcome.
  role("run-outcome").textContent = result.state === "READ_BACK" ? result.outcome : "—";
  role("run-code").textContent =
    result.state === "READ_BACK" ? result.detail || "—" : result.code || "—";
  role("run-id").textContent = result.collection_run_id || "—";
}

// `probe` asks ICBM whether it is reachable. A tab change only re-reads what this browser knows.
async function refresh(probe = false) {
  const status = await chrome.runtime.sendMessage({ type: "status", probe });
  role("extension-id").textContent = status.extension_id;
  role("pairing-state").textContent = status.paired ? "페어링됨" : "페어링 안 됨";
  role("supplier-state").textContent = status.supplier_key
    ? `검토된 공급처 (${status.supplier_key})`
    : "검토된 공급처 상품 페이지가 아님";
  if (probe || !(status.paired && status.supplier_key)) {
    role("icbm-state").textContent =
      status.icbm === "CONNECTED"
        ? `연결됨 · 정책 ${status.policy_revision}`
        : status.icbm === "DISCONNECTED"
          ? "연결 안 됨"
          : status.icbm === "UNKNOWN"
            ? "확인 전"
            : `거부됨 · ${status.icbm}`;
  }
  action("capture").disabled = Boolean(inFlight) || !(status.paired && status.supplier_key);
  action("check").disabled = !(status.paired && status.supplier_key);
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
  await refresh();
});

action("capture").addEventListener("click", () => {
  if (inFlight) return;
  const button = action("capture");
  button.disabled = true;
  showResult({ state: "IDLE" });
  const port = chrome.runtime.connect({ name: "capture" });
  const flight = { port, runId: null };
  inFlight = flight;
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
});

chrome.tabs.onActivated.addListener(() => refresh());
chrome.tabs.onUpdated.addListener((_tabId, change) => {
  if (change.status === "complete") refresh();
});
refresh(true);
