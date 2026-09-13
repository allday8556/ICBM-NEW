// The single help-tooltip owner (replaces v29's three overlapping v18/v22/v27 generations).
// Explanatory copy never sits visibly under a title; it lives behind one "i" icon per anchor.

import { h } from './dom.js';

let tooltip = null;

export function helpIcon(text) {
  return h('span', {
    class: 'help-icon',
    role: 'button',
    tabindex: '0',
    'aria-label': `도움말: ${text}`,
    'data-help': text,
  }, 'i');
}

export function withHelp(anchor, text) {
  if (!text) return anchor;
  anchor.classList.add('help-anchor');
  anchor.append(helpIcon(text));
  return anchor;
}

function show(icon) {
  if (!tooltip) return;
  tooltip.textContent = icon.dataset.help;
  tooltip.hidden = false;
  const margin = 12;
  const anchor = icon.getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  let left = anchor.left;
  let top = anchor.bottom + 8;
  if (left + box.width > window.innerWidth - margin) left = window.innerWidth - box.width - margin;
  if (left < margin) left = margin;
  if (top + box.height > window.innerHeight - margin) top = anchor.top - box.height - 8;
  if (top < margin) top = margin;
  tooltip.style.left = `${Math.round(left)}px`;
  tooltip.style.top = `${Math.round(top)}px`;
}

export function hideHelp() {
  if (tooltip) tooltip.hidden = true;
}

function iconFrom(event) {
  return event.target instanceof Element ? event.target.closest('.help-icon') : null;
}

export function initHelp() {
  tooltip = document.getElementById('helpTooltip');
  document.addEventListener('pointerover', (event) => {
    const icon = iconFrom(event);
    if (icon) show(icon);
  });
  document.addEventListener('pointerout', (event) => {
    const icon = iconFrom(event);
    if (icon && !icon.contains(event.relatedTarget)) hideHelp();
  });
  document.addEventListener('focusin', (event) => {
    const icon = iconFrom(event);
    if (icon) show(icon);
  });
  document.addEventListener('focusout', (event) => {
    if (iconFrom(event)) hideHelp();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideHelp();
  });
  window.addEventListener('scroll', hideHelp, true);
  window.addEventListener('resize', hideHelp);
}
