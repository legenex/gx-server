// Dashboard: quick create, studio status, resources, activity, recent work.
import { api } from '../api.js';
import { miniWave } from '../audio.js';
import { OP_LABEL, TYPE_LABEL } from '../assets.js';
import { ago, bytes, clear, h, mmss, plural, titleOf, toast, replace } from '../dom.js';
import { icon } from '../icons.js';
import { center, friendlyError, isTerminal, jobCard, submitMedia, submitMusic } from '../jobs.js';
import { href, navigate, openAsset } from '../nav.js';
import { ALLOWED_PROFILES, statusInfo, switchProfile } from '../resources.js';
import { assetThumb, button, callout, card, chips, composer, emptyState, skeletonGrid, skeletonLines, statusDot } from '../ui.js';

const STUDIOS = [
  ['image', 'Images', 'gx-image', 'image', 'Text to image, edits and variations'],
  ['video', 'Video', 'gx-video', 'video', 'Text to video, image to video, video edits'],
  ['music', 'Music', 'gx-music', 'music', 'Songs, remixes, repaint and extend'],
];

function greeting(user) {
  const hr = new Date().getHours();
  const part = hr < 5 ? 'Good night' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
  return `${part}, ${user}`;
}

function recentTile(asset) {
  const media = asset.type === 'audio'
    ? h('div', { class: 'tile-audio' }, icon('music', { size: 18 }), miniWave(asset.waveform))
    : assetThumb(asset);
  return h('li', { class: 'tile' },
    h('button', { type: 'button', class: 'tile-btn', 'aria-label': `Open ${titleOf(asset)} (${TYPE_LABEL[asset.type]})`, onclick: () => openAsset(asset) },
      h('div', { class: `tile-media tile-${asset.type}` }, media,
        asset.type === 'video' ? h('span', { class: 'tile-play', 'aria-hidden': 'true' }, icon('play', { size: 16 })) : null,
        asset.favourite ? h('span', { class: 'tile-fav', 'aria-hidden': 'true' }, icon('heart', { size: 14, cls: 'ic-fill' })) : null),
      h('span', { class: 'tile-caption' },
        h('span', { class: 'tile-title' }, titleOf(asset)),
        h('span', { class: 'tile-sub' }, `${TYPE_LABEL[asset.type]} · ${asset.type === 'audio' && asset.duration ? mmss(asset.duration) : (OP_LABEL[asset.operation] || '')} · ${ago(asset.created_at)}`))));
}

function profileSelector(summary, onChanged) {
  const group = h('div', { class: 'profile-select', id: 'profile-select', role: 'radiogroup', 'aria-label': 'Resource profile' });
  const profiles = (summary.profiles || []).filter((p) => ALLOWED_PROFILES.includes(p.id));
  const radios = profiles.map((p) => {
    const on = p.id === summary.profile;
    const b = h('button', {
      type: 'button', role: 'radio', class: 'profile-opt', 'aria-checked': String(on), tabindex: on ? '0' : '-1',
      dataset: { profile: p.id }, title: p.summary,
    }, h('span', { class: 'profile-name' }, p.label));
    b.addEventListener('click', async () => {
      if (p.id === summary.profile) return;
      for (const r of radios) r.disabled = true;
      const ok = await switchProfile(p.id);
      for (const r of radios) r.disabled = false;
      if (ok) onChanged();
    });
    return b;
  });
  if (!radios.some((r) => r.tabIndex === 0) && radios[0]) radios[0].tabIndex = 0;
  group.addEventListener('keydown', (ev) => {
    const i = radios.indexOf(document.activeElement);
    if (i < 0) return;
    let n = null;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') n = (i + 1) % radios.length;
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') n = (i - 1 + radios.length) % radios.length;
    if (n === null) return;
    ev.preventDefault();
    radios[n].focus();
  });
  group.append(...radios);
  const current = profiles.find((p) => p.id === summary.profile);
  return h('div', { class: 'stack-sm' }, group,
    current ? h('p', { class: 'muted small' }, current.summary) : null);
}

function resourceCard(summary, onChanged) {
  const rows = h('ul', { class: 'res-rows' }, (summary.rows || []).map((r) => {
    const s = statusInfo(r.status);
    return h('li', { class: 'res-row', dataset: { key: r.key } },
      h('span', { class: 'res-row-label' }, r.label),
      h('span', { class: `status status-${s.tone}` }, statusDot(s.tone), s.words));
  }));
  return card('Resources', h('div', { class: 'stack' },
    h('p', { class: 'res-active' }, 'Active profile: ', h('strong', { id: 'active-profile' }, summary.maintenance ? 'Maintenance' : summary.profile_label)),
    summary.maintenance ? callout('warn', 'Maintenance mode', 'An administrator paused the cluster. New jobs wait until maintenance ends.') : null,
    rows,
    h('p', { class: 'muted small', id: 'queued-count' }, `${plural(summary.queued || 0, 'job')} queued`),
    summary.maintenance ? null : profileSelector(summary, onChanged),
    /^https?:\/\//.test(summary.control_center_url || '') ? h('a', {
      class: 'link-ext', href: summary.control_center_url, target: '_blank', rel: 'noopener',
    }, 'Open Advanced Resource Controls', icon('external', { size: 14 })) : null), { cls: 'card-resources' });
}

function studioCard(summary) {
  const byKey = Object.fromEntries((summary.rows || []).map((r) => [r.key, r]));
  return card('Studios', h('ul', { class: 'studio-list' }, STUDIOS.map(([key, label, alias, ic]) => {
    const r = byKey[key] || { status: 'Unavailable', detail: '' };
    const s = statusInfo(r.status);
    return h('li', { class: 'studio-row' },
      h('span', { class: `studio-ic studio-${key}`, 'aria-hidden': 'true' }, icon(ic, { size: 18 })),
      h('div', { class: 'studio-text' }, h('p', { class: 'studio-name' }, label, h('span', { class: 'muted small' }, ` · ${alias}`)),
        h('p', { class: 'muted small' }, r.detail || (s.tone === 'ok' ? 'Ready for your next idea' : ''))),
      h('span', { class: `status status-${s.tone}` }, statusDot(s.tone), s.words));
  })));
}

function capacityCard(capacity, counts) {
  const total = Object.values(capacity || {}).reduce((n, c) => n + (c.bytes || 0), 0) || 1;
  return card('Library', h('div', { class: 'stack' },
    h('ul', { class: 'cap-list' }, ['image', 'video', 'audio'].map((t) => {
      const c = (capacity || {})[t] || { count: counts[t] || 0, bytes: 0 };
      const bar = h('div', { class: `cap-bar cap-${t}`, 'aria-hidden': 'true' }, h('span', {}));
      bar.firstChild.style.width = `${Math.max(2, Math.round(((c.bytes || 0) / total) * 100))}%`;
      return h('li', { class: 'cap-row' },
        h('div', { class: 'row-between' }, h('span', {}, `${TYPE_LABEL[t]}s`), h('span', { class: 'muted small' }, `${c.count} · ${bytes(c.bytes)}`)),
        bar);
    })),
    h('a', { class: 'link', href: href('library') }, 'Browse the Library')));
}

function errorsCard(errors, musicError) {
  const items = (errors || []).map((e) => h('li', { class: 'err-row' },
    icon('alert', { size: 16 }),
    h('div', {}, h('p', { class: 'err-kind' }, e.kind || 'Job'), h('p', { class: 'muted small' }, friendlyError(e.message).text),
      e.at ? h('p', { class: 'muted xsmall' }, ago(e.at)) : null)));
  return card('Recent problems', h('div', { class: 'stack-sm' },
    musicError ? callout('warn', 'Music is not reachable', friendlyError(musicError).text) : null,
    items.length ? h('ul', { class: 'err-list' }, items) : h('p', { class: 'muted' }, 'No failed jobs recently.')));
}

// Everything beyond the three studios, built from the navigation itself so
// only pages that exist are offered (other workstreams add theirs there).
const EXPLORE_SUB = {
  flows: 'Chain models into reusable pipelines', voice: 'Speech, voice design and cloning',
  live: 'Talk with a model using camera and microphone', call: 'Voice agents for calls',
  library: 'Everything you have made', history: 'Every job and why it waits',
  models: 'What each model does and whether it is ready', logs: 'Your activity and errors',
  settings: 'Preferences, HTTPS and API access',
};

function exploreCard() {
  const groups = [...document.querySelectorAll('.rail-group')].filter((g) => !g.hidden).map((g) => {
    const label = (g.querySelector('.rail-group-label') || {}).textContent || '';
    const links = [...g.querySelectorAll('.rail-link')]
      .filter((a) => !['dashboard', 'images', 'video', 'music'].includes(a.dataset.page))
      .map((a) => {
        const name = a.dataset.page;
        const ic = (a.querySelector('.rail-ic') || {}).dataset;
        return h('li', {}, h('a', { class: 'explore-link', href: a.getAttribute('href') },
          icon(ic && ic.icon ? ic.icon : 'chevronRight', { size: 18 }),
          h('span', {}, (a.querySelector('.rail-label') || a).textContent,
            EXPLORE_SUB[name] ? h('span', { class: 'explore-sub' }, EXPLORE_SUB[name]) : null)));
      });
    return links.length ? h('div', {}, h('h3', { class: 'explore-title' }, label), h('ul', { class: 'explore-list' }, links)) : null;
  }).filter(Boolean);
  const realtime = document.querySelector('#nav-realtime .rail-link');
  const insecure = realtime && !window.isSecureContext
    ? callout('info', 'Live and Call Agents need a secure connection', 'The microphone and camera only work over HTTPS here.',
      [h('a', { class: 'link', href: href('settings', { focus: 'https' }) }, 'Set up HTTPS')])
    : null;
  return card('Explore', h('div', { class: 'stack' }, insecure, h('div', { class: 'explore-groups' }, groups)),
    { sub: 'Realtime, models, logs and settings' });
}

function quickCreate(user) {
  let kind = 'image';
  const box = composer({ label: 'Describe what you want to create', placeholder: 'A lighthouse on a cliff at golden hour, cinematic…', maxLength: 2000, rows: 3, onSubmit: () => go() });
  box.classList.add('composer-hero');
  const kinds = chips([['image', 'Image'], ['video', 'Video'], ['music', 'Music']], { value: kind, label: 'What to create', onChange: (v) => { kind = v; } });
  const submit = button('Create', { icon: 'sparkles', variant: 'primary', onClick: () => go() });
  async function go() {
    const prompt = box.get();
    if (!prompt) { box.textarea.focus(); toast('Describe what you want to create first.', 'warn'); return; }
    submit.disabled = true;
    try {
      if (kind === 'music') await submitMusic({ operation: 'generate', prompt });
      else await submitMedia({ kind: kind === 'video' ? 't2v' : 't2i', prompt });
      toast('Started. Opening the workspace…', 'ok');
      navigate(kind === 'image' ? 'images' : kind);
    } catch (err) {
      toast(friendlyError(err.message).text, 'danger');
    } finally {
      submit.disabled = false;
    }
  }
  const quickCards = h('ul', { class: 'quick-cards' }, STUDIOS.map(([key, label, , ic, text]) => h('li', {},
    h('a', { class: `quick-card quick-${key}`, href: href(key === 'image' ? 'images' : key, { focus: 1 }) },
      h('span', { class: 'quick-ic', 'aria-hidden': 'true' }, icon(ic, { size: 22 })),
      h('span', { class: 'quick-text' }, h('span', { class: 'quick-title' }, `New ${label === 'Images' ? 'image' : label.toLowerCase()}`), h('span', { class: 'quick-sub' }, text)),
      icon('chevronRight', { size: 18, cls: 'quick-go' })))));
  return h('section', { class: 'hero', 'aria-labelledby': 'dash-title' },
    h('div', { class: 'hero-text' },
      h('p', { class: 'eyebrow' }, greeting(user)),
      h('h1', { class: 'hero-title', id: 'dash-title', tabindex: '-1' }, 'What will you create today?')),
    h('div', { class: 'hero-composer' }, box, h('div', { class: 'hero-actions' }, kinds, submit)),
    quickCards);
}

export default {
  title: 'Dashboard',
  async mount(root, ctx) {
    let alive = true;
    let timer = null;
    const side = h('div', { class: 'dash-side' }, card('Resources', skeletonLines(5)));
    const activeBox = h('div', { class: 'stack-sm', id: 'active-jobs' }, skeletonLines(2));
    const recentBox = h('div', { id: 'recent' }, skeletonGrid(6, 'tile-grid'));
    replace(root,
      quickCreate(ctx.user),
      h('div', { class: 'dash-grid' },
        h('div', { class: 'dash-main' },
          card('In progress', activeBox, { sub: 'Jobs update live. Waiting jobs explain what they wait for.', actions: h('a', { class: 'link', href: href('history') }, 'All activity') }),
          card('Recent creations', recentBox, { actions: h('a', { class: 'link', href: href('library') }, 'Open Library') }),
          exploreCard()),
        side));
    const shown = new Map();
    const renderActive = () => {
      const active = center.active();
      for (const [id, el] of shown) {
        if (!active.find((j) => j.id === id) && !el.isConnected) shown.delete(id);
      }
      if (!active.length && ![...shown.values()].some((el) => el.isConnected)) {
        replace(activeBox, emptyState({ icon: 'sparkles', title: 'Nothing running', text: 'Start something above; progress shows up here.' }));
        return;
      }
      if (activeBox.querySelector('.empty, .skeleton')) clear(activeBox);
      for (const j of active) {
        if (!shown.has(j.id)) {
          const el = jobCard(j, { compact: true, onOpen: () => navigate('library') });
          shown.set(j.id, el);
          activeBox.prepend(el);
        }
      }
    };
    const load = async () => {
      try {
        const ov = await api.get('/api/creative/overview');
        if (!alive) return;
        for (const j of ov.active || []) if (!center.jobs.has(j.id) && !isTerminal(j)) center.track(j);
        renderActive();
        replace(recentBox, (ov.recent || []).length
          ? h('ul', { class: 'tile-grid' }, ov.recent.map(recentTile))
          : emptyState({ icon: 'image', title: 'No creations yet', text: 'Your images, videos and tracks will appear here.' }));
        replace(side,
          studioCard(ov.resources),
          resourceCard(ov.resources, () => load()),
          capacityCard(ov.capacity, ov.counts || {}),
          errorsCard(ov.errors, ov.music_error));
      } catch (err) {
        if (!alive || err.status === 401) return;
        replace(side, callout('danger', 'Could not load the overview', err.message));
      }
    };
    await load();
    const unsub = center.subscribe(() => { if (alive) renderActive(); });
    const tick = () => {
      timer = setTimeout(async () => {
        if (!document.hidden && !document.querySelector('dialog[open]')) await load();
        if (alive) tick();
      }, 12000);
    };
    tick();
    if (ctx.query.focus) root.querySelector('.composer textarea').focus();
    return () => { alive = false; clearTimeout(timer); unsub(); };
  },
};
