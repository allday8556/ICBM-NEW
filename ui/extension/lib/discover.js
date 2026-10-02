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
// Whether the operator can see an anchor is not decided by reading styles one by one — every
// concealment has another — but by asking the browser what it paints at the anchor's own spot:
// the anchor is scrolled into the viewport and hit-tested at the centre of its painted box. That
// is one rule for display, visibility, opacity, clipping of every kind, an overlay on top, a box
// off the page and a box of no size. The page is only scrolled, never modified, and the scroll is
// put back.
//
// It is serialized and run in the page, so it is self-contained: it reads the DOM, talks to
// nothing, and returns. The server judges every link again; this filter keeps material in the page
// and never decides what is read.

export function discoverInPage(rule) {
  // The box an anchor paints: its own, or its first painted descendant's when the anchor itself
  // is an empty inline around block children.
  const box = (anchor) => {
    for (const element of [anchor, ...anchor.querySelectorAll("*")]) {
      const rect = element.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) return rect;
    }
    return null;
  };
  // Whether the operator can see this anchor: the browser's own visibility check first (display,
  // visibility, content-visibility and opacity, ancestors included; Chrome 105+, the manifest pins
  // 114), then the browser's own paint at the centre of the anchor's box once it is scrolled into
  // the viewport. What is painted there must be the anchor or something inside it.
  const shown = (anchor) => {
    if (!anchor.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    if (!box(anchor)) return false;
    anchor.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const rect = box(anchor);
    if (!rect) return false;
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
    const hit = document.elementFromPoint(x, y);
    return hit !== null && (hit === anchor || anchor.contains(hit));
  };
  let form;
  try {
    form = new RegExp(`^(?:${rule.productPath})$`);
  } catch {
    return { ok: false, code: "DISCOVERY_RULE_UNREADABLE" };
  }
  const links = [];
  const seen = new Set();
  let found = 0;
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;
  try {
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
      if (!shown(anchor)) continue;
      seen.add(link);
      found += 1;
      if (links.length < rule.maxLinks) links.push(link);
    }
  } finally {
    window.scrollTo({ left: scrollX, top: scrollY, behavior: "instant" });
  }
  return { ok: true, links, found };
}
