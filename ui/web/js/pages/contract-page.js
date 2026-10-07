// A top-level screen rendered from its server contract: page head + the approved runtime-ready
// empty state (v28/v29), with its call to action routed through navigate(). A screen that has a
// READY renderer (``spec.ready``) draws the server's READY state with it; any other state is
// surfaced honestly instead of guessed at.

import { getJson } from '../core/api.js';
import { fragment } from '../core/dom.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, unsupportedState } from '../components/states.js';

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
      if (view.meta.state === 'READY' && spec.ready) return fragment(head, spec.ready(view, ctx), extra);
      if (view.meta.state !== 'EMPTY') return fragment(head, unsupportedState(view.meta), extra);
      const { title, copy, actionLabel, to } = spec.empty;
      return fragment(
        head,
        emptyState({ title, copy, action: { label: actionLabel, onSelect: () => ctx.navigate(to.page, to.params) } }),
        extra,
      );
    },
  };
}
