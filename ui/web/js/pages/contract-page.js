// A top-level screen rendered from its server contract: page head + the approved runtime-ready
// empty state (v28/v29), with its call to action routed through navigate(). A screen that has a
// READY renderer (``spec.ready``) draws the server's READY state with it. A screen whose READY
// content is its always-shown owner panel (``spec.extra``) draws only that, and a screen whose
// feature is not built yet says so plainly (``spec.readyNote``), never as a "not implemented" error.

import { getJson } from '../core/api.js';
import { fragment } from '../core/dom.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, unsupportedState } from '../components/states.js';

// ``empty`` may depend on the view (for example, no "connect" button once a channel is connected).
function emptyOf(spec, view) {
  return typeof spec.empty === 'function' ? spec.empty(view) : spec.empty;
}

export function contractPage(spec) {
  return {
    key: spec.key,
    title: spec.title,
    navLabel: spec.navLabel ?? spec.title,
    icon: spec.icon,
    async render(ctx) {
      const view = await getJson(spec.endpoint);
      const head = pageHead({
        title: spec.title,
        help: spec.help,
        actions: spec.headActions ? spec.headActions(view) : [],
      });
      // An owner panel the screen always shows, whatever the contract's state (M6).
      const extra = spec.extra ? spec.extra(ctx) : null;
      if (view.meta.state === 'READY') {
        if (spec.ready) return fragment(head, spec.ready(view, ctx), extra);
        const note = spec.readyNote ? spec.readyNote(view) : null;
        return fragment(head, note ? emptyState(note) : null, extra);
      }
      if (view.meta.state !== 'EMPTY') return fragment(head, unsupportedState(view.meta), extra);
      const { title, copy, actionLabel, to } = emptyOf(spec, view);
      return fragment(
        head,
        emptyState({
          title,
          copy,
          action: actionLabel && to ? { label: actionLabel, onSelect: () => ctx.navigate(to.page, to.params) } : null,
        }),
        extra,
      );
    },
  };
}
