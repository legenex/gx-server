// Creative work moved to GX-Playground (D-037). The Control Center keeps the
// infrastructure side only; old #/create and #/library links land here.
import { api } from '../api.js';
import { h, clear, card } from '../dom.js';

export default {
  title: 'GX-Playground',
  interval: 0,
  async mount(el) {
    const setup = await api.get('/api/setup');
    const url = setup.playground_url;
    clear(el).append(card('Images, video and music live in GX-Playground',
      h('p', { class: 'lead' }, 'Generation, editing, the music studio and the Library (history, lineage, downloads) are in GX-Playground. '
        + 'Your Control Center sign-in works there too.'),
      h('p', {}, h('a', { class: 'btn btn-primary btn-lg', href: url, target: '_blank', rel: 'noopener', id: 'open-playground' },
        'Open GX-Playground ↗')),
      h('p', { class: 'small muted' }, 'The Control Center keeps the admin side: Resource Control, Storage & Cleanup, Model Manager, API Keys and Setup.')));
  },
};
