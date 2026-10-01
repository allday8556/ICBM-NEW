// The product-scope cut (ADR-0019 §6): what runs inside the captured tab, and nothing else does.
//
// `captureInPage` is injected with `chrome.scripting.executeScript`, so it is one self-contained
// function: it may use nothing outside its own body. It receives the reviewed BrowserCapturePolicy
// the server just served and answers either a refusal code or the capture.
//
// The order is fixed.
//   1. The preconditions, from the navigation entry and the document alone. Nothing of the page's
//      content is read before they hold: a redirected navigation, a status the browser did not
//      observe as 200 or a document that is not text/html refuses here.
//   2. The scope: exactly one product root, the policy's other allowed regions, and from <head>
//      only the elements the policy names.
//   3. The cut: a new serialization that keeps only the allowed attributes and leaves out every
//      excluded tag and region. The page itself is never modified.
//   4. The bounds. A capture over a bound is refused whole; nothing is truncated.
//
// It reads no cookie, no storage, no form value and no request or response header, and it returns
// nothing but the capture and what the browser observed of the navigation.
export function captureInPage(policy) {
  const refuse = (code) => ({ ok: false, code });

  // ---- 1. preconditions -------------------------------------------------------------------
  const entries = performance.getEntriesByType("navigation");
  const navigation = entries.length === 1 ? entries[0] : null;
  if (!navigation || typeof navigation.responseStatus !== "number") {
    return refuse("EVIDENCE_STATUS_UNAVAILABLE");
  }
  if (navigation.responseStatus !== 200) return refuse("EVIDENCE_STATUS_NOT_200");
  if (navigation.redirectCount !== 0) return refuse("EVIDENCE_REDIRECTED");
  if (navigation.name !== location.href) return refuse("EVIDENCE_URL_NOT_NAVIGATED");
  if (document.contentType !== "text/html") return refuse("EVIDENCE_CONTENT_TYPE");
  if (!document.characterSet) return refuse("EVIDENCE_CHARSET_UNAVAILABLE");
  if (location.protocol !== "https:" || location.hostname !== policy.host) {
    return refuse("HOST_NOT_REVIEWED");
  }
  // The server's target check refuses a port or a fragment; nothing is cut for a page it refuses.
  if (location.port !== "" || location.hash !== "") return refuse("TARGET_NOT_PLAIN_PRODUCT_URL");

  // ---- 2. scope ---------------------------------------------------------------------------
  const VOID = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    "track", "wbr",
  ]);
  const excludedTags = new Set(policy.excluded_tags);
  const allowed = policy.allowed_attributes;
  const everywhere = new Set(allowed["*"] || []);
  const keeps = (tag, name) =>
    everywhere.has(name) || (Object.hasOwn(allowed, tag) && allowed[tag].includes(name));
  const carries = (element, region) =>
    region.by === "id" ? element.id === region.token : element.classList.contains(region.token);
  const matching = (region) =>
    Array.from(document.querySelectorAll(region.by === "id" ? "[id]" : "[class]")).filter(
      (element) => carries(element, region),
    );

  const roots = matching(policy.product_root);
  if (roots.length !== 1) return refuse("PRODUCT_ROOT_NOT_EXACTLY_ONE");
  const scope = [roots[0]];
  for (const region of policy.allowed_regions) {
    const found = matching(region);
    if (found.length > 1) return refuse("ALLOWED_REGION_AMBIGUOUS");
    if (found.length === 1 && !scope.some((kept) => kept.contains(found[0]))) {
      scope.push(found[0]);
    }
  }
  // Document order, and no region twice: a region inside another kept region is already cut.
  scope.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  const outer = scope.filter((region) => !scope.some((other) => other !== region && other.contains(region)));
  // A kept region that is an excluded region, or sits inside one, is not product scope at all.
  const excludedAbove = (node) => {
    for (let at = node; at && at.nodeType === Node.ELEMENT_NODE; at = at.parentElement) {
      if (policy.excluded_regions.some((region) => carries(at, region))) return true;
    }
    return false;
  };
  if (outer.some(excludedAbove)) return refuse("SCOPE_INSIDE_EXCLUDED_REGION");

  // ---- 3. the cut -------------------------------------------------------------------------
  const bounds = policy.bounds;
  let nodes = 0;
  let imageRefs = 0;
  let over = null;
  const text = (value) =>
    value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const quoted = (value) => text(value).replace(/"/g, "&quot;");

  const element = (node, inHead) => {
    const tag = node.localName;
    if (!inHead && excludedTags.has(tag)) return "";
    if (!inHead && policy.excluded_regions.some((region) => carries(node, region))) return "";
    nodes += 1;
    if (nodes > bounds.max_nodes) over = "CEILING_NODES";
    let open = `<${tag}`;
    for (const attribute of Array.from(node.attributes)) {
      const name = attribute.name.toLowerCase();
      if (!keeps(tag, name)) continue;
      open += ` ${name}="${quoted(attribute.value)}"`;
      if (tag === "img" && !["id", "class", "style"].includes(name)) {
        imageRefs +=
          name === "srcset"
            ? attribute.value.split(",").filter((entry) => entry.trim()).length
            : attribute.value.trim()
              ? 1
              : 0;
      }
    }
    open += ">";
    if (VOID.has(tag)) return open;
    let inner = "";
    for (const child of Array.from(node.childNodes)) {
      if (over) break;
      if (child.nodeType === Node.ELEMENT_NODE) inner += element(child, inHead);
      else if (child.nodeType === Node.TEXT_NODE) inner += text(child.nodeValue);
      // Comments, processing instructions and everything else are left out.
    }
    return `${open}${inner}</${tag}>`;
  };

  let head = "";
  // In document order, and each element once, even when two rules name it.
  for (const candidate of Array.from(document.head.children)) {
    const named = policy.head_allowance.some((rule) => {
      if (candidate.localName !== rule.tag) return false;
      const stated = (candidate.getAttribute(rule.attribute) || "").toLowerCase().split(/\s+/);
      return stated.includes(rule.value.toLowerCase());
    });
    if (named) head += element(candidate, true);
  }
  let body = "";
  for (const region of outer) body += element(region, false);
  if (!over && body === "") return refuse("PRODUCT_SCOPE_EMPTY");
  if (over) return refuse(over);
  if (imageRefs > bounds.max_image_refs) return refuse("CEILING_IMAGE_REFS");

  // ---- 4. the bounds ----------------------------------------------------------------------
  // nodes counts <html>, <head> and <body> too, exactly as the server counts what arrives.
  nodes += 3;
  if (nodes > bounds.max_nodes) return refuse("CEILING_NODES");
  const html = `<!doctype html><html><head>${head}</head><body>${body}</body></html>`;
  if (new TextEncoder().encode(html).length > bounds.max_html_bytes) {
    return refuse("CEILING_HTML_BYTES");
  }
  return {
    ok: true,
    html,
    transport: {
      url: location.href,
      navigation_name: navigation.name,
      response_status: navigation.responseStatus,
      redirect_count: navigation.redirectCount,
      content_type: document.contentType,
      character_set: document.characterSet,
    },
  };
}
