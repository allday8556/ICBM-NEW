// The function the service worker injects into the operator's own list page (ADR-0019 §8.1).
//
// It reads only the page the operator already loaded, and returns only product URLs: a link the
// operator can see, on the supplier's storefront host, whose path fully matches the supplier's
// reviewed product path form, as its scheme, host and path. Nothing of the list page itself — its
// URL, its HTML, an anchor's text — and no credentials, query or fragment ever leaves the page. A
// product named only in a query is not discovered.
//
// Only a link the operator can see is the operator's list. A real KM통상 list page (2026-10-02)
// held links the page did not show — one of them to a product the storefront no longer served —
// and a queue read of such a link stops the queue at a product the operator never saw. A hidden
// anchor is never discovered: this narrows discovery, as ADR-0019 §8.1 allows, and widens nothing.
//
// It is serialized and run in the page, so it is self-contained: it reads the DOM, talks to
// nothing, and returns. The server judges every link again; this filter keeps material in the page
// and never decides what is read.

export function discoverInPage(rule) {
  // Whether the operator can see this anchor: it is laid out and not hidden by style.
  const shown = (anchor) =>
    anchor.getClientRects().length > 0 && getComputedStyle(anchor).visibility !== "hidden";
  let form;
  try {
    form = new RegExp(`^(?:${rule.productPath})$`);
  } catch {
    return { ok: false, code: "DISCOVERY_RULE_UNREADABLE" };
  }
  const links = [];
  const seen = new Set();
  let found = 0;
  for (const anchor of document.querySelectorAll("a[href]")) {
    let url;
    try {
      url = new URL(anchor.href);
    } catch {
      continue;
    }
    if (url.protocol !== "https:" || url.hostname !== rule.host) continue;
    if (!shown(anchor)) continue;
    if (url.username || url.password || url.port) continue;
    if (!form.test(url.pathname)) continue;
    const link = `https://${url.hostname}${url.pathname}`;
    if (seen.has(link)) continue;
    seen.add(link);
    found += 1;
    if (links.length < rule.maxLinks) links.push(link);
  }
  return { ok: true, links, found };
}
