// The only code that talks to ICBM (ADR-0019 §3, §12.4). It runs in the service worker.
//
// - The capture policy is read on every click, and its bytes must hash to the digest the server
//   names; a policy that cannot be proven is never used.
// - A capture is sent once. The answer is a transport state: the outcome is read back from the
//   canonical run afterwards and is never invented here.
// - Nothing is kept: no page material, no policy, no result. The pairing is the only thing this
//   extension stores, and it is read from the extension's own storage by the caller.

import { sha256Hex, signedHeaders } from "./signing.js";

const POLICY_PATH = (supplierKey) => `/api/v1/collect/extension/capture-policies/${supplierKey}`;
const CAPTURE_PATH = "/api/v1/collect/extension/captures";
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

// Send one capture. The answer names the run to read back, and says nothing about its outcome.
export async function sendCapture(pairing, extensionId, { supplierKey, revision, digest, capture }) {
  const body = JSON.stringify({
    supplier_key: supplierKey,
    policy: { revision, digest },
    transport: capture.transport,
    html: capture.html,
  });
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
