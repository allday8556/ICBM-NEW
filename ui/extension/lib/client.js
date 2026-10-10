// The only code that talks to ICBM (ADR-0019 §3, §12.4). It runs in the service worker.
//
// - The capture policy is read on every click, and its bytes must hash to the digest the server
//   names; a policy that cannot be proven is never used.
// - A capture is sent once. The answer is a transport state: the outcome is read back from the
//   canonical run afterwards and is never invented here.
// - Nothing is kept: no page material, no policy, no result. The pairing is the only thing this
//   extension stores, and it is read from the extension's own storage by the caller.
// - A list queue (ADR-0019 §8.1) is declared, read, asked for its next read and cancelled through
//   the same signed requests. ICBM decides every read; this client only asks and relays.

import { sha256Hex, signedHeaders } from "./signing.js";

const POLICY_PATH = (supplierKey) => `/api/v1/collect/extension/capture-policies/${supplierKey}`;
const CAPTURE_PATH = "/api/v1/collect/extension/captures";
const QUEUE_POLICY_PATH = (supplierKey) => `/api/v1/collect/extension/queue-policies/${supplierKey}`;
const QUEUES_PATH = "/api/v1/collect/extension/queues";
const SUPPLIERS_PATH = "/api/v1/collect/extension/suppliers";
const QUEUE_PATH = (queueId) => `${QUEUES_PATH}/${encodeURIComponent(queueId)}`;
const RUN_PATH = (runId) => `/api/v1/collect/collections/${encodeURIComponent(runId)}`;
const REVISION_PATH = (revisionId) => `/api/v1/collect/revisions/${encodeURIComponent(revisionId)}`;
const POLICY_DIGEST_HEADER = "X-ICBM-Capture-Policy-Digest";
const encoder = new TextEncoder();

export class IcbmRefused extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

async function request(pairing, extensionId, { method, path, body }) {
  const bytes = body === undefined ? new Uint8Array() : encoder.encode(body);
  const headers = await signedHeaders({ method, path, extensionId, pairing, body: bytes });
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(`${pairing.origin}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : bytes,
      // No cookie, no credential and no redirect ever travels with a request to ICBM.
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
    });
  } catch {
    throw new IcbmRefused("ICBM_DISCONNECTED");
  }
  return response;
}

async function refusal(response) {
  try {
    const envelope = await response.json();
    return new IcbmRefused(envelope.error.code);
  } catch {
    return new IcbmRefused(`ICBM_HTTP_${response.status}`);
  }
}

// The reviewed capture policy of one supplier, proven against the digest the server names.
export async function fetchPolicy(pairing, extensionId, supplierKey) {
  const response = await request(pairing, extensionId, {
    method: "GET",
    path: POLICY_PATH(supplierKey),
  });
  if (!response.ok) throw await refusal(response);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const digest = response.headers.get(POLICY_DIGEST_HEADER);
  if (!digest || (await sha256Hex(bytes)) !== digest) {
    throw new IcbmRefused("CAPTURE_POLICY_DIGEST_MISMATCH");
  }
  const policy = JSON.parse(new TextDecoder().decode(bytes));
  return { policy, revision: policy.revision, digest };
}

// Send one capture. The answer names the run to read back, and says nothing about its outcome. A
// queue read's capture carries its single-use ticket; a single click carries none.
export async function sendCapture(
  pairing,
  extensionId,
  { supplierKey, revision, digest, capture, ticket },
) {
  const envelope = {
    supplier_key: supplierKey,
    policy: { revision, digest },
    transport: capture.transport,
    html: capture.html,
  };
  if (ticket) envelope.queue_ticket = ticket;
  const body = JSON.stringify(envelope);
  const response = await request(pairing, extensionId, { method: "POST", path: CAPTURE_PATH, body });
  if (response.status !== 202) throw await refusal(response);
  try {
    return await response.json();
  } catch {
    // Accepted, and the answer could not be read: the run exists and its identity is unknown.
    throw new IcbmRefused("ICBM_ACCEPTED_UNREADABLE");
  }
}

// One canonical read-back, exactly as ICBM holds it. `unreadable` names an answer that is not JSON.
async function readCanonical(pairing, path, unreadable) {
  let response;
  try {
    response = await fetch(`${pairing.origin}${path}`, {
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
    });
  } catch {
    throw new IcbmRefused("ICBM_DISCONNECTED");
  }
  if (!response.ok) throw await refusal(response);
  try {
    return await response.json();
  } catch {
    throw new IcbmRefused(unreadable);
  }
}

// The canonical run.
export function readRun(pairing, runId) {
  return readCanonical(pairing, RUN_PATH(runId), "RUN_READ_BACK_UNREADABLE");
}

// The canonical revision a recorded run names: its fields, their evidence and its image references,
// as COLLECT recorded them. The side panel previews it; it is never kept.
export function readRevision(pairing, revisionId) {
  return readCanonical(pairing, REVISION_PATH(revisionId), "REVISION_READ_BACK_UNREADABLE");
}

// One signed request whose answer is JSON, or the refusal ICBM named.
async function signedJson(pairing, extensionId, { method, path, body, expected = 200 }) {
  const response = await request(pairing, extensionId, { method, path, body });
  if (response.status !== expected) throw await refusal(response);
  try {
    return await response.json();
  } catch {
    throw new IcbmRefused("QUEUE_ANSWER_UNREADABLE");
  }
}

// The reviewed supplier hosts and their keys: the server's registry (ADR-0030 §6).
export function fetchSuppliers(pairing, extensionId) {
  return signedJson(pairing, extensionId, { method: "GET", path: SUPPLIERS_PATH });
}

// The supplier's reviewed product path form and its declared queue limits (ADR-0019 §8.1).
export function fetchQueuePolicy(pairing, extensionId, supplierKey) {
  return signedJson(pairing, extensionId, { method: "GET", path: QUEUE_POLICY_PATH(supplierKey) });
}

// Declare one queue from discovered product URLs and the operator's own bounds.
export function declareQueue(pairing, extensionId, declaration) {
  return signedJson(pairing, extensionId, {
    method: "POST",
    path: QUEUES_PATH,
    body: JSON.stringify(declaration),
    expected: 201,
  });
}

// Ask ICBM for the next read: it answers WAIT, ISSUE or DONE. ICBM decides; this only asks.
export function nextRead(pairing, extensionId, queueId) {
  return signedJson(pairing, extensionId, { method: "POST", path: `${QUEUE_PATH(queueId)}/next` });
}

// The queue as ICBM holds it: each item's own state beside its run's own outcome.
export function readQueue(pairing, extensionId, queueId) {
  return signedJson(pairing, extensionId, { method: "GET", path: QUEUE_PATH(queueId) });
}

export function cancelQueue(pairing, extensionId, queueId) {
  return signedJson(pairing, extensionId, { method: "POST", path: `${QUEUE_PATH(queueId)}/cancel` });
}

// Give back an issued read this browser could not capture, so ICBM goes on to the next item at
// once instead of waiting for the read to run out. The ticket is sent once and kept nowhere.
export function releaseRead(pairing, extensionId, queueId, ticket) {
  return signedJson(pairing, extensionId, {
    method: "POST",
    path: `${QUEUE_PATH(queueId)}/release`,
    body: JSON.stringify({ ticket }),
  });
}
