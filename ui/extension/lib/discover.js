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
// Discovery reads nothing, so it never scrolls or touches the page: a scroll could make a lazy
// list load more from the supplier, which is a read the server did not issue. What the operator
// can see is read from the page as it stands: the browser's own visibility check, a painted box,
// that box not clipped away by the anchor or an ancestor, and inside the page the operator can
// scroll to. A link concealed by something these do not read (an element painted on top of it) is
// a product the operator collects with a single click; it is never read by the queue twice.
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
  // Whether an element clips what is inside it. A clip path is not measured: an element under
  // one is taken as concealed, which can only leave a product to the operator's own click.
  const clips = (style) =>
    style.overflowX !== "visible" || style.overflowY !== "visible" || style.clip !== "auto";
  const concealed = (style) => style.clipPath !== "none";
  // The part of a box the anchor and its ancestors let through: each one that clips cuts the box
  // down to its own; a box cut to nothing, or under a clip path, is not shown.
  const unclipped = (rect, anchor) => {
    let left = rect.left;
    let top = rect.top;
    let right = rect.right;
    let bottom = rect.bottom;
    for (let el = anchor; el && el !== document.body; el = el.parentElement) {
      const style = getComputedStyle(el);
      if (concealed(style)) return null;
      if (el === anchor || !clips(style)) continue;
      const own = el.getBoundingClientRect();
      left = Math.max(left, own.left);
      top = Math.max(top, own.top);
      right = Math.min(right, own.right);
      bottom = Math.min(bottom, own.bottom);
      if (right <= left || bottom <= top) return null;
    }
    return { left, top, right, bottom };
  };
  // Whether the operator can see this anchor: the browser's own visibility check (display,
  // visibility, content-visibility and opacity, ancestors included; Chrome 105+, the manifest pins
  // 114), a painted box, that box not clipped away, and inside the page the operator can scroll
  // to. Nothing here scrolls or changes the page.
  const shown = (anchor) => {
    if (!anchor.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const painted = box(anchor);
    if (!painted) return false;
    const rect = unclipped(painted, anchor);
    if (!rect) return false;
    const root = document.documentElement;
    return (
      rect.right + window.scrollX > 0 &&
      rect.bottom + window.scrollY > 0 &&
      rect.left + window.scrollX < root.scrollWidth &&
      rect.top + window.scrollY < root.scrollHeight
    );
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
  return { ok: true, links, found };
}
