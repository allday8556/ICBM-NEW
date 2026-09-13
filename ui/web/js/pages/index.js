// The ten top-level screens, in v29 navigation order (UI_SOURCE_OF_TRUTH: current visual IA).

import aiInsight from './ai-insight.js';
import analytics from './analytics.js';
import collect from './collect.js';
import dashboard from './dashboard.js';
import db from './db.js';
import inquiry from './inquiry.js';
import orders from './orders.js';
import register from './register.js';
import settings from './settings.js';
import soldout from './soldout.js';

export const PAGES = [dashboard, collect, db, register, orders, inquiry, soldout, aiInsight, analytics, settings];

export const PAGE_BY_KEY = new Map(PAGES.map((page) => [page.key, page]));
