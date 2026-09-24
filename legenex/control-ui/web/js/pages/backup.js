import { api } from '../api.js';
import { h, clear, card, kv, table, errorBox, levelBadge } from '../dom.js';

let root;

function render(d) {
  const snaps = d.snapshots || [];
  const rows = snaps.map((s) => [
    s.short_id || (s.id || '').slice(0, 8),
    s.hostname || '',
    (s.time || '').replace('T', ' ').slice(0, 19),
    (s.paths || []).slice(0, 2).join(', '),
  ]);
  const grid = h('div', { class: 'grid' });
  const drill = d.restore_drill || {};
  const integ = d.integrity || {};
  const snap = d.latest_snapshot || {};
  grid.append(card('Backup health',
    kv([
      ['Last backup', d.latest || 'none yet'],
      ['Latest snapshot', snap.short_id || (snap.id || '').slice(0, 8) || '—'],
      ['Snapshot count', String(d.snapshot_count ?? (d.snapshots || []).length)],
      ['Backup age', d.latest_time ? (d.latest_time.replace('T', ' ').slice(0, 19)) : '—'],
      ['Job', d.running ? 'running' : 'idle'],
      ['Local repository', d.repo],
      ['Local repo health', integ.ok ? 'healthy' : (integ.ok === false ? 'failed' : 'not verified this session')],
      ['Last Restic integrity verification', integ.at || 'never recorded'],
      ['Cross-node copy', d.peer],
      ['GitHub recovery recipe', h('a', { href: d.github, target: '_blank', rel: 'noopener' }, d.github)],
      ['Last successful restore drill', drill.at || 'never recorded'],
      ['Recovery tested', d.recovery_tested ? 'yes' : 'no — run a restore drill'],
      ['Offsite backup', 'Not configured'],
    ]),
    h('div', { class: 'btn-row' },
      h('button', { class: 'btn btn-primary', id: 'bak-now' }, 'Back Up Now'),
      h('button', { class: 'btn', id: 'bak-verify' }, 'Verify Backup'),
      h('a', { class: 'btn btn-ghost', href: 'https://github.com/legenex/gx-backup', target: '_blank', rel: 'noopener' }, 'Open GitHub'),
    ),
    h('p', { class: 'muted small', id: 'bak-msg' }, ''),
  ));
  grid.append(card('Restore points', table(['ID', 'Host', 'Time', 'Paths'], rows.length ? rows : [['—', '', '', 'no snapshots']])));
  grid.append(card('What is covered',
    h('p', {}, d.coverage || 'Encrypted restic snapshots hold projects, configs, databases, OpenWebUI data, AgentOS, and secrets. Public model weights are recreated from models.lock.'),
    h('p', { class: 'callout callout-warning' }, d.offsite_note || 'Offsite backup is not configured. Cross-node copies live in the same house.'),
    h('p', {}, 'Destructive restore from this page requires typing RESTORE on the command line; the UI only starts backup and verify.'),
    h('p', {}, 'Guides: docs/DISASTER-RECOVERY.md and docs/GX-BACKUP-RESTORE-GUIDE.pdf in the gx-backup repository.'),
  ));
  clear(root).append(grid);
  root.querySelector('#bak-now').addEventListener('click', async () => {
    const msg = root.querySelector('#bak-msg');
    msg.textContent = 'Starting backup…';
    try {
      await api.post('/api/backup/now', {});
      msg.textContent = 'Backup started. Refresh this page in a few minutes.';
    } catch (err) {
      msg.textContent = err.message;
    }
  });
  root.querySelector('#bak-verify').addEventListener('click', async () => {
    const msg = root.querySelector('#bak-msg');
    msg.textContent = 'Verifying…';
    try {
      const r = await api.post('/api/backup/verify', {});
      msg.textContent = r.ok ? 'Integrity check passed.' : (r.output || 'verify failed');
    } catch (err) {
      msg.textContent = err.message;
    }
  });
}

export default {
  title: 'Backup & Recovery',
  interval: 15,
  mount(el) { root = el; },
  async refresh() {
    try {
      render(await api.get('/api/backup/status'));
    } catch (err) {
      clear(root).append(errorBox(err));
    }
  },
};
