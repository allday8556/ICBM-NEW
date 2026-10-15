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

  // ---- 5. the embedded-data member (ADR-0040 §3; 1688 only) -------------------------------
  // Only a policy that declares `embedded_data` has one. Exactly one inline script that starts
  // with the declared prefix is read; the text between the argument marker and the terminator is
  // parsed as data (JSON.parse, never run), bounded, and projected onto the declared allowlist.
  // Nothing outside the allowlist leaves the page, and the script's text is never sent. Any
  // mismatch sends no data at all, only the code that says why: the DOM capture itself goes on.
  const embedded = (rule) => {
    const mismatch = (code) => ({ refused: code });
    const scripts = Array.from(document.scripts).filter(
      (script) => !script.hasAttribute("src") && script.text.trimStart().startsWith(rule.prefix),
    );
    if (scripts.length !== 1) return mismatch("EMBEDDED_SCRIPT_NOT_EXACTLY_ONE");
    const source = scripts[0].text.trim();
    const at = source.indexOf(rule.argument_marker);
    if (at < 0 || source.indexOf(rule.argument_marker, at + 1) >= 0) {
      return mismatch("EMBEDDED_MARKER_NOT_EXACTLY_ONE");
    }
    if (!source.endsWith(rule.terminator)) return mismatch("EMBEDDED_TERMINATOR");
    let argument = source.slice(at + rule.argument_marker.length, -rule.terminator.length);
    if (new TextEncoder().encode(argument).length > rule.max_bytes) {
      return mismatch("EMBEDDED_OVER_BYTES");
    }
    if (rule.bare_integer_keys) {
      // The one departure from JSON a policy may declare: an object key written as a bare decimal
      // integer ({5919566953309:0.23}). Outside string literals, a run of digits right after "{"
      // or "," and before ":" is quoted; JSON.parse then decides everything else.
      let quoted = "";
      let inString = false;
      let previous = "";
      for (let index = 0; index < argument.length; ) {
        const character = argument[index];
        if (inString) {
          if (character === "\\") {
            quoted += argument.slice(index, index + 2);
            index += 2;
            continue;
          }
          if (character === '"') inString = false;
          quoted += character;
          index += 1;
          continue;
        }
        if (character === '"') {
          inString = true;
        } else if ((previous === "{" || previous === ",") && character >= "0" && character <= "9") {
          let end = index;
          while (end < argument.length && argument[end] >= "0" && argument[end] <= "9") end += 1;
          let after = end;
          while (after < argument.length && /\s/.test(argument[after])) after += 1;
          if (argument[after] === ":") {
            quoted += `"${argument.slice(index, end)}"`;
            previous = '"';
            index = end;
            continue;
          }
        }
        quoted += character;
        if (!/\s/.test(character)) previous = character;
        index += 1;
      }
      argument = quoted;
    }
    let parsed;
    try {
      parsed = JSON.parse(argument);
    } catch {
      return mismatch("EMBEDDED_NOT_JSON");
    }
    // The depth bound, walked without recursion so no document can exhaust the stack.
    const pending = [[parsed, 1]];
    while (pending.length) {
      const [value, depth] = pending.pop();
      if (depth > rule.max_depth) return mismatch("EMBEDDED_OVER_DEPTH");
      if (value !== null && typeof value === "object") {
        for (const child of Object.values(value)) pending.push([child, depth + 1]);
      }
    }
    // The allowlist as a tree. A path is dot-separated segments: a key name, or "*" for every key
    // of a map; a segment ending in "[]" names an array whose every element the rest applies to.
    const tree = () => ({ keys: new Map(), any: null, each: false, type: null });
    const root = tree();
    for (const entry of rule.allowlist) {
      let node = root;
      for (const segment of entry.path.split(".")) {
        const name = segment.endsWith("[]") ? segment.slice(0, -2) : segment;
        let next;
        if (name === "*") {
          next = node.any || (node.any = tree());
        } else {
          next = node.keys.get(name) || tree();
          node.keys.set(name, next);
        }
        next.each = segment.endsWith("[]");
        node = next;
      }
      node.type = entry.type;
    }
    const WRONG = {};
    const fits = (value, type) =>
      (type === "string" && typeof value === "string") ||
      (type === "integer" && Number.isSafeInteger(value)) ||
      (type === "number" && typeof value === "number" && Number.isFinite(value)) ||
      (type === "boolean" && typeof value === "boolean");
    // One value under one node: a leaf of its declared type, or an object of its allowed keys
    // only. A null is no value; a wrong shape or type refuses the whole member.
    const every = (value, node) => {
      if (!Array.isArray(value)) throw WRONG;
      return value.map((element) => one(element, node));
    };
    const one = (value, node) => {
      if (node.type !== null) {
        if (!fits(value, node.type)) throw WRONG;
        return value;
      }
      if (value === null || typeof value !== "object" || Array.isArray(value)) throw WRONG;
      const kept = {};
      const keep = (key, child) => {
        if (!Object.hasOwn(value, key) || value[key] === null) return;
        Object.defineProperty(kept, key, {
          value: child.each ? every(value[key], child) : one(value[key], child),
          enumerable: true,
          writable: true,
          configurable: true,
        });
      };
      for (const [key, child] of node.keys) keep(key, child);
      if (node.any) for (const key of Object.keys(value)) keep(key, node.any);
      return kept;
    };
    try {
      return { projection: one(parsed, root) };
    } catch (failure) {
      if (failure === WRONG) return mismatch("EMBEDDED_WRONG_SHAPE");
      throw failure;
    }
  };
  const member = policy.embedded_data ? { embedded_data: embedded(policy.embedded_data) } : {};

  return {
    ok: true,
    html,
    ...member,
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
