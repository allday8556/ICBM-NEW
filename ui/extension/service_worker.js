// The capture extension's service worker (ADR-0019 E1): transport and capture only.
//
// It owns no workflow and no truth. One click captures the one product page in the active tab,
// sends it to the paired ICBM over the loopback, and reads the canonical run back. It writes no
// database, no ProductFactsRevision and no file, and it keeps no page material: a capture exists
// in memory between the cut and the send, and nowhere afterwards.

import { captureInPage } from "./lib/capture.js";
import { IcbmRefused, fetchPolicy, readRun, sendCapture } from "./lib/client.js";
import { parsePairingCode } from "./lib/signing.js";

// The reviewed supplier hosts (ADR-0019 §3). This names which supplier a host belongs to and
// nothing else: the capture topology is the server's policy, fetched for every capture.
const SUPPLIERS = { "kmretail.co.kr": "kmretail" };
const PAIRING_KEY = "pairing";
const RUN_POLL_MS = 1000;
const RUN_POLL_LIMIT = 60;

// The capture UX is the side panel, which exists from Chrome 114: the manifest's minimum. There is
// no other surface, and the pairing is the only thing this extension stores.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

async function pairing() {
  const stored = await chrome.storage.local.get(PAIRING_KEY);
  return stored[PAIRING_KEY] || null;
}

async function targetTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

async function activeSupplierTab() {
  const tab = await targetTab();
  if (!tab || !tab.url) return null;
  let url;
  try {
    url = new URL(tab.url);
  } catch {
    return null;
  }
  const supplierKey = url.protocol === "https:" ? SUPPLIERS[url.hostname] : undefined;
  return supplierKey ? { tabId: tab.id, supplierKey } : null;
}

// One capture, from the click to the canonical run's own outcome. `progress` is told each
// transport state as it is reached; an outcome is only ever the one ICBM read back.
async function capture(progress) {
  const paired = await pairing();
  if (!paired) return { state: "REFUSED", code: "EXTENSION_NOT_PAIRED" };
  const target = await activeSupplierTab();
  if (!target) return { state: "REFUSED", code: "NOT_A_REVIEWED_SUPPLIER_PAGE" };
  try {
    // The policy first: a page is never read or cut without the reviewed policy in hand.
    const { policy, revision, digest } = await fetchPolicy(
      paired,
      chrome.runtime.id,
      target.supplierKey,
    );
    progress("CAPTURING");
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: target.tabId },
      func: captureInPage,
      args: [policy],
    });
    const cut = injection && injection.result;
    if (!cut || !cut.ok) {
      // Refused before anything was sent: nothing left the tab, and no run exists.
      return { state: "REFUSED", code: (cut && cut.code) || "CAPTURE_UNAVAILABLE" };
    }
    const accepted = await sendCapture(paired, chrome.runtime.id, {
      supplierKey: target.supplierKey,
      revision,
      digest,
      capture: cut,
    });
    progress("SENT");
    progress("PROCESSING");
    for (let attempt = 0; attempt < RUN_POLL_LIMIT; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, RUN_POLL_MS));
      const run = await readRun(paired, accepted.collection_run_id);
      if (run.outcome !== "PENDING") {
        return {
          state: "READ_BACK",
          collection_run_id: run.collection_run_id,
          outcome: run.outcome,
          detail: run.detail,
        };
      }
    }
    return { state: "PROCESSING", collection_run_id: accepted.collection_run_id };
  } catch (error) {
    const code = error instanceof IcbmRefused ? error.code : "CAPTURE_UNAVAILABLE";
    return { state: code === "ICBM_DISCONNECTED" ? "REFUSED_DISCONNECTED" : "REFUSED", code };
  }
}

// What the side panel shows of the connection. Only a `probe` asks ICBM: every signed request
// uses one nonce of the server's bounded replay cache, so a tab change never sends one.
async function status(probe) {
  const paired = await pairing();
  const target = await activeSupplierTab();
  const answer = {
    extension_id: chrome.runtime.id,
    paired: Boolean(paired),
    supplier_key: target ? target.supplierKey : null,
    icbm: "UNKNOWN",
    policy_revision: null,
  };
  if (!probe || !paired || !target) return answer;
  try {
    const fetched = await fetchPolicy(paired, chrome.runtime.id, target.supplierKey);
    return { ...answer, icbm: "CONNECTED", policy_revision: fetched.revision };
  } catch (error) {
    const code = error instanceof IcbmRefused ? error.code : "ICBM_DISCONNECTED";
    return { ...answer, icbm: code === "ICBM_DISCONNECTED" ? "DISCONNECTED" : code };
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "capture") return;
  port.onMessage.addListener(async (message) => {
    if (message.type !== "capture") return;
    const result = await capture((state) => port.postMessage({ type: "progress", state }));
    port.postMessage({ type: "result", result });
  });
});

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message.type === "status") {
    status(message.probe === true).then(respond);
    return true;
  }
  if (message.type === "pair") {
    const parsed = parsePairingCode(message.code || "");
    if (!parsed) {
      respond({ ok: false, code: "PAIRING_CODE_INVALID" });
      return false;
    }
    chrome.storage.local.set({ [PAIRING_KEY]: parsed }).then(() => respond({ ok: true }));
    return true;
  }
  if (message.type === "unpair") {
    chrome.storage.local.remove(PAIRING_KEY).then(() => respond({ ok: true }));
    return true;
  }
  return false;
});
