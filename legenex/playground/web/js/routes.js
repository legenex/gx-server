// Page registry: route name -> lazy module loader (D-040).
// Each feature adds ONE line here, one nav link in index.html, and its name to
// SPA_ROUTE (gx_playground/server.py) and GX_BUILD_PAGES (scripts/build-check.mjs).
export const PAGE_LOADERS = {
  dashboard: () => import('./pages/dashboard.js'),
  images: () => import('./pages/images.js'),
  video: () => import('./pages/video.js'),
  music: () => import('./pages/music.js'),
  library: () => import('./pages/library.js'),
  history: () => import('./pages/history.js'),
};

export function hasPage(name) {
  return Object.prototype.hasOwnProperty.call(PAGE_LOADERS, name);
}

export async function loadPage(name) {
  const mod = await PAGE_LOADERS[name]();
  return mod.default;
}
