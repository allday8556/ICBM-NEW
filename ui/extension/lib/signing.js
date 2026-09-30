// The pairing signature (ADR-0019 §3): the same canonical request tuple the server verifies.
//
// Every request carries the pinned extension identity, the pairing id and generation, a
// timestamp, a nonce, the digest of its exact body and an HMAC-SHA256 over all of them. The
// secret is used here to sign and is never sent, logged or shown.

export const SIGNATURE_SCHEME = "ICBM-EXT-1";
export const CLIENT_NAME = "icbm-capture-extension";

const encoder = new TextEncoder();

const hex = (buffer) =>
  Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");

const base64UrlBytes = (value) => {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
};

export async function sha256Hex(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

export function newNonce() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function canonicalRequest({ method, path, extensionId, pairing, timestamp, nonce, bodySha256 }) {
  return [
    SIGNATURE_SCHEME,
    method.toUpperCase(),
    path,
    extensionId,
    pairing.pairing_id,
    String(pairing.generation),
    timestamp,
    nonce,
    bodySha256,
  ].join("\n");
}

// The headers of one signed request. `body` is the exact bytes that will be sent, or an empty
// array for a request without one.
export async function signedHeaders({ method, path, extensionId, pairing, body }) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = newNonce();
  const bodySha256 = await sha256Hex(body);
  const key = await crypto.subtle.importKey(
    "raw",
    base64UrlBytes(pairing.secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const message = canonicalRequest({ method, path, extensionId, pairing, timestamp, nonce, bodySha256 });
  const signature = hex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
  return {
    "X-ICBM-Client": CLIENT_NAME,
    "X-ICBM-Extension-Id": extensionId,
    "X-ICBM-Pairing-Id": pairing.pairing_id,
    "X-ICBM-Pairing-Generation": String(pairing.generation),
    "X-ICBM-Timestamp": timestamp,
    "X-ICBM-Nonce": nonce,
    "X-ICBM-Body-SHA256": bodySha256,
    "X-ICBM-Signature": signature,
  };
}

// A pairing code is what `icbm extension pair` printed: base64url JSON with the ICBM origin, the
// pairing id and generation, and the secret. Anything else is refused.
export function parsePairingCode(code) {
  let document;
  try {
    document = JSON.parse(new TextDecoder().decode(base64UrlBytes(code.trim())));
  } catch {
    return null;
  }
  const origin = typeof document.origin === "string" ? document.origin : "";
  const loopback = /^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(origin);
  if (
    document.v !== 1 ||
    !loopback ||
    typeof document.pairing_id !== "string" ||
    !Number.isInteger(document.generation) ||
    document.generation < 1 ||
    typeof document.secret !== "string" ||
    document.secret.length < 40
  ) {
    return null;
  }
  return {
    origin,
    pairing_id: document.pairing_id,
    generation: document.generation,
    secret: document.secret,
  };
}
