// A top-level screen whose M0 contract can only report EMPTY: page head + the approved
// runtime-ready empty state (v28/v29), with its call to action routed through navigate().

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
      if (view.meta.state !== 'EMPTY') return fragment(head, unsupportedState(view.meta));
      const { title, copy, actionLabel, to } = spec.empty;
      return fragment(
        head,
        emptyState({ title, copy, action: { label: actionLabel, onSelect: () => ctx.navigate(to.page, to.params) } }),
      );
    },
  };
}
