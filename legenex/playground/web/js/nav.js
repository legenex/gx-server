// Hash routing helpers: #/page?key=value
export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  const [page, ...rest] = path.split('/').filter(Boolean);
  return { page: page || '', rest, query: Object.fromEntries(new URLSearchParams(query)) };
}

export function href(page, query = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  const s = q.toString();
  return `#/${page}${s ? `?${s}` : ''}`;
}

export function navigate(page, query = {}) {
  const target = href(page, query);
  if (location.hash === target) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = target;
}

// Where an asset opens for editing.
export function workspaceFor(asset) {
  if (asset.type === 'audio' && asset.source_kind && asset.source_kind.startsWith('voice_')) return 'voice';
  return asset.type === 'audio' ? 'music' : asset.type === 'video' ? 'video' : 'images';
}

export function openAsset(asset) {
  navigate(workspaceFor(asset), { asset: asset.id });
}
