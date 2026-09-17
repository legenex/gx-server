#!/usr/bin/env node
// Build validation for GX-Playground's dependency-free frontend: the same rules
// as the Control Center (ES modules, CSP safety, no innerHTML, no credentials),
// with the Playground's own pages and budget.
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
process.env.GX_BUILD_ROOT = process.env.GX_PG_STATIC_DIR
  ? resolve(process.env.GX_PG_STATIC_DIR) : resolve(here, '..', 'web');
process.env.GX_BUILD_PAGES = 'dashboard,images,video,music,voice,live,call,library,history,models,logs,settings';
process.env.GX_BUILD_NAV_IN_HTML = '0';
// Build V3 adds four pages (Creative Flows, Voice, Call Agents, Live). Pages
// are lazy-loaded, so the aggregate is a hygiene number, not what a visitor
// downloads; the per-module cap below is the one that bounds a single page.
process.env.GX_BUILD_BUDGET_KB = '700';
process.env.GX_BUILD_MODULE_KB = '64';
// Creative Flows ships a generated Vite bundle; it is validated by its own
// build (flows-ui) and must not be measured against the hand-written budget
// or the hand-written-source rules.
process.env.GX_BUILD_EXCLUDE = 'flows';
await import('../../control-ui/scripts/build-check.mjs');
