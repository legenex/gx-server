// Creative Flows page: the vanilla wrapper around the React island.
//
// The editor itself is a React + TypeScript + @xyflow/react bundle built by
// Vite into web/flows (source in ../../../flows-ui). This module keeps the
// Playground contract - default export { title, mount } - loads the bundle on
// demand and hands it a host object, so the island never touches the session
// cookie, the CSRF token or the router itself.
import { api, request, upload } from '../api.js';
import { detailsDrawer, pickAsset } from '../assets.js';
import { confirmDialog, h, replace, toast } from '../dom.js';
import { href } from '../nav.js';
import { callout, loading } from '../ui.js';

const BUNDLE_DIR = '/flows';
let modulePromise = null;

// The built file names carry a content hash, so the manifest is the only place
// that knows them. It is fetched once per page load and cached in this module.
async function loadIsland() {
  const res = await fetch(`${BUNDLE_DIR}/manifest.json`, { credentials: 'same-origin', cache: 'no-cache' });
  if (!res.ok) throw new Error(`The Creative Flows bundle is not deployed (manifest HTTP ${res.status}).`);
  const manifest = await res.json();
  const records = Object.values(manifest);
  const entry = records.find((r) => r && r.isEntry);
  if (!entry || typeof entry.file !== 'string') throw new Error('The Creative Flows bundle has no entry point.');
  const sheets = [
    ...(Array.isArray(entry.css) ? entry.css : []),
    ...records.map((r) => r && r.file).filter((f) => typeof f === 'string' && f.endsWith('.css')),
  ];
  for (const file of new Set(sheets)) {
    const url = `${BUNDLE_DIR}/${file}`;
    if (document.head.querySelector(`link[href="${url}"]`)) continue;
    const link = h('link', { rel: 'stylesheet', href: url });
    document.head.append(link);
  }
  const mod = await import(`${BUNDLE_DIR}/${entry.file}`);
  if (typeof mod.mount !== 'function') throw new Error('The Creative Flows bundle exports no mount().');
  return mod;
}

function island() {
  modulePromise ??= loadIsland().catch((err) => {
    modulePromise = null;      // a reload or a redeploy can still succeed
    throw err;
  });
  return modulePromise;
}

// Everything the island is allowed to do to the Playground shell.
function makeHost(ctx) {
  let query = { ...ctx.query };
  return {
    user: ctx.user,
    get query() { return query; },
    setQuery(next) {
      query = { ...next };
      // Replace the hash in place: re-routing would unmount the editor.
      history.replaceState(null, '', href('flows', query));
    },
    request: (method, path, body, opts = {}) => (method === 'GET'
      ? api.get(path, opts)
      : request(method, path, body, opts)),
    toast: (message, tone = 'ok') => { toast(message, tone); },
    pickAsset: (opts) => pickAsset({ type: opts.type, title: opts.title || 'Choose from Library' }),
    upload: (file, opts = {}) => upload('/api/media/upload', file,
      { title: opts.title || file.name, filename: file.name }),
    showAsset: (assetId) => {
      detailsDrawer(assetId).catch((err) => { toast(err.message, 'danger'); });
    },
    confirm: (opts) => confirmDialog(opts),
  };
}

export default {
  title: 'Creative Flows',
  async mount(root, ctx) {
    replace(root, loading('Loading the flow editor…'));
    let mod;
    try {
      mod = await island();
    } catch (err) {
      replace(root, callout('danger', 'The flow editor could not be loaded', err.message
        || 'Reload the page. If it keeps failing, the bundle in web/flows is missing or broken.'));
      return undefined;
    }
    const container = h('div', { class: 'gxf-root' });
    replace(root, container);
    let unmount;
    try {
      unmount = mod.mount(container, makeHost(ctx));
    } catch (err) {
      replace(root, callout('danger', 'The flow editor could not start', err.message || String(err)));
      return undefined;
    }
    return () => {
      try { unmount(); } catch { /* already gone */ }
    };
  },
};
