// Line icons (24x24, stroke = currentColor), built with createElementNS.
import { svg } from './dom.js';

const P = {
  home: ['M3 10.5 12 3l9 7.5', 'M5 9.5V21h5v-6h4v6h5V9.5'],
  image: ['M4 5h16v14H4z', 'M4 16l4.5-4.5 3.5 3.5 2.5-2.5L20 17', 'M15.5 9.5a1.5 1.5 0 1 0 0-.01'],
  video: ['M3 6h13v12H3z', 'M16 10l5-3v10l-5-3'],
  music: ['M9 18V5l11-2v13', 'M9 18a3 3 0 1 1-3-3 3 3 0 0 1 3 3z', 'M20 16a3 3 0 1 1-3-3 3 3 0 0 1 3 3z'],
  library: ['M4 4h6v6H4z', 'M14 4h6v6h-6z', 'M4 14h6v6H4z', 'M14 14h6v6h-6z'],
  history: ['M3 12a9 9 0 1 0 3-6.7', 'M3 4v5h5', 'M12 8v4l3 2'],
  plus: ['M12 5v14', 'M5 12h14'],
  brush: ['M18.5 3.5l2 2L11 15l-2-2z', 'M9 13l-1 1c-2 0-3 1.5-3 3.5 0 1-1 2-2 2.5h5.5A3.5 3.5 0 0 0 12 16.5V16'],
  eraser: ['M4 16l9-9 6 6-6 6H8z', 'M9 11l6 6', 'M13 19h7'],
  undo: ['M9 14L4 9l5-5', 'M4 9h11a5 5 0 0 1 0 10h-3'],
  invert: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 3v18', 'M12 7h4', 'M12 11h6', 'M12 15h5'],
  square: ['M5 5h14v14H5z'],
  search: ['M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14z', 'M20 20l-4-4'],
  sun: ['M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M12 2v2', 'M12 20v2', 'M4.9 4.9l1.4 1.4', 'M17.7 17.7l1.4 1.4', 'M2 12h2', 'M20 12h2', 'M4.9 19.1l1.4-1.4', 'M17.7 6.3l1.4-1.4'],
  moon: ['M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
  logout: ['M9 21H5V3h4', 'M16 17l5-5-5-5', 'M21 12H9'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v6H4V6h6'],
  activity: ['M3 12h4l3-8 4 16 3-8h4'],
  heart: ['M12 20s-7-4.4-9-9a4.8 4.8 0 0 1 9-3 4.8 4.8 0 0 1 9 3c-2 4.6-9 9-9 9z'],
  download: ['M12 4v11', 'M7 10l5 5 5-5', 'M4 20h16'],
  trash: ['M4 7h16', 'M9 7V4h6v3', 'M6 7l1 13h10l1-13'],
  edit: ['M4 20h4L19 9l-4-4L4 16z', 'M13.5 6.5l4 4'],
  shuffle: ['M3 7h4l10 10h4', 'M3 17h4l3-3', 'M14 10l3-3h4', 'M18 4l3 3-3 3', 'M18 14l3 3-3 3'],
  refresh: ['M20 11a8 8 0 0 0-14.5-4.5L4 8', 'M4 3v5h5', 'M4 13a8 8 0 0 0 14.5 4.5L20 16', 'M20 21v-5h-5'],
  copy: ['M8 8h12v12H8z', 'M16 8V4H4v12h4'],
  dice: ['M4 4h16v16H4z', 'M8.5 8.5h.01', 'M15.5 8.5h.01', 'M12 12h.01', 'M8.5 15.5h.01', 'M15.5 15.5h.01'],
  lock: ['M5 11h14v10H5z', 'M8 11V7a4 4 0 0 1 8 0v4'],
  unlock: ['M5 11h14v10H5z', 'M8 11V7a4 4 0 0 1 7.5-2'],
  compare: ['M12 3v18', 'M4 6h5v12H4z', 'M15 6h5v12h-5z'],
  expand: ['M4 9V4h5', 'M20 9V4h-5', 'M4 15v5h5', 'M20 15v5h-5'],
  film: ['M4 4h16v16H4z', 'M8 4v16', 'M16 4v16', 'M4 9h4', 'M4 15h4', 'M16 9h4', 'M16 15h4'],
  play: ['M7 4l13 8-13 8z'],
  pause: ['M7 4h3v16H7z', 'M14 4h3v16h-3z'],
  x: ['M6 6l12 12', 'M18 6L6 18'],
  check: ['M4 12.5l5 5L20 6.5'],
  chevronRight: ['M9 5l7 7-7 7'],
  chevronLeft: ['M15 5l-7 7 7 7'],
  chevronDown: ['M5 9l7 7 7-7'],
  upload: ['M12 20V9', 'M7 14l5-5 5 5', 'M4 4h16'],
  info: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 11v6', 'M12 7.5h.01'],
  layers: ['M12 3l9 5-9 5-9-5z', 'M3 13l9 5 9-5'],
  grid: ['M4 4h7v7H4z', 'M13 4h7v7h-7z', 'M4 13h7v7H4z', 'M13 13h7v7h-7z'],
  list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3.5 6h.01', 'M3.5 12h.01', 'M3.5 18h.01'],
  sliders: ['M4 6h10', 'M18 6h2', 'M4 12h4', 'M12 12h8', 'M4 18h12', 'M20 18h0', 'M16 4v4', 'M10 10v4', 'M18 16v4'],
  sparkles: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z'],
  wand: ['M4 20L16 8', 'M14 6l4 4', 'M19 3v3', 'M17.5 4.5h3', 'M9 3v2', 'M8 4h2'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7v5l3 2'],
  alert: ['M12 3l10 18H2z', 'M12 10v5', 'M12 18h.01'],
  scissors: ['M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M8.2 7.8L20 19', 'M8.2 16.2L20 5'],
  extend: ['M4 12h12', 'M12 7l5 5-5 5', 'M20 5v14'],
  tag: ['M3 12V3h9l9 9-9 9z', 'M7.5 7.5h.01'],
  user: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21a8 8 0 0 1 16 0'],
  command: ['M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z'],
  cpu: ['M6 6h12v12H6z', 'M9 9h6v6H9z', 'M9 2v4', 'M15 2v4', 'M9 18v4', 'M15 18v4', 'M2 9h4', 'M2 15h4', 'M18 9h4', 'M18 15h4'],
  wave: ['M2 12h2', 'M6 8v8', 'M10 4v16', 'M14 7v10', 'M18 10v4', 'M22 12h-2'],
  remix: ['M4 7h11a4 4 0 0 1 0 8H9', 'M7 4L4 7l3 3', 'M12 12l-3 3 3 3'],
  menu: ['M4 6h16', 'M4 12h16', 'M4 18h16'],
  folder: ['M3 6h6l2 2h10v11H3z'],
  flow: ['M5 4h5v5H5z', 'M14 15h5v5h-5z', 'M7.5 9v3.5a2.5 2.5 0 0 0 2.5 2.5h4'],
  mic: ['M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z', 'M19 11a7 7 0 0 1-14 0', 'M12 18v3'],
  camera: ['M3 7h4l2-3h6l2 3h4v13H3z', 'M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z'],
  phone: ['M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A17 17 0 0 1 3 5a2 2 0 0 1 2-2z'],
  logs: ['M6 3h9l4 4v14H6z', 'M14 3v5h5', 'M9 12h7', 'M9 16h7'],
  settings: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'],
  shield: ['M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z', 'M9 12l2 2 4-4'],
};

export function icon(name, { size = 20, cls = '', label } = {}) {
  const el = svg('svg', {
    viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor',
    'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    class: `ic ${cls}`.trim(), 'aria-hidden': label ? undefined : 'true', role: label ? 'img' : undefined,
    'aria-label': label, focusable: 'false',
  }, (P[name] || P.info).map((d) => svg('path', { d })));
  return el;
}

// Fills every [data-icon] placeholder in static markup.
export function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) {
    if (el.firstChild) continue;
    el.append(icon(el.dataset.icon, { size: Number(el.dataset.size || 20) }));
  }
}
