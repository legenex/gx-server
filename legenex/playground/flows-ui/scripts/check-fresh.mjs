#!/usr/bin/env node
// Build validation for the committed Creative Flows bundle (../web/flows).
//
// The Playground ships its frontend as committed static files, so the built
// React island is committed too. This check proves that the bundle in the tree
// is the one the current sources produce, that it is CSP-safe (no inline
// script, no eval, no external origin) and that it stays inside its budget.
//
//   node scripts/check-fresh.mjs          verify   (npm run check:fresh)
//   node scripts/check-fresh.mjs --write  stamp it (run by npm run build)
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, '..');
const outDir = resolve(uiRoot, '..', 'web', 'flows');
const STAMP = 'build-stamp.json';
const BUDGET = Number(process.env.GX_FLOWS_BUDGET_KB || 1600) * 1024;
const write = process.argv.includes('--write');
const errors = [];
const fail = (msg) => errors.push(msg);

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    if (e.name === 'node_modules' || e.name.startsWith('.')) return [];
    const p = join(dir, e.name);
    return e.isDirectory() ? walk(p) : [p];
  });
}

/** Everything that changes the bundle: the sources and the build configuration. */
function sourceFingerprint() {
  const files = [
    ...walk(join(uiRoot, 'src')),
    join(uiRoot, 'package.json'),
    join(uiRoot, 'package-lock.json'),
    join(uiRoot, 'vite.config.ts'),
    join(uiRoot, 'tsconfig.json'),
  ].filter((f) => existsSync(f)).sort();
  const h = createHash('sha256');
  for (const f of files) {
    h.update(relative(uiRoot, f).split('\\').join('/'));
    h.update('\0');
    h.update(readFileSync(f));
    h.update('\0');
  }
  return { hash: h.digest('hex'), files: files.length };
}

const fingerprint = sourceFingerprint();

if (!existsSync(outDir)) {
  fail(`the bundle directory ${relative(uiRoot, outDir)} does not exist - run "npm run build"`);
} else {
  const manifestPath = join(outDir, 'manifest.json');
  if (!existsSync(manifestPath)) {
    fail('manifest.json is missing from the bundle - run "npm run build"');
  } else {
    let manifest = {};
    try {
      manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
    } catch (err) {
      fail(`manifest.json is not valid JSON: ${String(err)}`);
    }
    const entry = Object.values(manifest).find((e) => e && e.isEntry);
    if (!entry) fail('manifest.json has no entry chunk');
    else if (!existsSync(join(outDir, entry.file))) {
      fail(`the entry ${entry.file} named by the manifest is missing`);
    }
    // With cssCodeSplit off the stylesheet is its own manifest record, so the
    // page loader collects every .css file the manifest names.
    const sheets = [
      ...(entry?.css ?? []),
      ...Object.values(manifest).map((e) => e.file).filter((f) => typeof f === 'string' && f.endsWith('.css')),
    ];
    if (!sheets.length) fail('the manifest names no stylesheet: the island would render unstyled');
    for (const css of sheets) {
      if (!existsSync(join(outDir, css))) fail(`the stylesheet ${css} named by the manifest is missing`);
    }
    for (const chunk of Object.values(manifest)) {
      for (const f of chunk.imports ?? []) {
        if (!manifest[f]) fail(`manifest chunk ${chunk.file} imports unknown ${f}`);
      }
    }
  }

  const files = walk(outDir).filter((f) => !f.endsWith(STAMP));
  if (!files.length) fail('the bundle directory is empty');
  for (const f of files) {
    const name = relative(outDir, f);
    if (f.endsWith('.map')) fail(`${name}: source maps must not be shipped`);
    if (f.endsWith('.html')) fail(`${name}: the island is a module, it must not ship an HTML document`);
    if (!/\.(js|css|json|svg|woff2?|png|webp)$/.test(f)) fail(`${name}: unexpected file type in the bundle`);
    if (!/\.(js|css|json|svg)$/.test(f)) continue;
    const src = readFileSync(f, 'utf8');
    if (/(^|[^.\w])eval\s*\(|new\s+Function\s*\(|document\.write\s*\(/.test(src)) {
      fail(`${name}: forbidden dynamic code (eval / new Function / document.write) - blocked by the CSP`);
    }
    // Only loads matter: documentation URLs inside error messages are text.
    const loads = f.endsWith('.css')
      ? [/url\(\s*['"]?(https?:[^)'"]+)/, /@import\s+(?:url\()?['"](https?:[^'"]+)/]
      : [/\b(?:fetch|importScripts|import)\s*\(\s*['"`](https?:[^'"`]+)/,
        /\.(?:src|href)\s*=\s*['"`](https?:[^'"`]+)/,
        /new\s+(?:Worker|EventSource|WebSocket)\s*\(\s*['"`]((?:https?|wss?):[^'"`]+)/];
    for (const re of loads) {
      const hit = re.exec(src);
      if (hit) fail(`${name}: loads the external resource ${hit[1]} - the CSP allows 'self' only`);
    }
    if (/sk-[A-Za-z0-9]{20,}|LITELLM_MASTER_KEY|GX_SWAP_API_KEY/.test(src)) {
      fail(`${name}: credential or credential name in the bundle`);
    }
  }

  const total = files.reduce((n, f) => n + statSync(f).size, 0);
  if (total > BUDGET) fail(`bundle size ${total} B exceeds the budget ${BUDGET} B`);

  const stampPath = join(outDir, STAMP);
  if (write) {
    writeFileSync(stampPath, `${JSON.stringify({
      sources: fingerprint.hash, source_files: fingerprint.files, bytes: total, built_at: new Date().toISOString(),
    }, null, 2)}\n`);
  } else if (!existsSync(stampPath)) {
    fail(`${STAMP} is missing - run "npm run build"`);
  } else {
    let stamp = {};
    try {
      stamp = JSON.parse(readFileSync(stampPath, 'utf8'));
    } catch (err) {
      fail(`${STAMP} is not valid JSON: ${String(err)}`);
    }
    if (stamp.sources !== fingerprint.hash) {
      fail('the committed bundle is older than the sources - run "npm run build" and commit ../web/flows');
    }
    if (stamp.bytes !== total) fail('the bundle files changed after the last build - run "npm run build"');
  }

  if (!errors.length) {
    console.log(`flows bundle OK: ${files.length} files, ${(total / 1024).toFixed(1)} KiB `
      + `(budget ${BUDGET / 1024} KiB), ${fingerprint.files} sources${write ? ' - stamped' : ''}`);
  }
}

if (errors.length) {
  console.error(`flows bundle check FAILED (${errors.length}):`);
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}
