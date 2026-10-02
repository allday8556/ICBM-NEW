// The function the service worker injects into the operator's own list page (ADR-0019 §8.1).
//
// It reads only the page the operator already loaded, and returns only product URLs: a link on the
// supplier's storefront host whose path fully matches the supplier's reviewed product path form,
// as its scheme, host and path. Nothing of the list page itself — its URL, its HTML, an anchor's
// text — and no credentials, query or fragment ever leaves the page. A product named only in a
// query is not discovered.
//
// It is serialized and run in the page, so it is self-contained: it reads the DOM, talks to
// nothing, and returns. The server judges every link again; this filter keeps material in the page
// and never decides what is read.

export function discoverInPage(rule) {
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
