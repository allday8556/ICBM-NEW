// The capture extension's service worker (ADR-0019 E1): transport and capture only.
//
// It owns no workflow and no truth. One click captures the one product page in the active tab,
// sends it to the paired ICBM over the loopback, and reads the canonical run back. It writes no
// database, no ProductFactsRevision and no file, and it keeps no page material: a capture exists
// in memory between the cut and the send, and nowhere afterwards.
//
// A list queue (ADR-0019 §8.1, E3) is the same capture, read by read: the operator's loaded list
// page is searched for product URLs, the operator declares the queue's bounds, and ICBM decides
// every read. This worker asks ICBM for the next read, waits when told to wait, navigates the
// operator's own tab to the one URL ICBM issued, captures it with the unchanged cut and sends it
// with its ticket. Its own clock never decides that a read may happen.

import { captureInPage } from "./lib/capture.js";
import {
  IcbmRefused,
  cancelQueue,
  declareQueue,
  fetchPolicy,
  fetchQueuePolicy,
  nextRead,
  readQueue,
  readRevision,
  readRun,
  sendCapture,
} from "./lib/client.js";
import { discoverInPage } from "./lib/discover.js";
import { parsePairingCode } from "./lib/signing.js";

// The reviewed supplier hosts (ADR-0019 §3). This names which supplier a host belongs to and
// nothing else: the capture topology is the server's policy, fetched for every capture.
const SUPPLIERS = { "kmretail.co.kr": "kmretail" };
const PAIRING_KEY = "pairing";
const RUN_POLL_MS = 1000;
const RUN_POLL_LIMIT = 60;
// The one run state this extension knows by name: not settled yet.
const PENDING = "PENDING";

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
//
// Three things are kept apart, and what is not known is never shown as known:
//   REFUSED / REFUSED_DISCONNECTED  nothing was accepted: ICBM answered a refusal, or nothing left.
//   SENT_UNKNOWN                    the capture may have reached ICBM and no answer was read.
//   PROCESSING / READ_BACK          ICBM accepted it; the run exists, settled or not.
async function capture(progress) {
  const paired = await pairing();
  if (!paired) return { state: "REFUSED", code: "EXTENSION_NOT_PAIRED" };
  const target = await activeSupplierTab();
  if (!target) return { state: "REFUSED", code: "NOT_A_REVIEWED_SUPPLIER_PAGE" };
  return captureTab(paired, target, null, progress);
}

// The one capture path, for a single click and for a queue read alike: the policy, the cut, one
// send (with the queue read's ticket, when there is one) and the canonical run's read-back.
async function captureTab(paired, target, ticket, progress) {
  let accepted;
  let sending = false;
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
    sending = true;
    accepted = await sendCapture(paired, chrome.runtime.id, {
      supplierKey: target.supplierKey,
      revision,
      digest,
      capture: cut,
      ticket,
    });
  } catch (error) {
    const code = error instanceof IcbmRefused ? error.code : "CAPTURE_UNAVAILABLE";
    // A request that failed on the wire, or whose acceptance could not be read, may have been
    // accepted: that is unknown, never "not saved".
    if (sending && (code === "ICBM_DISCONNECTED" || code === "ICBM_ACCEPTED_UNREADABLE")) {
      return { state: "SENT_UNKNOWN", code };
    }
    return { state: code === "ICBM_DISCONNECTED" ? "REFUSED_DISCONNECTED" : "REFUSED", code };
  }
  const runId = accepted && accepted.collection_run_id;
  if (typeof runId !== "string" || !runId) {
    return { state: "SENT_UNKNOWN", code: "ICBM_ACCEPTED_UNREADABLE" };
  }
  // From here the run exists. A failed read-back never turns it into a refusal.
  progress("SENT", runId);
  let code = null;
  for (let attempt = 0; attempt < RUN_POLL_LIMIT; attempt += 1) {
    // One message per poll: the panel learns the run, and the worker stays alive.
    progress("PROCESSING", runId);
    await new Promise((resolve) => setTimeout(resolve, RUN_POLL_MS));
    let run;
    try {
      run = await readRun(paired, runId);
    } catch (error) {
      code = error instanceof IcbmRefused ? error.code : "RUN_READ_BACK_UNAVAILABLE";
      continue;
    }
    if (!run || run.collection_run_id !== runId) {
      code = "RUN_READ_BACK_UNREADABLE";
      continue;
    }
    // An outcome is whatever word ICBM returned for this very run, and only a word. This extension
    // names no outcome itself, so it can neither invent one nor restate one.
    const settled = typeof run.outcome === "string" && run.outcome !== "" && run.outcome !== PENDING;
    if (settled) {
      return {
        state: "READ_BACK",
        collection_run_id: runId,
        outcome: run.outcome,
        detail: typeof run.detail === "string" ? run.detail : null,
        supplier_key: run.supplier_key,
        source_url: run.source_url,
        ...(await preview(paired, run.revision_id)),
      };
    }
    code = run.outcome === PENDING ? null : "RUN_READ_BACK_UNREADABLE";
  }
  return { state: "PROCESSING", collection_run_id: runId, code };
}

// The field and evidence preview of a recorded run (ADR-0019 §12.1): the canonical revision as
// COLLECT holds it, handed to the panel and kept nowhere. A run that names no revision has none,
// and a revision that cannot be read leaves the run's own outcome standing.
async function preview(paired, revisionId) {
  if (typeof revisionId !== "string" || !revisionId) return { revision: null };
  try {
    return { revision: await readRevision(paired, revisionId) };
  } catch (error) {
    const code = error instanceof IcbmRefused ? error.code : "REVISION_READ_BACK_UNAVAILABLE";
    return { revision: null, revision_code: code };
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
    reviewed_hosts: Object.keys(SUPPLIERS),
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

// ICBM Collection Management owns canonical run management and the DIRECT_URL fallback
// (ADR-0019 §12.2): the panel only opens it, at the paired ICBM, on a run when it has one.
async function openCollectionManagement(runId) {
  const paired = await pairing();
  if (!paired) return { ok: false, code: "EXTENSION_NOT_PAIRED" };
  const query = new URLSearchParams({ view: "jobs" });
  if (typeof runId === "string" && runId) query.set("run", runId);
  await chrome.tabs.create({ url: `${paired.origin}/#/collect?${query}` });
  return { ok: true };
}

// ---------------------------------------------------------------- the list queue (E3)

// How often a wait the server ordered is counted down to the panel. The server decides how long;
// this only keeps the panel and the worker awake while it passes.
const WAIT_TICK_MS = 1000;
// How long a navigation to an issued URL may take, at most. An issued read that is not captured
// expires at ICBM, still counted, and stops its queue there.
const LOAD_LIMIT_MS = 60_000;
// How much of an issued read's time is kept back for the capture and its send after the load.
const SEND_MARGIN_MS = 15_000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const codeOf = (error, fallback) => (error instanceof IcbmRefused ? error.code : fallback);

// Find the products of the loaded list page in the active tab. ICBM is asked only for the
// supplier's reviewed product path form and its declared queue limits; the page is read where it
// is, and only its product URLs come back from it.
async function discover() {
  const paired = await pairing();
  if (!paired) return { ok: false, code: "EXTENSION_NOT_PAIRED" };
  const target = await activeSupplierTab();
  if (!target) return { ok: false, code: "NOT_A_REVIEWED_SUPPLIER_PAGE" };
  let policy;
  try {
    policy = await fetchQueuePolicy(paired, chrome.runtime.id, target.supplierKey);
  } catch (error) {
    return { ok: false, code: codeOf(error, "QUEUE_POLICY_UNAVAILABLE") };
  }
  let found;
  try {
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: target.tabId },
      func: discoverInPage,
      args: [
        {
          host: policy.storefront_host,
          productPath: policy.product_path,
          maxLinks: policy.max_discovered_links,
        },
      ],
    });
    found = injection && injection.result;
  } catch {
    found = null;
  }
  if (!found || !found.ok) {
    return { ok: false, code: (found && found.code) || "DISCOVERY_UNAVAILABLE" };
  }
  const tab = await chrome.tabs.get(target.tabId);
  return {
    ok: true,
    supplier_key: target.supplierKey,
    tab_id: target.tabId,
    // Shown in the panel only; it is never sent.
    title: tab.title || "",
    links: found.links,
    found: found.found,
    max_queue_products: policy.max_queue_products,
    min_queue_interval_s: policy.min_queue_interval_s,
  };
}

// Navigate the operator's own tab to the one URL ICBM issued, and wait until it has loaded there.
function load(tabId, url, limitMs) {
  return new Promise((resolve) => {
    let timer = null;
    const done = (ok) => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listen);
      resolve(ok);
    };
    const listen = (id, change, tab) => {
      if (id === tabId && change.status === "complete") done(tab.url === url);
    };
    timer = setTimeout(() => done(false), limitMs);
    chrome.tabs.onUpdated.addListener(listen);
    chrome.tabs.update(tabId, { url }).catch(() => done(false));
  });
}

// What the operator asked for one queue: pause (ask for nothing new), resume, or cancel.
function queueControl(tabId, declaration) {
  let wake = null;
  const control = {
    tabId,
    declaration,
    queueId: null,
    paused: false,
    cancelled: false,
    pause() {
      control.paused = true;
    },
    resume() {
      control.paused = false;
      if (wake) wake();
    },
    stop() {
      control.cancelled = true;
      if (wake) wake();
    },
    resumed() {
      return new Promise((resolve) => {
        wake = () => {
          wake = null;
          resolve();
        };
      });
    },
  };
  return control;
}

// One queue, read by read, exactly as ICBM issues them.
async function runQueue(control, tell) {
  const paired = await pairing();
  if (!paired) return tell({ type: "stopped", code: "EXTENSION_NOT_PAIRED" });
  let declared;
  try {
    declared = await declareQueue(paired, chrome.runtime.id, control.declaration);
  } catch (error) {
    return tell({ type: "stopped", code: codeOf(error, "QUEUE_DECLARATION_UNAVAILABLE") });
  }
  control.queueId = declared.queue.queue_id;
  tell({ type: "queue", queue: declared.queue, count: declared.count });
  const target = { tabId: control.tabId, supplierKey: control.declaration.supplier_key };
  let code = null;
  while (!control.cancelled) {
    if (control.paused) {
      tell({ type: "paused" });
      await control.resumed();
      continue;
    }
    let answer;
    try {
      answer = await nextRead(paired, chrome.runtime.id, control.queueId);
    } catch (error) {
      code = codeOf(error, "QUEUE_NEXT_UNAVAILABLE");
      break;
    }
    if (answer.kind === "DONE") break;
    if (answer.kind === "WAIT") {
      // ICBM said how long. This worker only waits it out, and then asks again.
      let remaining = Math.max(1, Math.ceil(answer.wait_s || 0));
      while (remaining > 0 && !control.paused && !control.cancelled) {
        tell({ type: "waiting", seconds: remaining });
        await sleep(WAIT_TICK_MS);
        remaining -= 1;
      }
      continue;
    }
    // One issued read: this URL, under this ticket, once.
    const item = answer.item;
    tell({ type: "issued", item });
    const limit = Math.min(LOAD_LIMIT_MS, Math.max(0, answer.expires_in_s * 1000 - SEND_MARGIN_MS));
    if (!(await load(target.tabId, item.source_url, limit))) {
      // Nothing was captured: the issued read expires at ICBM, still counted, and stops the queue.
      code = "QUEUE_PAGE_NOT_LOADED";
      break;
    }
    const result = await captureTab(paired, target, answer.ticket, (state, runId) =>
      tell({ type: "progress", item_id: item.item_id, state, collection_run_id: runId || null }),
    );
    tell({ type: "result", item_id: item.item_id, result });
    await showQueue(paired, control.queueId, tell);
    // A refused or unknown capture ends this worker's part: ICBM stops the queue for it, and the
    // operator decides what follows.
    if (result.state !== "READ_BACK") {
      code = result.code || result.state;
      break;
    }
  }
  await showQueue(paired, control.queueId, tell);
  tell({ type: code ? "stopped" : "finished", code });
}

async function showQueue(paired, queueId, tell) {
  try {
    tell({ type: "queue", queue: await readQueue(paired, chrome.runtime.id, queueId) });
  } catch {
    // The panel keeps what it last showed; ICBM holds the queue either way.
  }
}

async function cancel(queueId, tell) {
  const paired = await pairing();
  if (!paired || typeof queueId !== "string" || !queueId) return;
  try {
    tell({ type: "queue", queue: await cancelQueue(paired, chrome.runtime.id, queueId) });
  } catch (error) {
    tell({ type: "stopped", code: codeOf(error, "QUEUE_CANCEL_UNAVAILABLE") });
  }
}

function queuePort(port) {
  let control = null;
  const tell = (message) => {
    try {
      port.postMessage(message);
    } catch {
      // The panel is gone. No new read is asked for once it notices; ICBM holds the queue.
    }
  };
  port.onMessage.addListener(async (message) => {
    if (message.type === "discover") {
      tell({ type: "discovered", discovery: await discover() });
    } else if (message.type === "start" && !control) {
      control = queueControl(message.tab_id, message.declaration);
      const running = control;
      runQueue(running, tell).finally(() => {
        if (control === running) control = null;
      });
    } else if (message.type === "pause" && control) {
      control.pause();
    } else if (message.type === "resume" && control) {
      control.resume();
    } else if (message.type === "cancel") {
      if (control) control.stop();
      await cancel(message.queue_id, tell);
    }
  });
  // The panel is gone: no new read is asked for. A read already issued finishes its capture.
  port.onDisconnect.addListener(() => {
    if (control) control.pause();
  });
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "queue") {
    queuePort(port);
    return;
  }
  if (port.name !== "capture") return;
  port.onMessage.addListener(async (message) => {
    if (message.type !== "capture") return;
    // The panel may be closed while the capture runs: a message to a closed port is dropped.
    const tell = (message) => {
      try {
        port.postMessage(message);
      } catch {
        // The panel is gone. The capture still finishes; the run is in ICBM either way.
      }
    };
    const result = await capture((state, runId) =>
      tell({ type: "progress", state, collection_run_id: runId || null }),
    );
    tell({ type: "result", result });
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
  if (message.type === "open-icbm") {
    openCollectionManagement(message.collection_run_id).then(respond);
    return true;
  }
  return false;
});
