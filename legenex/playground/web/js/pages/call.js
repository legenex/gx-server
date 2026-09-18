// Call Agents (gx-call, NemotronLabs VoiceChat 11B): build a versioned phone
// agent, call it live from the browser over the realtime tunnel, watch the
// transcript and the authoritative intake fill up, transfer or end the call,
// and review every past call with its result and recording.
//
// The microphone needs a secure context (plt.md section 2), so the Call tab
// renders the shared secureContextCallout() when there is none. The audio
// worklets under js/realtime/ are the shared realtime code (LIV).
import { api, getAsset } from '../api.js';
import { audioPlayer } from '../audio.js';
import { ago, clear, confirmDialog, dateTime, h, openDialog, promptDialog, replace, toast, truncate, uid } from '../dom.js';
import { friendlyError } from '../jobs.js';
import { navigate } from '../nav.js';
import { openRealtime, secureContextCallout, secureContextInfo } from '../realtime.js';
import {
  badge, button, callout, card, chips, emptyState, field, iconButton, kv, loading, pageHeader, progressBar,
  select, skeletonLines, tabs, textInput, toggle,
} from '../ui.js';

const WIRE_IN = 16000; // caller audio the engine expects (PCM16 mono)
const WIRE_OUT = 22050; // agent audio the engine produces
const FRAME_MS = 80;
const TABS = [['agents', 'Agents', 'phone'], ['call', 'Call', 'mic'], ['history', 'History', 'clock']];
const STATUS_TONE = { enabled: 'ok', draft: 'info', disabled: 'warn', archived: 'neutral' };
const DISPOSITION_TONE = {
  completed: 'ok', transferred: 'ok', intake_complete: 'ok', abandoned: 'warn', timeout: 'warn',
  failed: 'danger', preempted: 'danger', dropped: 'warn',
};
const LONG_FIELDS = [
  ['system_instructions', 'Role and task', 'What the agent is and what the call is for.'],
  ['personality', 'Personality', 'Tone of voice, pace, manner.'],
  ['opening_greeting', 'Opening line', 'The first thing the caller hears.'],
  ['qualification_flow', 'Call flow', 'The order the agent works through the call.'],
  ['objection_handling', 'Objection handling', ''],
  ['conversation_rules', 'Conversation rules', ''],
  ['prohibited_behaviour', 'Never do this', ''],
  ['transfer_rules', 'When to transfer', ''],
  ['business_hours_behaviour', 'Outside business hours', ''],
  ['voicemail_behaviour', 'Voicemail', ''],
  ['fallback_behaviour', 'When stuck', ''],
  ['knowledge', 'Knowledge the agent may use', 'Facts the agent is allowed to state.'],
];

const session = { tab: 'agents' };

function tone(map, value, fallback = 'neutral') {
  return map[value] || fallback;
}

function agentStatusBadges(agent) {
  return [
    badge(agent.status, tone(STATUS_TONE, agent.status)),
    badge(agent.mode === 'production' ? 'production' : 'test', agent.mode === 'production' ? 'warn' : 'neutral'),
    badge(`v${agent.current_version || agent.version || 1}`, 'neutral'),
  ];
}

function errorText(err) {
  return friendlyError(err && err.message ? err.message : String(err)).text;
}

function fail(err) {
  toast(errorText(err), 'danger');
}

function schemaFieldNames(cfg) {
  const props = (cfg.structured_output_schema && cfg.structured_output_schema.properties) || {};
  return Object.keys(props).filter((k) => !['disposition', 'qualification_status', 'transfer_status'].includes(k));
}

function textArea(value, { rows = 3, maxLength } = {}) {
  return h('textarea', { class: 'input', rows: String(rows), maxlength: maxLength ? String(maxLength) : undefined,
    spellcheck: 'true' }, String(value || ''));
}

// ---------------------------------------------------------------- live call
// One browser call: microphone -> capture worklet -> WebSocket -> gx-call,
// and agent audio -> playback worklet. Everything is torn down by stop().
class LiveCall {
  constructor(created, handlers) {
    this.created = created;
    this.on = handlers;
    this.ws = null;
    this.ctx = null;
    this.stream = null;
    this.capture = null;
    this.playback = null;
    this.source = null;
    this.muted = false;
    this.stopped = false;
    this.state = 'connecting';
  }

  async open() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    await this.ctx.audioWorklet.addModule('/js/realtime/capture-worklet.js');
    await this.ctx.audioWorklet.addModule('/js/realtime/playback-worklet.js');
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.capture = new AudioWorkletNode(this.ctx, 'gx-capture', {
      numberOfInputs: 1, numberOfOutputs: 0,
      processorOptions: { targetRate: WIRE_IN, frameMs: FRAME_MS, muted: false },
    });
    this.playback = new AudioWorkletNode(this.ctx, 'gx-playback', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { sourceRate: WIRE_OUT },
    });
    this.source.connect(this.capture);
    this.playback.connect(this.ctx.destination);
    this.capture.port.onmessage = (ev) => {
      const data = ev.data || {};
      if (data.type !== 'frame') return;
      if (this.on.level) this.on.level(data.peak || 0);
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(data.pcm);
    };
    this.playback.port.onmessage = (ev) => {
      if (ev.data && ev.data.type === 'state' && this.on.playing) this.on.playing(Boolean(ev.data.playing));
    };
    this.ws = openRealtime('call', this.created.session_id);
    this.ws.addEventListener('message', (ev) => {
      if (typeof ev.data === 'string') {
        let event = null;
        try { event = JSON.parse(ev.data); } catch { return; }
        this.handle(event);
      } else if (ev.data instanceof ArrayBuffer) {
        this.playback.port.postMessage({ type: 'push', pcm: ev.data }, [ev.data]);
      }
    });
    this.ws.addEventListener('close', (ev) => {
      if (!this.stopped && this.on.closed) this.on.closed(ev.code, ev.reason);
      this.teardown();
    });
    this.ws.addEventListener('error', () => {
      if (!this.stopped && this.on.closed) this.on.closed(0, 'connection lost');
    });
    this.keepalive = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: 'ping' }));
    }, 25_000);
  }

  handle(event) {
    const kind = event && event.type;
    if (kind === 'session.status' && event.state) this.state = event.state;
    if (kind === 'session.ready') this.state = 'live';
    if (kind === 'interruption.started' && event.flush) this.playback.port.postMessage({ type: 'flush' });
    if (kind === 'session.ended') this.state = 'ended';
    if (this.on.event) this.on.event(event);
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.capture) this.capture.port.postMessage({ type: 'mute', muted: this.muted });
  }

  teardown() {
    clearInterval(this.keepalive);
    if (this.capture) { this.capture.port.onmessage = null; try { this.capture.disconnect(); } catch { /* gone */ } }
    if (this.source) { try { this.source.disconnect(); } catch { /* gone */ } }
    if (this.playback) { try { this.playback.disconnect(); } catch { /* gone */ } }
    if (this.stream) for (const t of this.stream.getTracks()) t.stop();
    if (this.ctx && this.ctx.state !== 'closed') this.ctx.close().catch(() => {});
    this.stream = null;
    this.ctx = null;
  }

  async stop({ notify = true } = {}) {
    if (this.stopped) return;
    this.stopped = true;
    if (notify && this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'session.end', reason: 'caller_ended' })); } catch { /* closing */ }
    }
    if (this.ws) { try { this.ws.close(1000, 'ended'); } catch { /* closing */ } }
    this.teardown();
  }
}

// ---------------------------------------------------------------- the page
export default {
  title: 'Call Agents',
  async mount(root, ctx) {
    replace(root, pageHeader('Call Agents', 'gx-call · NemotronLabs VoiceChat 11B'), skeletonLines(6));
    let catalog = null;
    let agents = [];
    let model = null;
    try {
      [catalog, agents] = await Promise.all([
        api.get('/api/call/catalog'),
        api.get('/api/call/agents').then((r) => r.agents || []),
      ]);
    } catch (err) {
      replace(root, pageHeader('Call Agents', 'gx-call'),
        callout('danger', 'Call Agents is not reachable', errorText(err), [
          button('Try again', { icon: 'refresh', onClick: () => navigate('call') })]));
      return undefined;
    }
    try {
      model = await api.get('/api/call/model');
    } catch (err) {
      model = { health: { state: 'unreachable' }, error: { message: errorText(err) } };
    }

    if (TABS.some(([id]) => id === ctx.query.tab)) session.tab = ctx.query.tab;
    let live = null; // LiveCall
    let liveView = null; // the session view of the call in progress

    const panel = h('div', { class: 'panel-body', id: uid('call-panel') });
    const head = pageHeader('Call Agents', 'gx-call · NemotronLabs VoiceChat 11B', [
      button('New agent', { icon: 'plus', variant: 'primary', onClick: () => createAgent() }),
    ]);
    const bar = tabs(TABS, session.tab, (id) => { session.tab = id; render(); }, { label: 'Call Agents sections' });
    replace(root, head, engineBanner(), bar, panel);

    function engineBanner() {
      const state = (model && model.health && model.health.state) || 'unknown';
      if (state === 'unreachable') {
        return callout('warn', 'gx-call is not answering',
          'The voice model service on gx10-02 is not reachable, so no new call can start. '
          + 'Agents can still be edited and past calls reviewed.');
      }
      if (state === 'failed') {
        return callout('danger', 'The voice model failed to start', String((model.health && model.health.detail) || ''));
      }
      return null;
    }

    // ------------------------------------------------------------ agents
    async function reloadAgents() {
      agents = (await api.get('/api/call/agents')).agents || [];
    }

    function agentCard(agent) {
      const actions = [
        button('Call', { icon: 'phone', variant: 'primary', size: 'sm', onClick: () => startTab(agent) }),
        button('Edit', { icon: 'edit', size: 'sm', onClick: () => editAgent(agent.agent_id) }),
        iconButton('copy', `Duplicate ${agent.name}`, () => cloneAgent(agent)),
      ];
      if (agent.status !== 'archived') {
        actions.push(iconButton(agent.status === 'enabled' ? 'pause' : 'check',
          agent.status === 'enabled' ? `Disable ${agent.name}` : `Enable ${agent.name}`,
          () => setStatus(agent, agent.status === 'enabled' ? 'disabled' : 'enabled')));
      }
      actions.push(iconButton('trash', `Archive ${agent.name}`, () => archiveAgent(agent)));
      return card(agent.name, h('div', { class: 'stack-sm' },
        agent.description ? h('p', { class: 'muted' }, truncate(agent.description, 180)) : null,
        h('div', { class: 'row-wrap' }, agentStatusBadges(agent)),
        h('div', { class: 'row-wrap' }, (agent.tags || []).map((t) => h('span', { class: 'tag tag-static' }, t))),
        h('p', { class: 'xsmall muted' }, `Updated ${ago(agent.updated_at)} by ${agent.updated_by || 'unknown'}`),
        h('div', { class: 'row-wrap' }, actions)), { sub: agent.use_case, cls: 'model-card' });
    }

    function renderAgents() {
      const visible = agents.filter((a) => a.status !== 'archived');
      if (!visible.length) {
        return emptyState({
          icon: 'phone', title: 'No call agents yet',
          text: 'An agent decides what the voice model is told, which tools it may use and what it must collect.',
          action: button('Create a voice agent', { icon: 'plus', variant: 'primary', onClick: () => createAgent() }),
        });
      }
      return h('div', { class: 'grid-2' }, visible.map(agentCard));
    }

    async function createAgent() {
      const useCase = await pickUseCase();
      if (!useCase) return;
      try {
        const agent = await api.post('/api/call/agents', { template: useCase });
        await reloadAgents();
        session.tab = 'agents';
        render();
        editAgent(agent.agent_id);
      } catch (err) { fail(err); }
    }

    function pickUseCase() {
      return new Promise((resolve) => {
        // The server lists the general agent first; it is the default choice and
        // the rest are optional templates.
        const labels = catalog.use_case_labels || {};
        const options = (catalog.use_cases || ['general']).map((u) => [u, labels[u] || u]);
        const sel = select(options, options[0][0]);
        let picked = null;
        const create = button('Create', { variant: 'primary', onClick: () => { picked = sel.value; dlg.close('ok'); } });
        const dlg = drawer('New call agent',
          h('div', { class: 'stack' },
            field('Start from', sel, { hint: 'Everything can be changed afterwards. The agent starts as a draft.' })),
          [button('Cancel', { onClick: () => dlg.close('cancel') }), create],
          () => resolve(picked));
      });
    }

    async function cloneAgent(agent) {
      const name = await promptDialog({ title: 'Duplicate agent', label: 'Name of the copy',
        value: `${agent.name} (copy)`, okLabel: 'Duplicate', maxLength: 120 });
      if (!name) return;
      try {
        await api.post(`/api/call/agents/${agent.agent_id}/clone`, { name });
        await reloadAgents();
        render();
        toast('Agent duplicated.', 'ok');
      } catch (err) { fail(err); }
    }

    async function setStatus(agent, status, mode) {
      try {
        await api.post(`/api/call/agents/${agent.agent_id}/status`, { status, mode });
        await reloadAgents();
        render();
      } catch (err) { fail(err); }
    }

    async function archiveAgent(agent) {
      const ok = await confirmDialog({
        title: 'Archive this agent?', danger: true, okLabel: 'Archive',
        message: `${agent.name} disappears from the list and can no longer take calls. Past calls and their `
          + 'results are kept.',
      });
      if (!ok) return;
      await setStatus(agent, 'archived');
    }

    // ------------------------------------------------------- agent editor
    async function editAgent(agentId) {
      let agent = null;
      try {
        agent = await api.get(`/api/call/agents/${agentId}`);
      } catch (err) { fail(err); return; }
      const cfg = JSON.parse(JSON.stringify(agent.config));
      const fieldNames = schemaFieldNames(cfg);
      const controls = {};
      const nameInput = textInput({ value: cfg.name, maxLength: 120 });
      const descInput = textArea(cfg.description, { rows: 2, maxLength: 600 });
      const companyInput = textInput({ value: cfg.company, maxLength: 120 });
      const brandInput = textInput({ value: cfg.brand, maxLength: 120 });
      const tagsInput = textInput({ value: (cfg.tags || []).join(', '), maxLength: 200 });
      const voiceSel = select((catalog.voices || ['Aria']).map((v) => [v, v]), cfg.voice);
      const toolChips = chips((catalog.tools || []).map((t) => [t.name, t.label]),
        { value: cfg.tool_permissions || [], multiple: true, label: 'Tools the agent may call' });
      const requiredChips = chips(fieldNames.map((f) => [f, f]),
        { value: cfg.required_fields || [], multiple: true, label: 'Required intake fields' });
      const optionalChips = chips(fieldNames.map((f) => [f, f]),
        { value: cfg.optional_fields || [], multiple: true, label: 'Optional intake fields' });
      const transferType = select([['queue', 'Internal queue'], ['phone', 'Phone number'], ['webhook', 'Webhook']],
        (cfg.transfer_destination || {}).type || 'queue');
      const transferValue = textInput({ value: (cfg.transfer_destination || {}).value || '', maxLength: 120 });
      const recordToggle = toggle('Record calls with this agent', Boolean((cfg.recording || {}).enabled));
      const noticeInput = textInput({ value: (cfg.recording || {}).notice || '', maxLength: 300 });
      const retentionInput = textInput({ value: String(cfg.retention_days), type: 'number' });
      const minutesInput = textInput({ value: String(cfg.max_call_minutes), type: 'number' });
      for (const [key, label, hint] of LONG_FIELDS) {
        controls[key] = textArea(cfg[key], { rows: key === 'qualification_flow' ? 6 : 3,
          maxLength: (catalog.text_limits || {})[key] });
        controls[`${key}__field`] = field(label, controls[key], { hint });
      }
      const promptOut = h('pre', { class: 'code-wrap' });
      const promptInfo = h('p', { class: 'field-hint', 'aria-live': 'polite' });

      function collect() {
        const next = JSON.parse(JSON.stringify(cfg)); // keeps webhooks, schema, hours, post-call actions intact
        next.name = nameInput.value.trim();
        next.description = descInput.value.trim();
        next.company = companyInput.value.trim();
        next.brand = brandInput.value.trim();
        next.voice = voiceSel.value;
        next.tags = tagsInput.value.split(',').map((t) => t.trim().toLowerCase()).filter(Boolean);
        next.tool_permissions = toolChips.getValue();
        next.required_fields = requiredChips.getValue();
        next.optional_fields = optionalChips.getValue().filter((f) => !next.required_fields.includes(f));
        next.transfer_destination = { ...(cfg.transfer_destination || {}), type: transferType.value,
          value: transferValue.value.trim() };
        next.recording = { enabled: recordToggle.input.checked, notice: noticeInput.value.trim() };
        next.retention_days = Number(retentionInput.value) || cfg.retention_days;
        next.max_call_minutes = Number(minutesInput.value) || cfg.max_call_minutes;
        for (const [key] of LONG_FIELDS) next[key] = controls[key].value;
        return next;
      }

      async function preview() {
        clear(promptOut);
        promptInfo.textContent = 'Checking…';
        try {
          const res = await api.post('/api/call/preview', { config: collect() });
          promptOut.textContent = res.compiled_prompt;
          promptInfo.textContent = `Valid · ${res.prompt_chars} characters · tools: `
            + (res.compiled_tools.map((t) => t.name).join(', ') || 'none');
        } catch (err) {
          promptInfo.textContent = errorText(err);
        }
      }

      const saveBtn = button('Save new version', { icon: 'check', variant: 'primary' });
      saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        try {
          const res = await api.post(`/api/call/agents/${agentId}`,
            { config: collect(), base_version: agent.version });
          await reloadAgents();
          render();
          toast(res.unchanged ? 'No changes to save.' : `Saved as version ${res.version}.`, 'ok');
          dlg.close('saved');
        } catch (err) {
          fail(err);
        } finally {
          saveBtn.disabled = false;
        }
      });

      const body = h('div', { class: 'stack' },
        h('div', { class: 'row-wrap' }, agentStatusBadges(agent)),
        field('Name', nameInput),
        field('What it does', descInput),
        h('div', { class: 'grid-2' }, field('Company', companyInput), field('Brand / line name', brandInput)),
        h('div', { class: 'grid-2' }, field('Voice', voiceSel), field('Tags', tagsInput, { hint: 'Comma separated, lower case.' })),
        ...LONG_FIELDS.map(([key]) => controls[`${key}__field`]),
        chipField(`Tools (at most ${catalog.max_tools})`, toolChips,
          'Every tool runs on the Control Center against the real call state.'),
        chipField('Required intake fields', requiredChips),
        chipField('Optional intake fields', optionalChips),
        h('div', { class: 'grid-2' }, field('Transfer to', transferType), field('Destination', transferValue)),
        recordToggle,
        field('Recording notice (spoken)', noticeInput, { hint: 'Required when recording is on.' }),
        h('div', { class: 'grid-2' }, field('Keep call content for (days)', retentionInput),
          field('Maximum call length (minutes)', minutesInput)),
        integrationsSummary(cfg),
        card('Compiled prompt', h('div', { class: 'stack-sm' },
          h('div', { class: 'row-wrap' }, button('Check and preview', { icon: 'search', size: 'sm', onClick: preview })),
          promptInfo, promptOut), { level: 3 }),
        versionsSection(agentId));
      const dlg = drawer(`Edit ${agent.config.name}`, body,
        [button('Close', { onClick: () => dlg.close('cancel') }), saveBtn]);
    }

    function integrationsSummary(cfg) {
      const hooks = cfg.webhooks || [];
      const post = cfg.post_call_actions || [];
      const rows = [
        ['Webhooks', hooks.length ? hooks.map((w) => w.name).join(', ') : 'none'],
        ['Post-call actions', post.length ? post.map((a) => `${a.type} → ${a.target} (${a.when})`).join(', ') : 'none'],
        ['Business hours', (cfg.business_hours && cfg.business_hours.timezone) || 'not set'],
        ['Intake schema', (cfg.structured_output_schema && cfg.structured_output_schema.$id) || 'custom'],
      ];
      return card('Integrations', h('div', { class: 'stack-sm' }, kv(rows),
        h('p', { class: 'xsmall muted' },
          'Webhooks, business hours and the intake schema are edited through the gx-call API '
          + '(see the Call Agents documentation). They are preserved unchanged when this form is saved.')),
      { level: 3 });
    }

    function versionsSection(agentId) {
      const box = h('div', { class: 'stack-sm' }, loading('Loading versions…'));
      api.get(`/api/call/agents/${agentId}/versions`).then(({ versions }) => {
        replace(box, h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Version'), h('th', {}, 'Saved'), h('th', {}, 'By'),
            h('th', {}, 'Calls'), h('th', {}, 'Note'))),
          h('tbody', {}, (versions || []).map((v) => h('tr', {},
            h('td', {}, `v${v.version}`), h('td', {}, ago(v.created_at)), h('td', {}, v.created_by || '—'),
            h('td', {}, String((v.metrics && v.metrics.calls) || 0)), h('td', {}, v.note || '—')))))));
      }).catch((err) => replace(box, callout('warn', 'Versions could not be loaded', errorText(err))));
      return card('Version history', box, { level: 3 });
    }

    // -------------------------------------------------------------- call
    function startTab(agent) {
      session.tab = 'call';
      render(agent.agent_id);
    }

    function renderCall(preselect) {
      const enabled = agents.filter((a) => a.status === 'enabled');
      if (live) return liveCallView();
      if (!enabled.length) {
        return emptyState({
          icon: 'phone', title: 'No agent is enabled',
          text: 'Enable an agent on the Agents tab before calling it. Draft agents cannot take calls.',
          action: button('Go to agents', { onClick: () => { session.tab = 'agents'; render(); } }),
        });
      }
      const agentSel = select(enabled.map((a) => [a.agent_id, `${a.name} · v${a.current_version}`]),
        preselect || enabled[0].agent_id);
      const recordToggle = toggle('Record this call', false,
        { hint: 'Only possible when the agent has recording and its spoken notice configured.' });
      const startBtn = button('Start the call', { icon: 'phone', variant: 'primary' });
      const secure = h('div', {});
      const wrap = h('div', { class: 'stack' },
        card('Start a call', h('div', { class: 'stack' },
          field('Agent', agentSel),
          recordToggle,
          h('p', { class: 'field-hint' },
            'The call runs in this browser tab: your microphone goes to gx-call on gx10-02 over the '
            + 'realtime tunnel, and the agent answers with speech.'),
          h('div', { class: 'row-wrap' }, startBtn)), { sub: 'gx-call' }),
        secure);
      secureContextInfo().then((info) => {
        if (info.ok) return;
        startBtn.disabled = true;
        replace(secure, secureContextCallout(info));
      });
      startBtn.addEventListener('click', async () => {
        startBtn.disabled = true;
        try {
          await startCall(agentSel.value, recordToggle.input.checked);
        } catch (err) {
          fail(err);
          startBtn.disabled = false;
        }
      });
      return wrap;
    }

    async function startCall(agentId, record) {
      const created = await api.post('/api/call/sessions', { agent_id: agentId, record: record || undefined });
      const transcript = [];
      const partial = { caller: '', agent: '' };
      live = new LiveCall(created, {
        event: (event) => onCallEvent(event, transcript, partial),
        level: (peak) => { if (ui.level) ui.level(peak); },
        playing: (on) => { if (ui.playing) ui.playing(on); },
        closed: (code, reason) => {
          toast(code === 1000 ? 'The call ended.' : `The call was disconnected (${reason || code}).`,
            code === 1000 ? 'ok' : 'warn');
          finishCall();
        },
      });
      liveView = { created, transcript, partial, intake: created.state_snapshot || {},
        completion: created.completion || null, transfer: null, state: 'connecting', events: [] };
      render();
      try {
        await live.open();
      } catch (err) {
        await live.stop({ notify: false });
        live = null;
        liveView = null;
        render();
        throw err;
      }
      if (created.record && created.recording_notice) {
        toast(`Recording: "${created.recording_notice}"`, 'warn', 8000);
      }
    }

    const ui = {};

    function onCallEvent(event, transcript, partial) {
      if (!liveView) return;
      const kind = event.type;
      liveView.events.push(event);
      if (liveView.events.length > 400) liveView.events.shift();
      if (kind === 'transcript.user.delta') partial.caller += event.delta || '';
      else if (kind === 'transcript.agent.delta') partial.agent += event.delta || '';
      else if (kind === 'transcript.user.final') { transcript.push({ speaker: 'caller', text: event.text }); partial.caller = ''; }
      else if (kind === 'transcript.agent.final') {
        transcript.push({ speaker: 'agent', text: event.text, interrupted: event.interrupted });
        partial.agent = '';
      } else if (kind === 'state.updated') {
        liveView.intake = event.state || liveView.intake;
        liveView.completion = event.completion || liveView.completion;
      } else if (kind === 'tool.result' && event.state) {
        liveView.intake = event.state;
        if (event.completion) liveView.completion = event.completion;
      } else if (kind === 'transfer.updated') {
        liveView.transfer = { status: event.status, destination: event.destination };
      } else if (kind === 'session.status' || kind === 'session.ready' || kind === 'session.ended') {
        liveView.state = kind === 'session.ended' ? 'ended' : (event.state || 'live');
        if (kind === 'session.ended') finishCall();
      } else if (kind === 'error') {
        toast(event.message || 'The call reported an error.', event.fatal ? 'danger' : 'warn');
      }
      if (ui.refresh) ui.refresh();
    }

    async function finishCall() {
      if (!live) return;
      const sid = live.created.session_id;
      await live.stop({ notify: false });
      live = null;
      try {
        const view = await api.get(`/api/call/sessions/${sid}`);
        liveView = null;
        session.tab = 'history';
        render();
        openSession(view.session_id);
      } catch {
        liveView = null;
        render();
      }
    }

    function liveCallView() {
      const lines = h('div', { class: 'stack-sm', role: 'log', 'aria-live': 'polite', 'aria-label': 'Live transcript' });
      const intakeBox = h('div', { class: 'stack-sm' });
      const statusText = h('span', {});
      const meter = progressBar(0, 'Microphone level');
      const meterFill = meter.firstChild;
      const speaking = badge('listening', 'neutral');
      const muteBtn = button('Mute', { icon: 'mic', size: 'sm' });
      muteBtn.addEventListener('click', () => {
        const next = !live.muted;
        live.setMuted(next);
        muteBtn.replaceChildren(h('span', {}, next ? 'Unmute' : 'Mute'));
        muteBtn.setAttribute('aria-pressed', String(next));
      });
      const transferBtn = button('Request transfer', { icon: 'user', size: 'sm' });
      transferBtn.addEventListener('click', async () => {
        transferBtn.disabled = true;
        try {
          await api.post(`/api/call/sessions/${live.created.session_id}/transfer`,
            { status: 'requested', note: 'requested by the operator' });
          toast('Transfer requested.', 'ok');
        } catch (err) { fail(err); } finally { transferBtn.disabled = false; }
      });
      const endBtn = button('End call', { icon: 'x', variant: 'danger', size: 'sm' });
      endBtn.addEventListener('click', async () => {
        endBtn.disabled = true;
        const call = live;
        const sid = call.created.session_id;
        await call.stop();
        try { await api.post(`/api/call/sessions/${sid}/end`, {}); } catch (err) { fail(err); }
        live = null;
        try {
          liveView = null;
          session.tab = 'history';
          render();
          openSession(sid);
        } catch { render(); }
      });

      ui.level = (peak) => { meterFill.style.width = `${Math.min(100, Math.round(peak * 140))}%`; };
      ui.playing = (on) => {
        speaking.textContent = on ? 'agent speaking' : 'listening';
        speaking.className = `badge badge-${on ? 'ok' : 'neutral'}`;
      };
      ui.refresh = () => {
        if (!liveView) return;
        statusText.textContent = liveView.state;
        clear(lines);
        for (const line of liveView.transcript.slice(-60)) {
          lines.append(h('p', { class: 'log-row' },
            h('strong', {}, line.speaker === 'caller' ? 'You: ' : 'Agent: '),
            h('span', {}, line.text), line.interrupted ? h('span', { class: 'xsmall muted' }, ' (interrupted)') : null));
        }
        for (const [who, text] of [['You', liveView.partial.caller], ['Agent', liveView.partial.agent]]) {
          if (text) lines.append(h('p', { class: 'log-row muted' }, h('strong', {}, `${who}: `), text));
        }
        lines.scrollTop = lines.scrollHeight;
        replace(intakeBox, intakeTable(liveView.intake, liveView.completion),
          liveView.transfer ? badge(`transfer ${liveView.transfer.status}`, 'warn') : null);
      };
      setTimeout(() => ui.refresh(), 0);

      return h('div', { class: 'stack' },
        card(live.created.agent_name || 'Call in progress', h('div', { class: 'stack-sm' },
          h('div', { class: 'row-wrap' }, h('span', { class: 'status' }, 'State: '), statusText, speaking,
            live.created.record ? badge('recording', 'warn') : null),
          meter,
          h('div', { class: 'row-wrap' }, muteBtn, transferBtn, endBtn)),
        { sub: `session ${live.created.session_id.slice(0, 12)}…` }),
        h('div', { class: 'grid-2' },
          card('Transcript', lines, { level: 3 }),
          card('Intake', intakeBox, { level: 3 })));
    }

    function intakeTable(data, completion) {
      const entries = Object.entries(data || {}).filter(([, v]) => v !== null && v !== '' && v !== undefined);
      const rows = entries.map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)]);
      return h('div', { class: 'stack-sm' },
        completion ? h('p', { class: 'small' },
          `${completion.filled || 0} of ${(completion.required || []).length} required fields`
          + (completion.missing && completion.missing.length ? ` · still needed: ${completion.missing.join(', ')}` : ' · complete')) : null,
        rows.length ? kv(rows) : h('p', { class: 'muted' }, 'Nothing captured yet.'));
    }

    // ----------------------------------------------------------- history
    function renderHistory() {
      const box = h('div', { class: 'stack' }, loading('Loading calls…'));
      api.get('/api/call/sessions?limit=50').then(({ sessions }) => {
        if (!sessions || !sessions.length) {
          replace(box, emptyState({ icon: 'phone', title: 'No calls yet',
            text: 'Calls you start here, and calls started with your gateway key, appear in this list.' }));
          return;
        }
        replace(box, h('div', { class: 'history-list' }, sessions.map(sessionRow)));
      }).catch((err) => replace(box, callout('danger', 'Calls could not be loaded', errorText(err))));
      return box;
    }

    function sessionRow(s) {
      const open = button('Open', { size: 'sm', onClick: () => openSession(s.session_id) });
      return h('div', { class: 'log-row row-between' },
        h('div', { class: 'stack-sm' },
          h('p', {}, h('strong', {}, s.agent_name || s.agent_id), ` · v${s.agent_version}`),
          h('p', { class: 'xsmall muted' }, `${dateTime(s.created_at)} · ${s.via}`
            + (s.duration_s ? ` · ${Math.round(s.duration_s)} s` : '')),
          h('div', { class: 'row-wrap' },
            badge(s.state, s.state === 'ended' ? 'neutral' : 'info'),
            s.disposition ? badge(s.disposition, tone(DISPOSITION_TONE, s.disposition)) : null,
            s.recording_asset_id ? badge('recording', 'info') : null,
            s.content_purged ? badge('content deleted', 'neutral') : null)),
        open);
    }

    async function openSession(sid) {
      let view = null;
      try {
        view = await api.get(`/api/call/sessions/${sid}`);
      } catch (err) { fail(err); return; }
      const body = h('div', { class: 'stack' },
        kv([
          ['Agent', `${view.agent_name || view.agent_id} v${view.agent_version}`],
          ['Started', dateTime(view.created_at)],
          ['Duration', view.duration_s ? `${Math.round(view.duration_s)} s` : '—'],
          ['Call outcome', view.disposition || view.state],
          ['Ended because', view.end_reason || '—'],
          ['Transfer', (view.transfer && view.transfer.status) || 'none'],
          ['Started from', view.via],
          ['Reference', view.external_ref || '—'],
        ]),
        view.error ? callout('danger', 'The call failed', String(view.error.message || view.error.code)) : null,
        view.result ? card('Result', h('div', { class: 'stack-sm' },
          h('p', {}, view.result.summary),
          h('div', { class: 'row-wrap' },
            badge(`intake ${view.result.intake_disposition}`, tone(DISPOSITION_TONE, view.result.intake_disposition)),
            view.result.qualification_status ? badge(view.result.qualification_status, 'info') : null),
          intakeTable(view.result.structured, { filled: undefined, required: [], missing: view.result.missing_required })),
        { level: 3 }) : null,
        view.transcript && view.transcript.length ? card('Transcript',
          h('div', { class: 'stack-sm' }, view.transcript.map((t) => h('p', { class: 'log-row' },
            h('strong', {}, t.speaker === 'caller' ? 'Caller: ' : 'Agent: '), t.text))), { level: 3 }) : null,
        view.tools && view.tools.length ? card('Tools used',
          h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Tool'), h('th', {}, 'Result'), h('th', {}, 'Latency'))),
            h('tbody', {}, view.tools.map((t) => h('tr', {},
              h('td', {}, t.name),
              h('td', {}, t.ok ? 'ok' : (t.error || 'failed')),
              h('td', {}, t.latency_ms === null || t.latency_ms === undefined ? '—' : `${t.latency_ms} ms`)))))),
          { level: 3 }) : null,
        view.metrics && Object.keys(view.metrics).length ? card('Timings', kv(
          Object.entries(view.metrics).map(([k, v]) => [k.replace(/_/g, ' '),
            typeof v === 'object' && v ? JSON.stringify(v) : String(v)])), { level: 3 }) : null,
        recordingSection(view));
      const actions = [];
      if (!view.content_purged) {
        actions.push(button('Delete transcript and recording', { icon: 'trash', variant: 'danger-ghost', onClick: async () => {
          const ok = await confirmDialog({ title: 'Delete the call content?', danger: true, okLabel: 'Delete',
            message: 'The transcript, the captured intake and the recording are deleted. '
              + 'The call metadata and the result summary are kept.' });
          if (!ok) return;
          try {
            await api.post(`/api/call/sessions/${sid}/delete-content`, {});
            toast('Call content deleted.', 'ok');
            dlg.close('deleted');
            render();
          } catch (err) { fail(err); }
        } }));
      }
      actions.push(button('Close', { onClick: () => dlg.close('cancel') }));
      const dlg = drawer(`Call ${sid.slice(5, 13)}…`, body, actions);
    }

    function recordingSection(view) {
      if (!view.recording_asset_id) {
        return view.record ? card('Recording', h('p', { class: 'muted' },
          'This call was recorded; the mixed file is still being imported into the Library.'), { level: 3 }) : null;
      }
      const box = h('div', {}, loading('Loading the recording…'));
      getAsset(view.recording_asset_id)
        .then((asset) => replace(box, audioPlayer(asset, { label: 'call recording' })))
        .catch((err) => replace(box, callout('warn', 'The recording could not be loaded', errorText(err))));
      return card('Recording', box, { level: 3 });
    }

    // ------------------------------------------------------------ shell
    function render(preselect) {
      bar.select(session.tab);
      if (session.tab === 'agents') replace(panel, renderAgents());
      else if (session.tab === 'call') replace(panel, renderCall(preselect));
      else replace(panel, renderHistory());
    }

    render(ctx.query.agent);

    return () => {
      if (live) live.stop({ notify: true });
      live = null;
      liveView = null;
    };
  },
};

// A wide side drawer with footer actions, from the shared dialog helper.
function drawer(title, body, actions, onClose) {
  return openDialog({ title, body, actions, className: 'drawer drawer-wide', onClose });
}

// A labelled group of chips. Chips are buttons, so they get a heading and the
// group's own aria-label instead of a <label for>, which only labels controls.
function chipField(label, group, hint) {
  const hintId = hint ? uid('chips-hint') : null;
  if (hintId) group.setAttribute('aria-describedby', hintId);
  return h('div', { class: 'field' },
    h('div', { class: 'field-row' }, h('p', { class: 'field-label' }, label)),
    group,
    hint ? h('p', { class: 'field-hint', id: hintId }, hint) : null);
}
