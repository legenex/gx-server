// Live (gx-live, MiniCPM-o 4.5 on gx10-02): a spoken conversation you can
// interrupt, with the camera as a second input and the bigger GX text models
// one tool call away.
//
// The browser keeps streaming microphone audio (and camera stills) while the
// assistant is speaking — that full-duplex transport is what makes barge-in
// possible. Audio never touches gx10-01: the WebSocket goes to this origin and
// the Playground tunnels it to gx10-02 (plt.md section 1).
import { api } from '../api.js';
import { clear, confirmDialog, h, mmss, replace, storeGet, storeSet, toast } from '../dom.js';
import { navigate } from '../nav.js';
import { openRealtime, secureContextCallout, secureContextInfo } from '../realtime.js';
import { createSpeaker, mediaErrorText, startMicCapture } from '../realtime/audio.js';
import { startCamera } from '../realtime/camera.js';
import {
  badge, button, callout, card, field, iconButton, kv, pageHeader, select, skeletonLines, slider, textInput,
  toggle,
} from '../ui.js';

const PROTOCOL = 'gx-live.v1';
const HEADER = 8;
const KIND_MIC = 0x01;
const KIND_CAMERA = 0x02;
const KIND_ASSISTANT_AUDIO = 0x11;
const MIC_RATE = 16000;
const OUT_RATE = 24000;
const FRAME_MS = 100;
const LANGUAGES = [['en', 'English'], ['zh', 'Chinese']];
const STATE_TONE = {
  unloaded: 'idle', waiting: 'warn', loading: 'busy', ready: 'ok', busy: 'accent', ended: 'idle',
  failed: 'bad', connecting: 'busy',
};
const STATE_WORD = {
  unloaded: 'Not loaded', waiting: 'Waiting for memory', loading: 'Loading MiniCPM-o 4.5',
  ready: 'Listening', busy: 'Answering', ended: 'Ended', failed: 'Failed', connecting: 'Connecting',
};

const prefs = {
  language: storeGet('live.language', 'en'),
  instructions: storeGet('live.instructions', ''),
  tools: storeGet('live.tools', true),
  outputAudio: storeGet('live.outputAudio', true),
  silenceMs: Number(storeGet('live.silenceMs', 700)) || 700,
  threshold: Number(storeGet('live.threshold', 0.5)) || 0.5,
  maxTokens: Number(storeGet('live.maxTokens', 256)) || 256,
};

function packFrame(kind, payload, response = 0, seq = 0) {
  const out = new Uint8Array(HEADER + payload.length);
  const view = new DataView(out.buffer);
  view.setUint8(0, kind);
  view.setUint8(1, 1);
  view.setUint16(2, response & 0xffff);
  view.setUint32(4, seq >>> 0);
  out.set(payload, HEADER);
  return out.buffer;
}

function unpackFrame(buffer) {
  if (buffer.byteLength < HEADER) return null;
  const view = new DataView(buffer);
  return {
    kind: view.getUint8(0), version: view.getUint8(1), response: view.getUint16(2),
    seq: view.getUint32(4), payload: new Uint8Array(buffer, HEADER),
  };
}

function waitingText(waiting) {
  if (!waiting) return '';
  const parts = [];
  if (waiting.reason) parts.push(waiting.reason);
  if (typeof waiting.required_gib === 'number' && typeof waiting.available_gib === 'number') {
    parts.push(`needs ${waiting.required_gib.toFixed(0)} GiB, ${waiting.available_gib.toFixed(0)} GiB free`);
  }
  return parts.join(' · ');
}

function closeText(event) {
  const map = {
    1000: 'The session ended.', 1001: 'The server closed the session (shutdown, gx-max or an idle tunnel).',
    1003: 'The server refused a media frame.', 1007: 'A malformed message closed the session.',
    1008: 'The session was refused (policy).', 1009: 'A frame was too large.',
    1011: 'gx-live failed internally.', 4409: 'Another browser tab took over this session.',
  };
  return map[event.code] || `The connection closed (code ${event.code}).`;
}

export default {
  title: 'Live',
  async mount(root, ctx) {
    let alive = true;
    replace(root, pageHeader('Live', 'gx-live · MiniCPM-o 4.5'), skeletonLines(6));

    const secure = await secureContextInfo();
    let model = null;
    let modelError = null;
    try {
      model = await api.get('/api/live/model');
    } catch (err) {
      modelError = err;
    }
    if (!alive) return undefined;

    // ------------------------------------------------------------- state
    const session = {
      id: null, ws: null, mic: null, camera: null, speaker: null, state: 'unloaded', ready: false,
      startedAt: 0, turns: [], transcript: [], pendingTurns: [], responses: new Map(),
      interrupted: new Set(), micSeq: 0, camSeq: 0, cameraOn: false, muted: false, saving: false,
      lastPlayback: 0, latest: {}, closing: false,
    };
    const timers = new Set();
    const every = (fn, ms) => { const t = setInterval(fn, ms); timers.add(t); return t; };

    // ------------------------------------------------------------- DOM
    const dot = h('span', { class: 'dot dot-idle', id: 'live-dot' });
    const stateText = h('span', { class: 'live-state-text' }, 'Not started');
    const stateDetail = h('span', { class: 'live-state-detail muted xsmall' }, '');
    const statusBar = h('div', { class: 'live-status', id: 'live-state', role: 'status', 'aria-live': 'polite' },
      dot, stateText, stateDetail);

    const levelFill = h('span', { class: 'live-level-fill', id: 'live-level-fill' });
    const levelMeter = h('div', {
      class: 'live-level', id: 'live-level', role: 'meter', 'aria-label': 'Microphone level',
      'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0',
    }, levelFill);

    const video = h('video', { class: 'live-video', id: 'live-video', muted: true, playsinline: true });
    const videoWrap = h('div', { class: 'live-video-wrap', id: 'live-video-wrap', hidden: true },
      video, h('p', { class: 'live-video-note xsmall muted' }, 'The camera sends up to two stills a second.'));

    const log = h('ol', {
      class: 'live-log', id: 'live-transcript', role: 'log', 'aria-label': 'Conversation', 'aria-live': 'polite',
      'aria-relevant': 'additions text',
    });
    const emptyLog = h('li', { class: 'live-empty muted' },
      'Start the session, then just talk. You can interrupt at any time.');
    log.append(emptyLog);

    const textBox = h('textarea', {
      class: 'input live-text', id: 'live-text', rows: '2', maxLength: '4000',
      placeholder: 'Type instead of talking…', 'aria-label': 'Message',
    });
    const sendBtn = button('Send', { icon: 'wave', variant: 'secondary', onClick: () => sendText() });
    sendBtn.id = 'live-send';

    const startBtn = button('Start session', { icon: 'play', variant: 'primary', onClick: () => start() });
    startBtn.id = 'live-start';
    const endBtn = button('End session', { icon: 'x', variant: 'danger-ghost', onClick: () => end('completed') });
    endBtn.id = 'live-end';
    endBtn.hidden = true;
    const muteBtn = iconButton('mic', 'Mute the microphone', () => setMuted(!session.muted), { pressed: false });
    muteBtn.id = 'live-mute';
    const cameraBtn = iconButton('camera', 'Turn the camera on', () => toggleCamera(), { pressed: false });
    cameraBtn.id = 'live-camera';
    const stopBtn = button('Stop', { icon: 'pause', variant: 'ghost', onClick: () => cancelResponse() });
    stopBtn.id = 'live-interrupt';
    stopBtn.disabled = true;
    stopBtn.title = 'Stop the assistant mid-sentence';
    const talkBtn = button('Send turn', { icon: 'check', variant: 'ghost', onClick: () => commit() });
    talkBtn.id = 'live-commit';
    talkBtn.disabled = true;
    talkBtn.title = 'End your turn now instead of waiting for the pause';

    const controls = h('div', { class: 'live-controls row-wrap' }, startBtn, endBtn, muteBtn, cameraBtn,
      stopBtn, talkBtn);
    for (const b of [muteBtn, cameraBtn]) b.disabled = true;

    const stage = h('section', { class: 'live-stage', 'aria-label': 'Live conversation' },
      statusBar, levelMeter, videoWrap, log,
      h('div', { class: 'live-composer' }, textBox, sendBtn),
      controls);

    // --------------------------------------------------------- setup form
    const languageSel = select(LANGUAGES, prefs.language);
    languageSel.id = 'live-language';
    const instructions = h('textarea', {
      class: 'input', id: 'live-instructions', rows: '3', maxLength: '2000',
      placeholder: 'You are Jarvis. Answer briefly and warmly.',
    });
    instructions.value = prefs.instructions;
    const toolsToggle = toggle('Let the assistant use tools', prefs.tools, {
      hint: 'Time, the bigger GX text models, your Library and public web pages.',
    });
    toolsToggle.input.id = 'live-tools-toggle';
    const speakToggle = toggle('Speak the answers', prefs.outputAudio, {
      hint: 'Off means captions only — useful on a busy network.',
    });
    speakToggle.input.id = 'live-speak-toggle';
    const silence = slider({
      label: 'Pause before answering', min: 300, max: 2000, step: 50, value: prefs.silenceMs,
      format: (v) => `${(v / 1000).toFixed(2)} s`,
      hint: 'How long you may pause mid-sentence before the assistant answers.',
    });
    const sensitivity = slider({
      label: 'Voice detection', min: 0.3, max: 0.9, step: 0.05, value: prefs.threshold,
      format: (v) => v.toFixed(2), hint: 'Higher needs a clearer voice: raise it in a noisy room.',
    });
    const tokens = textInput({ value: String(prefs.maxTokens), placeholder: '256', type: 'number' });
    tokens.id = 'live-tokens';
    tokens.min = '32';
    tokens.max = '1024';
    const setupBody = h('div', { class: 'stack-sm' },
      field('Language', languageSel, { hint: 'MiniCPM-o 4.5 speaks English and Chinese.' }),
      field('Personality and rules', instructions, { hint: 'Optional, up to 2000 characters.' }),
      toolsToggle, speakToggle, silence, sensitivity,
      field('Longest answer', tokens, { hint: '32–1024 tokens. Short answers feel faster.' }));
    const setupCard = card('Session', setupBody, { cls: 'live-setup', level: 2 });

    const toolList = h('ul', { class: 'live-tools', id: 'live-tools' },
      h('li', { class: 'muted xsmall' }, 'Nothing has run yet.'));
    const toolsCard = card('Tool activity', toolList, { cls: 'live-tools-card', level: 2 });

    const metricsBox = h('div', { id: 'live-metrics' }, h('p', { class: 'muted xsmall' }, 'No turns yet.'));
    const saveBtn = button('Save transcript', { icon: 'copy', size: 'sm', onClick: () => saveTranscript() });
    saveBtn.id = 'live-save';
    saveBtn.disabled = true;
    const metricsCard = card('This session', metricsBox, {
      cls: 'live-metrics-card', level: 2,
      actions: [saveBtn, button('Session log', { icon: 'logs', size: 'sm', variant: 'ghost',
        onClick: () => navigate('logs', { kind: 'live' }) })],
    });

    const side = h('aside', { class: 'live-side' }, setupCard, toolsCard, metricsCard);
    const grid = h('div', { class: 'live-grid' }, stage, side);

    const head = pageHeader('Live', 'gx-live · MiniCPM-o 4.5 · talk, show it something, interrupt it',
      [badge('Realtime', 'info')]);
    const notices = h('div', { class: 'stack-sm live-notices' });
    replace(root, head, notices, grid);

    if (!secure.ok) {
      notices.append(secureContextCallout(secure));
      startBtn.disabled = true;
      startBtn.title = 'The microphone needs a secure (HTTPS) connection.';
    }
    if (modelError) {
      notices.append(callout('danger', 'gx-live is not reachable',
        `The Control Center could not ask gx10-02 about gx-live: ${modelError.message}`,
        [button('Try again', { icon: 'refresh', onClick: () => navigate('live') })]));
      startBtn.disabled = true;
    } else if (model && model.error) {
      notices.append(callout('warn', 'gx-live is not ready',
        model.error.message || 'The gx-live supervisor answered with an error.'));
    } else if (model && model.health && model.health.state === 'busy') {
      notices.append(callout('info', 'Another live session is running',
        'gx-live holds MiniCPM-o 4.5 for one session at a time. Starting now will be refused until it ends.'));
    }
    if (model && model.identity && model.identity.repository) {
      const id = model.identity;
      metricsBox.prepend(kv([['Model', `${id.repository} @ ${(id.revision || '').slice(0, 12)}`]]));
    }

    // ----------------------------------------------------------- helpers
    function setState(state, detail = '') {
      session.state = state;
      dot.className = `dot dot-${STATE_TONE[state] || 'idle'}`;
      stateText.textContent = STATE_WORD[state] || state;
      stateDetail.textContent = detail;
    }

    function setLevel(peak) {
      const pct = Math.min(100, Math.round(peak * 140));
      levelFill.style.width = `${pct}%`;
      levelMeter.setAttribute('aria-valuenow', String(pct));
    }

    function line(speaker, text, { interrupted = false, turn = null } = {}) {
      emptyLog.remove();
      const body = h('p', { class: 'live-line-text' }, text);
      const el = h('li', { class: `live-line live-line-${speaker}`, 'data-speaker': speaker },
        h('span', { class: 'live-line-who' }, speaker === 'user' ? 'You' : 'Assistant'), body);
      if (interrupted) el.append(h('span', { class: 'live-line-note xsmall muted' }, 'interrupted'));
      log.append(el);
      log.scrollTop = log.scrollHeight;
      session.transcript.push({ speaker, text, turn, interrupted });
      saveBtn.disabled = session.transcript.length === 0;
      return { el, body };
    }

    function toolRow(callId, name) {
      const existing = toolList.querySelector(`[data-call="${callId}"]`);
      if (existing) return existing;
      clear(toolList);
      const el = h('li', { class: 'live-tool', 'data-call': callId, 'data-state': 'running' },
        h('span', { class: 'live-tool-name' }, name),
        h('span', { class: 'live-tool-detail xsmall muted' }, 'running…'));
      toolList.prepend(el);
      while (toolList.children.length > 8) toolList.lastElementChild.remove();
      return el;
    }

    function renderMetrics() {
      const done = session.turns.filter((t) => t.turn_ms);
      const median = (key) => {
        const values = session.turns.map((t) => t[key]).filter((v) => typeof v === 'number').sort((a, b) => a - b);
        return values.length ? values[Math.floor(values.length / 2)] : null;
      };
      const rows = [
        ['Turns', String(session.turns.length)],
        ['First audio (median)', median('first_audio_ms') === null ? '—' : `${median('first_audio_ms')} ms`],
        ['Turn (median)', median('turn_ms') === null ? '—' : `${median('turn_ms')} ms`],
        ['Interruptions', String(session.turns.filter((t) => t.status === 'interrupted').length)],
      ];
      if (median('interrupt_ms') !== null) rows.push(['Interruption latency', `${median('interrupt_ms')} ms`]);
      if (session.startedAt) rows.push(['Elapsed', mmss(Math.round((Date.now() - session.startedAt) / 1000))]);
      if (session.latest.load_ms) rows.push(['Model load', `${(session.latest.load_ms / 1000).toFixed(1)} s`]);
      clear(metricsBox);
      if (model && model.identity && model.identity.repository) {
        metricsBox.append(kv([['Model', `${model.identity.repository} @ ${(model.identity.revision || '').slice(0, 12)}`]]));
      }
      metricsBox.append(kv(rows));
      if (!done.length && !session.id) {
        metricsBox.append(h('p', { class: 'muted xsmall' }, 'No turns yet.'));
      }
    }

    function send(event) {
      const ws = session.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(JSON.stringify(event));
      return true;
    }

    function sendBinary(kind, payload, response, seq) {
      const ws = session.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (ws.bufferedAmount > 2 * 1024 * 1024) return; // never queue audio without bound
      ws.send(packFrame(kind, payload, response, seq));
    }

    // ------------------------------------------------------- turn records
    function queueTurn(record) {
      session.turns.push(record);
      session.pendingTurns.push(record);
      renderMetrics();
      if (session.pendingTurns.length >= 5) flushTurns();
    }

    async function flushTurns() {
      if (!session.id || !session.pendingTurns.length) return;
      const batch = session.pendingTurns.splice(0, 50);
      try {
        await api.post(`/api/live/sessions/${session.id}/turns`, { turns: batch });
      } catch {
        // Timings are a nicety; never interrupt a conversation for them.
      }
    }

    // ------------------------------------------------------------ events
    function onEvent(ev) {
      switch (ev.type) {
        case 'session.created':
          setState(session.ready ? 'ready' : 'connecting',
            ev.protocol === PROTOCOL ? '' : `unexpected protocol ${ev.protocol}`);
          if (ev.protocol !== PROTOCOL) {
            toast('This page does not understand the server protocol; the session was closed.', 'danger');
            end('failed');
          }
          break;
        case 'model.state':
          session.latest.load_ms = ev.load_ms || session.latest.load_ms;
          setState(ev.state, ev.state === 'waiting' ? waitingText(ev.waiting) : (ev.reason || ''));
          break;
        case 'session.ready':
          session.ready = true;
          session.latest.load_ms = ev.load_ms || session.latest.load_ms;
          setState('ready', 'Say something.');
          for (const b of [muteBtn, cameraBtn, talkBtn]) b.disabled = false;
          renderMetrics();
          break;
        case 'input.speech.started':
          setState('ready', 'Hearing you…');
          if (ev.during_response && session.speaker) session.speaker.flush();
          break;
        case 'input.speech.stopped':
          setState('busy', 'Thinking…');
          break;
        case 'transcript.user':
          if (ev.source === 'speech') line('user', ev.text, { turn: ev.turn });
          break;
        case 'response.started': {
          const entry = line('assistant', '');
          session.responses.set(ev.response, { entry, text: '', turn: ev.turn, trigger: ev.trigger });
          stopBtn.disabled = false;
          setState('busy', 'Answering…');
          break;
        }
        case 'transcript.assistant.delta': {
          const r = session.responses.get(ev.response);
          if (r) {
            r.text += ev.text;
            r.entry.body.textContent = r.text;
            log.scrollTop = log.scrollHeight;
          }
          break;
        }
        case 'response.interrupted': {
          session.interrupted.add(ev.response);
          if (session.speaker) session.speaker.flush();
          const r = session.responses.get(ev.response);
          if (r) r.interrupt = { ms: ev.latency_ms, reason: ev.reason };
          break;
        }
        case 'response.done': {
          const r = session.responses.get(ev.response);
          const m = ev.metrics || {};
          if (r) {
            if (ev.text && ev.text !== r.text) { r.text = ev.text; r.entry.body.textContent = ev.text; }
            if (ev.status === 'interrupted') {
              r.entry.el.append(h('span', { class: 'live-line-note xsmall muted' }, 'interrupted'));
            }
            const stored = session.transcript.find((t) => t.speaker === 'assistant' && t.text === '');
            if (stored) { stored.text = r.text; stored.interrupted = ev.status === 'interrupted'; }
            queueTurn({
              response: ev.response, turn: r.turn || null, trigger: r.trigger || null, status: ev.status,
              first_audio_ms: m.first_audio_ms ?? null, first_text_ms: m.first_text_ms ?? null,
              turn_ms: m.turn_ms ?? null, audio_ms: m.audio_ms ?? null,
              interrupt_ms: (r.interrupt || {}).ms ?? null,
              interrupt_reason: (r.interrupt || {}).reason ?? null,
              assistant_chars: r.text.length, camera: session.cameraOn,
            });
            session.responses.delete(ev.response);
          }
          stopBtn.disabled = true;
          if (session.state !== 'ended') setState('ready', 'Listening.');
          break;
        }
        case 'tool.call': {
          const el = toolRow(ev.call_id, ev.name);
          el.querySelector('.live-tool-detail').textContent = 'starting…';
          break;
        }
        case 'tool.progress': {
          const el = toolRow(ev.call_id, 'tool');
          el.dataset.state = ev.state;
          el.querySelector('.live-tool-detail').textContent =
            [ev.model, ev.detail || ev.state].filter(Boolean).join(' · ');
          break;
        }
        case 'tool.result': {
          const el = toolRow(ev.call_id, ev.name);
          el.dataset.state = ev.ok ? 'ok' : 'failed';
          const detail = ev.ok
            ? [ev.model || '', ev.latency_ms ? `${Math.round(ev.latency_ms / 100) / 10}s` : ''].filter(Boolean).join(' · ')
            : `failed: ${(ev.error && ev.error.message) || 'unknown error'}`;
          el.querySelector('.live-tool-detail').textContent = detail || (ev.ok ? 'done' : 'failed');
          break;
        }
        case 'metrics':
          if (ev.first_audio_ms) session.latest.first_audio_ms = ev.first_audio_ms;
          break;
        case 'error':
          toast(`${ev.message}`, ev.fatal ? 'danger' : 'warn');
          if (ev.fatal) setState('failed', ev.code || '');
          break;
        case 'session.ended':
          finish(ev.reason, `${ev.turns} turn${ev.turns === 1 ? '' : 's'} · ${mmss(Math.round(ev.duration_s || 0))}`);
          break;
        default:
          break;
      }
    }

    function onBinary(buffer) {
      const frame = unpackFrame(buffer);
      if (!frame || frame.kind !== KIND_ASSISTANT_AUDIO || frame.version !== 1) return;
      if (session.interrupted.has(frame.response)) return; // dropped: the person took over
      if (!session.speaker) return;
      const bytes = frame.payload;
      const samples = new Int16Array(bytes.byteLength / 2);
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      for (let i = 0; i < samples.length; i += 1) samples[i] = view.getInt16(i * 2, true);
      session.speaker.push(samples, frame.response);
    }

    // ---------------------------------------------------------- lifecycle
    async function start() {
      startBtn.disabled = true;
      setState('connecting', 'Opening the microphone…');
      let created = null;
      try {
        session.mic = await startMicCapture({
          rate: MIC_RATE, frameMs: FRAME_MS,
          onLevel: setLevel,
          onFrame: (pcm) => {
            const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
            sendBinary(KIND_MIC, bytes, 0, session.micSeq++);
          },
        });
      } catch (err) {
        startBtn.disabled = false;
        setState('unloaded', '');
        notices.append(callout('danger', 'The microphone could not be opened', mediaErrorText(err)));
        return;
      }
      try {
        session.speaker = await createSpeaker({
          rate: OUT_RATE,
          onState: ({ playing, bufferedMs }) => {
            const now = Date.now();
            if (now - session.lastPlayback < 400 && playing) return;
            session.lastPlayback = now;
            send({ type: 'playback.state', playing, buffered_ms: bufferedMs });
          },
        });
        await session.speaker.resume();
      } catch (err) {
        toast(`Audio playback is unavailable: ${err.message}`, 'warn');
      }
      const config = {
        language: languageSel.value,
        instructions: instructions.value.trim(),
        tools: toolsToggle.input.checked,
        output_audio: speakToggle.input.checked,
        vad: { threshold: sensitivity.getValue(), silence_ms: Math.round(silence.getValue()) },
        max_response_tokens: Math.min(1024, Math.max(32, Number(tokens.value) || 256)),
      };
      storeSet('live.language', config.language);
      storeSet('live.instructions', config.instructions);
      storeSet('live.tools', config.tools);
      storeSet('live.outputAudio', config.output_audio);
      storeSet('live.silenceMs', config.vad.silence_ms);
      storeSet('live.threshold', config.vad.threshold);
      storeSet('live.maxTokens', config.max_response_tokens);
      try {
        created = await api.post('/api/live/sessions', { config });
      } catch (err) {
        await teardownMedia();
        startBtn.disabled = false;
        setState('unloaded', '');
        notices.append(callout('danger', 'The session could not be started', err.message,
          [button('Try again', { icon: 'refresh', onClick: () => navigate('live') })]));
        return;
      }
      session.id = created.session_id;
      session.startedAt = Date.now();
      setState(created.model_state === 'ready' ? 'ready' : (created.model_state || 'loading'),
        waitingText(created.waiting));
      endBtn.hidden = false;
      setupDisabled(true);
      openSocket();
      every(renderMetrics, 1000);
      every(() => { if (session.id) flushTurns(); }, 15000);
    }

    function openSocket() {
      let ws = null;
      try {
        ws = openRealtime('live', session.id);
      } catch (err) {
        toast(`The realtime address is invalid: ${err.message}`, 'danger');
        return;
      }
      session.ws = ws;
      ws.addEventListener('message', (event) => {
        if (typeof event.data === 'string') {
          let parsed = null;
          try { parsed = JSON.parse(event.data); } catch { return; }
          onEvent(parsed);
        } else {
          onBinary(event.data);
        }
      });
      ws.addEventListener('close', (event) => {
        if (session.closing || session.state === 'ended') return;
        setState('ended', closeText(event));
        finish('abandoned', closeText(event));
      });
      ws.addEventListener('error', () => {
        if (!session.closing) toast('The realtime connection failed.', 'danger');
      });
      every(() => send({ type: 'ping', t: Date.now() }), 20000);
    }

    function setupDisabled(disabled) {
      for (const el of setupCard.querySelectorAll('input, textarea, select, button')) el.disabled = disabled;
    }

    function setMuted(value) {
      session.muted = value;
      if (session.mic) session.mic.setMuted(value);
      send({ type: 'session.update', muted: value });
      muteBtn.setAttribute('aria-pressed', String(value));
      muteBtn.setAttribute('aria-label', value ? 'Unmute the microphone' : 'Mute the microphone');
      muteBtn.title = value ? 'Unmute the microphone' : 'Mute the microphone';
      muteBtn.classList.toggle('is-muted', value);
      if (value) setLevel(0);
    }

    async function toggleCamera() {
      if (session.cameraOn) {
        if (session.camera) session.camera.stop();
        session.camera = null;
        session.cameraOn = false;
        videoWrap.hidden = true;
        cameraBtn.setAttribute('aria-pressed', 'false');
        cameraBtn.setAttribute('aria-label', 'Turn the camera on');
        send({ type: 'session.update', camera: false });
        return;
      }
      try {
        session.camera = await startCamera({
          video, fps: 1,
          onFrame: (jpeg) => sendBinary(KIND_CAMERA, jpeg, 0, session.camSeq++),
        });
      } catch (err) {
        notices.append(callout('warn', 'The camera could not be opened', mediaErrorText(err)));
        return;
      }
      session.cameraOn = true;
      videoWrap.hidden = false;
      cameraBtn.setAttribute('aria-pressed', 'true');
      cameraBtn.setAttribute('aria-label', 'Turn the camera off');
      send({ type: 'session.update', camera: true });
    }

    function sendText() {
      const text = textBox.value.trim();
      if (!text) return;
      if (!session.ws || session.ws.readyState !== WebSocket.OPEN) {
        toast('Start the session first.', 'warn');
        return;
      }
      if (send({ type: 'input.text', text })) {
        line('user', text);
        textBox.value = '';
        setState('busy', 'Thinking…');
      }
    }

    function commit() {
      if (send({ type: 'input.audio.commit' })) setState('busy', 'Thinking…');
    }

    function cancelResponse() {
      if (session.speaker) session.speaker.flush();
      send({ type: 'response.cancel' });
      stopBtn.disabled = true;
    }

    async function saveTranscript() {
      if (!session.id || session.saving) return;
      const entries = session.transcript.filter((t) => t.text && t.text.trim());
      if (!entries.length) { toast('There is nothing to save yet.', 'warn'); return; }
      session.saving = true;
      saveBtn.disabled = true;
      try {
        const res = await api.post(`/api/live/sessions/${session.id}/transcript`, { entries });
        toast(`Transcript saved (${res.entries} lines).`, 'ok');
      } catch (err) {
        toast(`The transcript could not be saved: ${err.message}`, 'danger');
        saveBtn.disabled = false;
      } finally {
        session.saving = false;
      }
    }

    async function teardownMedia() {
      if (session.camera) { session.camera.stop(); session.camera = null; }
      if (session.mic) { await session.mic.stop(); session.mic = null; }
      if (session.speaker) { await session.speaker.stop(); session.speaker = null; }
      setLevel(0);
    }

    function finish(reason, detail) {
      if (session.state === 'ended') return;
      setState('ended', detail || reason || '');
      session.ready = false;
      stopBtn.disabled = true;
      for (const b of [muteBtn, cameraBtn, talkBtn]) b.disabled = true;
      endBtn.hidden = true;
      startBtn.disabled = !secure.ok;
      setupDisabled(false);
      videoWrap.hidden = true;
      teardownMedia();
      flushTurns();
      renderMetrics();
    }

    async function end(reason) {
      if (!session.id) return;
      if (session.transcript.length && reason === 'completed') {
        const ok = await confirmDialog({
          title: 'End the live session?',
          message: 'The conversation is only in this browser. Save the transcript first if you want to keep it.',
          okLabel: 'End session',
        });
        if (!ok) return;
      }
      session.closing = true;
      const id = session.id;
      send({ type: 'session.stop' });
      try { if (session.ws) session.ws.close(1000, 'client ended'); } catch { /* already closed */ }
      finish(reason, 'Ended.');
      session.closing = false;
      try {
        await api.post(`/api/live/sessions/${id}/end`, { reason });
      } catch {
        // gx-live already ended it, or the Control Center answered 404: the record is closed either way.
      }
    }

    // Leaving the page (or the tab) must not leave MiniCPM-o 4.5 held on gx10-02.
    const onUnload = () => {
      if (session.ws && session.ws.readyState === WebSocket.OPEN) {
        session.closing = true;
        try { session.ws.send(JSON.stringify({ type: 'session.stop' })); } catch { /* closing anyway */ }
        try { session.ws.close(1000, 'page closed'); } catch { /* closing anyway */ }
      }
    };
    window.addEventListener('pagehide', onUnload);
    textBox.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendText(); }
    });

    renderMetrics();
    if (ctx && ctx.query && ctx.query.session) {
      notices.append(callout('info', 'Past session',
        `Session ${ctx.query.session} is in the session log; a live conversation cannot be replayed.`,
        [button('Open the log', { icon: 'logs', size: 'sm', onClick: () => navigate('logs', { kind: 'live' }) })]));
    }

    return () => {
      alive = false;
      window.removeEventListener('pagehide', onUnload);
      for (const t of timers) clearInterval(t);
      timers.clear();
      onUnload();
      const id = session.id;
      teardownMedia();
      if (id) {
        api.post(`/api/live/sessions/${id}/end`, { reason: 'abandoned' }).catch(() => {});
      }
    };
  },
};
