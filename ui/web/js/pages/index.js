// The ten top-level screens, in v29 navigation order (UI_SOURCE_OF_TRUTH: current visual IA).

import aiInsight from './ai-insight.js';
import analytics from './analytics.js';
import collect from './collect.js';
import dashboard from './dashboard.js';
import db from './db.js';
import inquiry from './inquiry.js';
import orders from './orders.js';
import register from './register.js';
import registerEditor from './register-editor.js';
import settings from './settings.js';
import soldout from './soldout.js';

export const PAGES = [dashboard, collect, db, register, orders, inquiry, soldout, aiInsight, analytics, settings];

// Pages reached by route only, never from the navigation: the product editor opens in its own tab.
const ROUTED_ONLY = [registerEditor];

export const PAGE_BY_KEY = new Map([...PAGES, ...ROUTED_ONLY].map((page) => [page.key, page]));
