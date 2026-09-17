#!/usr/bin/env node
// Build validation for the dependency-free frontend in web/.
//
// The UI ships as native ES modules (no bundler, no runtime npm packages), so
// "build" means proving the static bundle is complete and CSP-safe:
//   * every module parses (node --check)
//   * every relative import resolves, and every named import is exported
//   * no unused imports
//   * index.html references only existing local assets, has no inline script,
//     no inline event handlers and no style attributes (blocked by the CSP)
//   * no eval / new Function / document.write; innerHTML only in dom.js's
//     setTrustedHTML (server-rendered, escape-first documentation)
//   * every page module exports { title, mount }
//   * asset size budget
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// GX-Playground reuses this checker with its own root, page list and budget.
const root = process.env.GX_BUILD_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), '..', 'web');
const errors = [];
const fail = (file, msg) => errors.push(`${relative(root, file) || file}: ${msg}`);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

const files = walk(root);
const jsFiles = files.filter((f) => f.endsWith('.js'));
const exportsOf = new Map();

for (const file of jsFiles) {
  try {
    execFileSync(process.execPath, ['--check', file], { stdio: 'pipe' });
  } catch (err) {
    fail(file, `syntax error: ${err.stderr.toString().split('\n').slice(0, 4).join(' ')}`);
  }
  const src = readFileSync(file, 'utf8');
  const names = new Set();
  for (const m of src.matchAll(/export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z0-9_$]+)/g)) names.add(m[1]);
  for (const m of src.matchAll(/export\s*\{([^}]+)\}/g)) {
    for (const part of m[1].split(',')) names.add(part.trim().split(/\s+as\s+/).pop());
  }
  if (/export\s+default\b/.test(src)) names.add('default');
  exportsOf.set(file, names);
}

for (const file of jsFiles) {
  const src = readFileSync(file, 'utf8');
  const body = src.replace(/^import[\s\S]*?from\s+['"][^'"]+['"];?/gm, '');
  for (const m of src.matchAll(/^import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]/gm)) {
    const [, clause, spec] = m;
    if (!spec.startsWith('.')) { fail(file, `non-relative import '${spec}' (no runtime packages allowed)`); continue; }
    const target = resolve(dirname(file), spec);
    if (!existsSync(target)) { fail(file, `import '${spec}' does not resolve`); continue; }
    const available = exportsOf.get(target) || new Set();
    const used = [];
    const def = clause.match(/^([A-Za-z0-9_$]+)\s*(,|$)/);
    if (def) {
      used.push(def[1]);
      if (!available.has('default')) fail(file, `'${spec}' has no default export`);
    }
    const named = clause.match(/\{([^}]*)\}/);
    if (named) {
      for (const part of named[1].split(',').map((s) => s.trim()).filter(Boolean)) {
        const [orig, alias] = part.split(/\s+as\s+/);
        if (!available.has(orig)) fail(file, `'${orig}' is not exported by '${spec}'`);
        used.push(alias || orig);
      }
    }
    for (const name of used) {
      if (!new RegExp(`\\b${name.replace('$', '\\$')}\\b`).test(body)) fail(file, `unused import '${name}'`);
    }
  }
  if (/\beval\s*\(|new\s+Function\s*\(|document\.write\s*\(/.test(src)) fail(file, 'forbidden dynamic code (eval/Function/document.write)');
  const inner = [...src.matchAll(/\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(/g)];
  if (inner.length && !(file.endsWith('dom.js') && inner.length === 1)) fail(file, 'innerHTML/insertAdjacentHTML outside dom.setTrustedHTML');
  if (/setAttribute\(\s*['"]style['"]/.test(src)) fail(file, "setAttribute('style') is blocked by the CSP; use el.style");
  if (/\bh\([^)]*\bstyle:\s*['"`]/.test(src)) fail(file, 'string style attribute is blocked by the CSP');
  if (/sk-[A-Za-z0-9]{20,}|LITELLM_MASTER_KEY|GX_SWAP_API_KEY/.test(src)) fail(file, 'credential or credential name in frontend code');
  if (file.includes(`${join('js', 'pages')}`) && !file.endsWith('common.js')) {
    if (!/title:\s*['"]/.test(src) || !/\bmount\s*\(/.test(src)) fail(file, 'page module must export { title, mount }');
  }
}

const html = readFileSync(join(root, 'index.html'), 'utf8');
for (const m of html.matchAll(/(?:src|href)="([^"#][^"]*)"/g)) {
  const ref = m[1];
  if (/^https?:|^\/\//.test(ref)) { fail('index.html', `external reference ${ref} (blocked by CSP)`); continue; }
  if (!existsSync(join(root, ref.replace(/^\//, '')))) fail('index.html', `missing asset ${ref}`);
}
if (/<script(?![^>]*\bsrc=)[^>]*>/.test(html)) fail('index.html', 'inline <script> (blocked by CSP)');
if (/\son[a-z]+\s*=/.test(html)) fail('index.html', 'inline event handler (blocked by CSP)');
if (/\sstyle\s*=/.test(html)) fail('index.html', 'style attribute (blocked by CSP)');
if (!/<html lang="/.test(html)) fail('index.html', 'missing lang attribute');

const pages = (process.env.GX_BUILD_PAGES
  || 'dashboard,models,resources,storage,setup,runtime,cluster,jobs,logs,playground,docs,settings').split(',');
const navInHtml = process.env.GX_BUILD_NAV_IN_HTML !== '0';
for (const p of pages) {
  if (!existsSync(join(root, 'js', 'pages', `${p}.js`))) fail('js/pages', `missing page ${p}`);
  if (navInHtml && !html.includes(`data-page="${p}"`)) fail('index.html', `nav link for ${p} missing`);
}

const BUDGET = Number(process.env.GX_BUILD_BUDGET_KB || 400) * 1024;
const total = files.reduce((n, f) => n + statSync(f).size, 0);
if (total > BUDGET) fail('web/', `asset size ${total} B exceeds budget ${BUDGET} B`);

if (errors.length) {
  console.error(`build check FAILED (${errors.length}):`);
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}
console.log(`build check OK: ${jsFiles.length} modules, ${files.length} files, ${(total / 1024).toFixed(1)} KiB total (budget ${BUDGET / 1024} KiB)`);
