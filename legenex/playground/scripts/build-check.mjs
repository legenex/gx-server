#!/usr/bin/env node
// Build validation for GX-Playground's dependency-free frontend: the same rules
// as the Control Center (ES modules, CSP safety, no innerHTML, no credentials),
// with the Playground's own pages and budget.
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
process.env.GX_BUILD_ROOT = process.env.GX_PG_STATIC_DIR
  ? resolve(process.env.GX_PG_STATIC_DIR) : resolve(here, '..', 'web');
process.env.GX_BUILD_PAGES = 'dashboard,images,video,music,voice,library,history,models,logs,settings';
process.env.GX_BUILD_NAV_IN_HTML = '0';
process.env.GX_BUILD_BUDGET_KB = '600';
await import('../../control-ui/scripts/build-check.mjs');
