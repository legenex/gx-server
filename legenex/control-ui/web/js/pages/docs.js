import { api } from '../api.js';
import { h, clear, setTrustedHTML, copyButton, errorBox, spinner } from '../dom.js';

let root;
let pages = [];
let contentEl;
let tocEl;

function enhance(container) {
  for (const pre of container.querySelectorAll('pre.code')) {
    const wrap = h('div', { class: 'code-wrap' });
    pre.replaceWith(wrap);
    wrap.append(copyButton(() => pre.textContent), pre);
    pre.tabIndex = 0;
  }
  for (const a of container.querySelectorAll('a[href^="#d-"]')) {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      const target = container.querySelector(a.getAttribute('href'));
      if (target) { target.scrollIntoView({ block: 'start' }); target.tabIndex = -1; target.focus({ preventScroll: true }); }
    });
  }
  for (const wrap of container.querySelectorAll('.table-wrap')) {
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', 'table');
  }
}

async function openPage(slug, anchor) {
  clear(contentEl).append(spinner());
  try {
    const page = await api.get(`/api/docs/${encodeURIComponent(slug)}`);
    const article = h('article', { class: 'doc', 'aria-labelledby': 'doc-title' });
    setTrustedHTML(article, page.html);
    const h1 = article.querySelector('h1');
    if (h1) h1.id = 'doc-title';
    enhance(article);
    clear(contentEl).append(article);
    for (const a of tocEl.querySelectorAll('a[data-slug]')) {
      if (a.dataset.slug === slug && !a.dataset.anchor) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    if (anchor) {
      const target = article.querySelector(`#${CSS.escape(anchor)}`);
      if (target) target.scrollIntoView({ block: 'start' });
    } else {
      contentEl.scrollTop = 0;
      window.scrollTo(0, 0);
    }
  } catch (err) {
    clear(contentEl).append(errorBox(err));
  }
}

function buildToc() {
  clear(tocEl);
  const search = h('input', { type: 'search', id: 'doc-search', placeholder: 'Search the docs', 'aria-label': 'Search the documentation' });
  const results = h('div', { class: 'doc-results', 'aria-live': 'polite' });
  let t;
  search.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = search.value.trim();
      clear(results);
      if (q.length < 2) return;
      const data = await api.get(`/api/docs?q=${encodeURIComponent(q)}`);
      results.append(h('p', { class: 'muted small' }, `${data.results.length} match(es)`),
        h('ul', {}, data.results.map((r) => h('li', {},
          h('a', { href: `#/docs/${r.slug}/${r.anchor}` }, `${r.page} › ${r.section}`),
          h('div', { class: 'muted small' }, r.snippet)))));
    }, 250);
  });
  tocEl.append(search, results);
  const list = h('ul', { class: 'doc-toc' });
  for (const p of pages) {
    list.append(h('li', {},
      h('a', { href: `#/docs/${p.slug}`, 'data-slug': p.slug, class: 'toc-page' }, p.title),
      p.sections.length ? h('ul', {}, p.sections.map((s) => h('li', {},
        h('a', { href: `#/docs/${p.slug}/${s.id}`, 'data-slug': p.slug, 'data-anchor': s.id }, s.title)))) : null));
  }
  tocEl.append(h('nav', { 'aria-label': 'Documentation contents' }, list));
}

export default {
  title: 'Docs',
  interval: 0,
  async mount(el, { params }) {
    root = el;
    clear(root).append(spinner());
    const data = await api.get('/api/docs');
    pages = data.pages;
    tocEl = h('aside', { class: 'doc-sidebar' });
    contentEl = h('div', { class: 'doc-content' });
    clear(root).append(h('div', { class: 'docs-layout' }, tocEl, contentEl));
    buildToc();
    const slug = params && params[0] && pages.some((p) => p.slug === params[0]) ? params[0] : (pages[0] || {}).slug;
    if (slug) await openPage(slug, params && params[1]);
  },
};
