// The in-browser measurement of the Gate 3 area 3 visual acceptance (scripts/g3visual/checker.py).
// Evaluated by Playwright as one function; it describes elements by tag, class and data-*
// attributes only, never by their text.
([stateSelectors, populatedSelectors, screen]) => {
  const content = document.querySelector('#content');
  const describe = (el) => {
    const parts = [el.tagName.toLowerCase()];
    if (el.classList.length) parts.push('.' + el.classList[0]);
    for (const attr of el.attributes) {
      if (!attr.name.startsWith('data-')) continue;
      const value = attr.value.replace(/[^\x20-\x7e]/g, '?').slice(0, 48);
      parts.push(`[${attr.name}="${value}"]`);
    }
    return parts.join('');
  };
  const boxOf = (el) => el.getBoundingClientRect();
  const hidden = (el) => {
    if (!el.getClientRects().length) return 'no-box';
    const box = boxOf(el);
    if (box.width < 1 || box.height < 1) return 'zero-size';
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.visibility === 'hidden' || style.visibility === 'collapse') return 'visibility';
      if (Number(style.opacity) === 0) return 'opacity';
    }
    return null;
  };
  const cut = (node) => {
    const style = getComputedStyle(node);
    const clipsX = ['hidden', 'clip'].includes(style.overflowX) || style.textOverflow === 'ellipsis';
    const clipsY = ['hidden', 'clip'].includes(style.overflowY) || style.webkitLineClamp !== 'none';
    return (clipsX && node.scrollWidth > node.clientWidth + 1)
      || (clipsY && node.scrollHeight > node.clientHeight + 1);
  };
  const truncated = (el) => {
    if (cut(el)) return true;
    for (const child of el.querySelectorAll('*')) {
      if (child.textContent.trim() && cut(child)) return true;
    }
    return false;
  };
  const clipped = (el) => {
    const box = boxOf(el);
    for (let node = el.parentElement; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      const frame = boxOf(node);
      if (style.overflowX !== 'visible' && (box.left < frame.left - 1 || box.right > frame.right + 1)) {
        return 'x:' + describe(node);
      }
      if (['hidden', 'clip'].includes(style.overflowY) && (box.top < frame.top - 1 || box.bottom > frame.bottom + 1)) {
        return 'y:' + describe(node);
      }
      if (node === content) break;
    }
    return null;
  };
  const selector = stateSelectors.join(',');
  const elements = content ? [...new Set(content.querySelectorAll(selector))] : [];
  // A native <option> never has a layout box of its own: the <select> that shows it is judged.
  const subjects = elements.map((el) => (el.tagName === 'OPTION' ? el.closest('select') || el : el));
  const states = subjects.map((el, index) => {
    const found = { at: describe(elements[index]) };
    const why = hidden(el);
    if (why) {
      found.hidden = why;
      return found;
    }
    if (truncated(el)) found.truncated = true;
    const clip = clipped(el);
    if (clip) found.clipped = clip;
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    const box = boxOf(el);
    if (box.left < -1 || box.right > window.innerWidth + 1) found.offscreen = true;
    const x = Math.min(Math.max(box.left + box.width / 2, 0), window.innerWidth - 1);
    const y = Math.min(Math.max(box.top + Math.min(box.height, 40) / 2, 0), window.innerHeight - 1);
    const top = document.elementFromPoint(x, y);
    if (!top || !(top === el || el.contains(top))) {
      found.covered = top ? describe(top) : 'nothing';
    }
    found.box = [box.left, box.top + window.scrollY, box.right, box.bottom + window.scrollY];
    return found;
  });
  const overlaps = [];
  const visible = subjects.map((el, i) => [el, states[i]]).filter(([, s]) => !s.hidden);
  for (let i = 0; i < visible.length; i += 1) {
    for (let j = i + 1; j < visible.length; j += 1) {
      const [a, sa] = visible[i];
      const [b, sb] = visible[j];
      if (a.contains(b) || b.contains(a)) continue;
      const w = Math.min(sa.box[2], sb.box[2]) - Math.max(sa.box[0], sb.box[0]);
      const h = Math.min(sa.box[3], sb.box[3]) - Math.max(sa.box[1], sb.box[1]);
      if (w > 1 && h > 1) overlaps.push(`${sa.at} ~ ${sb.at}`);
    }
  }
  window.scrollTo(0, 0);
  const title = content ? content.querySelector('h1') : null;
  const icons = content ? [...content.querySelectorAll('.help-icon')] : [];
  const nav = [...document.querySelectorAll('#nav .nav-item')];
  const active = document.querySelector("#nav .nav-item[aria-current='page']");
  const populated = {};
  for (const sel of populatedSelectors) populated[sel] = content ? content.querySelectorAll(sel).length : 0;
  return {
    rendered: Boolean(content) && content.dataset.page === screen && !content.hasAttribute('aria-busy'),
    populated,
    document_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    states: states.map(({ box, ...rest }) => rest),
    overlaps,
    title_help_icons: title ? title.querySelectorAll('.help-icon').length : 0,
    unlabeled_help_icons: icons.filter((icon) => !icon.getAttribute('aria-label') || !icon.dataset.help).map(describe),
    nav_items: nav.length,
    active_nav: active ? active.dataset.page : null,
  };
}
