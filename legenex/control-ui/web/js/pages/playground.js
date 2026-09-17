import { api, request } from '../api.js';
import {
  h, clear, card, kv, codeBlock, errorBox, toast, stateBadge, bytes, duration,
} from '../dom.js';

let root;
let cfg;
let activeTab = 'chat';
let videoPoll = null;

const $$ = (sel) => root.querySelector(sel);

// --------------------------------------------------------------- snippets
function shellQuote(s) { return `'${s.replace(/'/g, `'\\''`)}'`; }

export function snippets(path, body, { gateway, stream } = {}) {
  const url = `${gateway.replace(/\/$/, '')}${path}`;
  const json = JSON.stringify(body, null, 2);
  const curl = [
    `curl -sS${stream ? 'N' : ''} ${url} \\`,
    '  -H "Authorization: Bearer $GX_API_KEY" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d ${shellQuote(JSON.stringify(body))}`,
  ].join('\n');
  const pyJson = json.includes("'''") ? JSON.stringify(JSON.stringify(body)) : `r'''${json}'''`;
  let python;
  if (path.endsWith('/chat/completions')) {
    python = [
      'import json, os',
      'from openai import OpenAI  # pip install openai',
      '',
      `client = OpenAI(base_url="${gateway}", api_key=os.environ["GX_API_KEY"])`,
      `payload = json.loads(${pyJson})`,
      stream
        ? 'for chunk in client.chat.completions.create(**payload):\n    if chunk.choices and chunk.choices[0].delta.content:\n        print(chunk.choices[0].delta.content, end="", flush=True)'
        : 'resp = client.chat.completions.create(**payload)\nprint(resp.choices[0].message.content)\nprint(resp.usage)',
    ].join('\n');
  } else if (path.endsWith('/images/generations')) {
    python = [
      'import base64, json, os',
      'from openai import OpenAI  # pip install openai',
      '',
      `client = OpenAI(base_url="${gateway}", api_key=os.environ["GX_API_KEY"])`,
      `payload = json.loads(${pyJson})`,
      'extra = {k: payload.pop(k) for k in list(payload) if k not in ("model", "prompt", "size", "n", "quality", "response_format")}',
      'img = client.images.generate(**payload, extra_body=extra)',
      'open("gx-image.png", "wb").write(base64.b64decode(img.data[0].b64_json))',
    ].join('\n');
  } else {
    python = [
      'import json, os, time, urllib.request',
      '',
      `BASE = "${gateway}"`,
      'HDRS = {"Authorization": "Bearer " + os.environ["GX_MEDIA_API_KEY"], "Content-Type": "application/json"}',
      `payload = json.loads(${pyJson})`,
      'req = urllib.request.Request(BASE + "/videos", data=json.dumps(payload).encode(), headers=HDRS)',
      'job = json.load(urllib.request.urlopen(req))',
      'while True:',
      '    st = json.load(urllib.request.urlopen(urllib.request.Request(BASE + "/videos/" + job["id"], headers=HDRS)))',
      '    if st["status"] in ("completed", "failed"):',
      '        break',
      '    time.sleep(5)',
      'mp4 = urllib.request.urlopen(urllib.request.Request(BASE + "/videos/" + job["id"] + "/content", headers=HDRS)).read()',
      'open("gx-video.mp4", "wb").write(mp4)',
    ].join('\n');
  }
  const js = [
    `const res = await fetch("${url}", {`,
    '  method: "POST",',
    `  headers: { Authorization: \`Bearer \${process.env.${path.includes('/videos') ? 'GX_MEDIA_API_KEY' : 'GX_API_KEY'}}\`, "Content-Type": "application/json" },`,
    `  body: JSON.stringify(${json.split('\n').join('\n  ')}),`,
    '});',
    path.endsWith('/chat/completions') && !stream
      ? 'const data = await res.json();\nconsole.log(data.choices[0].message.content, data.usage);'
      : (path.endsWith('/chat/completions')
        ? 'for await (const chunk of res.body) process.stdout.write(new TextDecoder().decode(chunk));'
        : 'console.log(await res.json());'),
  ].join('\n');
  return { curl, python, js };
}

function snippetTabs(snips) {
  const wrap = h('div', { class: 'snippets' });
  const bar = h('div', { class: 'tabbar', role: 'tablist', 'aria-label': 'Code examples' });
  const panel = h('div', { role: 'tabpanel' });
  const langs = [['curl', 'curl', 'bash'], ['python', 'Python', 'python'], ['js', 'JavaScript', 'js']];
  const show = (key) => {
    for (const b of bar.children) b.setAttribute('aria-selected', String(b.dataset.key === key));
    const [, , lang] = langs.find(([k]) => k === key);
    clear(panel).append(codeBlock(snips[key], lang));
  };
  for (const [key, label] of langs) {
    const b = h('button', { type: 'button', role: 'tab', 'data-key': key, class: 'tab' }, label);
    b.addEventListener('click', () => show(key));
    bar.append(b);
  }
  wrap.append(bar, panel);
  show('curl');
  return wrap;
}

function forSnippet(body) {
  const copy = JSON.parse(JSON.stringify(body));
  for (const m of copy.messages || []) {
    if (Array.isArray(m.content)) {
      for (const p of m.content) if (p.image_url) p.image_url.url = 'data:image/png;base64,<BASE64_IMAGE>';
    }
  }
  return copy;
}

// ------------------------------------------------------------------- chat
function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

function chatForm() {
  const models = cfg.chat_models.map((m) => h('option', { value: m }, m));
  const form = h('form', { class: 'pg-form', id: 'chat-form', novalidate: true },
    h('div', { class: 'form-grid' },
      h('div', {}, h('label', { for: 'pg-model' }, 'Model alias'), h('select', { id: 'pg-model', name: 'model' }, models)),
      h('div', {}, h('label', { for: 'pg-temp' }, 'Temperature'),
        h('input', { id: 'pg-temp', name: 'temperature', type: 'number', min: 0, max: 2, step: 0.1, value: 0.7 })),
      h('div', {}, h('label', { for: 'pg-max' }, 'Max tokens'),
        h('input', { id: 'pg-max', name: 'max_tokens', type: 'number', min: 1, max: 16384, step: 1, value: 512 })),
      h('div', { class: 'checks' },
        h('label', { class: 'inline' }, h('input', { type: 'checkbox', id: 'pg-stream', name: 'stream' }), ' Stream'),
        h('label', { class: 'inline' }, h('input', { type: 'checkbox', id: 'pg-tools', name: 'tools' }), ' Tool-call sample (get_weather)'))),
    h('label', { for: 'pg-system' }, 'System prompt (optional)'),
    h('textarea', { id: 'pg-system', name: 'system', rows: 2, maxlength: 8000 }),
    h('label', { for: 'pg-prompt' }, 'Prompt'),
    h('textarea', { id: 'pg-prompt', name: 'prompt', rows: 5, maxlength: 16000, required: true },
      'Explain in two sentences why a two-node cluster should not be treated as one 256 GB machine.'),
    h('div', { id: 'pg-image-row' },
      h('label', { for: 'pg-image' }, 'Image input (vision models: gx-mini, gx-fast, gx-reason, gx-auto; PNG/JPEG/WebP ≤ 8 MiB)'),
      h('input', { id: 'pg-image', type: 'file', accept: 'image/png,image/jpeg,image/webp' }),
      h('img', { id: 'pg-image-preview', alt: 'Selected image preview', hidden: true, class: 'thumb' })),
    h('div', { id: 'pg-max-warn', class: 'callout callout-danger', hidden: true },
      h('p', {}, h('strong', {}, 'gx-max is not running. '),
        'A direct gx-max request makes the orchestrator take over BOTH nodes: it drains gx-mini, gx-fast, gx-reason and the media stack, then loads for about 9 minutes before answering. gx-max never falls back to another model.'),
      h('label', { class: 'inline' }, h('input', { type: 'checkbox', id: 'pg-max-confirm' }), ' I understand — acquire gx-max for this request')),
    h('div', { class: 'btn-row' },
      h('button', { type: 'submit', class: 'btn btn-primary', id: 'pg-send' }, 'Send'),
      h('button', { type: 'button', class: 'btn btn-ghost', id: 'pg-stop', hidden: true }, 'Stop')));
  return form;
}

function renderChatResult(out, res, { streamed } = {}) {
  clear(out);
  const msg = (((res.response || {}).choices || [])[0] || {}).message || {};
  const content = streamed ? res.content : msg.content;
  const reasoning = streamed ? res.reasoning : (msg.reasoning_content || msg.reasoning);
  const toolCalls = streamed ? res.tool_calls : msg.tool_calls;
  out.append(
    h('div', { class: 'card-head' }, h('h3', {}, 'Response'), stateBadge(res.ok ? 'succeeded' : 'failed', res.ok ? 'OK' : `HTTP ${res.status}`)),
    kv([
      ['Latency', `${res.latency_ms} ms${res.ttft_ms ? ` (first token ${res.ttft_ms} ms)` : ''}`],
      ['Model requested', res.model_requested || res.request.model],
      ['Model used', res.model_used || '—'],
      ['Routed to', res.routed_to || '—'],
      ['Tokens', res.usage ? `prompt ${res.usage.prompt_tokens} · completion ${res.usage.completion_tokens} · total ${res.usage.total_tokens}` : '—'],
    ]),
    content ? h('div', { class: 'answer', tabindex: '0' }, content) : h('p', { class: 'muted' }, '(no text content)'),
    reasoning ? h('details', {}, h('summary', {}, 'Reasoning content'), h('pre', { class: 'code', tabindex: '0' }, reasoning)) : null,
    toolCalls && toolCalls.length ? h('div', {}, h('h4', {}, 'Tool calls'), codeBlock(JSON.stringify(toolCalls, null, 2), 'json')) : null,
    h('details', {}, h('summary', {}, 'Request sent (credentials are added server-side and never shown)'), codeBlock(JSON.stringify(res.request, null, 2), 'json')),
    res.response ? h('details', {}, h('summary', {}, 'Raw response'), codeBlock(JSON.stringify(res.response, null, 2), 'json')) : null,
    h('h3', {}, 'Use it from code'),
    snippetTabs(snippets('/chat/completions', forSnippet(res.request), { gateway: cfg.gateway_url, stream: res.request.stream })),
  );
}

async function streamChat(body, out, ctrl) {
  const res = await request('POST', '/api/playground/chat', body, { raw: true, signal: ctrl.signal });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error ? data.error.message : `HTTP ${res.status}`);
  }
  const acc = { content: '', reasoning: '', tool_calls: [], model_used: null, usage: null, ok: false, status: res.status };
  const live = h('div', { class: 'answer streaming', 'aria-live': 'polite' });
  clear(out).append(h('p', { class: 'muted' }, 'Streaming…'), live);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let event = 'message';
  const t0 = performance.now();
  let ttft = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) { event = 'message'; continue; }
      if (line.startsWith('event:')) { event = line.slice(6).trim(); continue; }
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (payload === '[DONE]') continue;
      let obj;
      try { obj = JSON.parse(payload); } catch { continue; }
      if (event === 'gx-error') throw new Error(obj.error);
      if (event === 'gx-meta') {
        acc.latency_ms = obj.latency_ms;
        acc.ttft_ms = obj.ttft_ms;
        acc.request = obj.request;
        continue;
      }
      if (obj.error) throw new Error(obj.error.message || JSON.stringify(obj.error));
      acc.model_used = obj.model || acc.model_used;
      if (obj.usage) acc.usage = obj.usage;
      const delta = ((obj.choices || [])[0] || {}).delta || {};
      if (delta.content) {
        if (ttft === null) ttft = Math.round(performance.now() - t0);
        acc.content += delta.content;
        live.textContent = acc.content;
      }
      if (delta.reasoning_content) acc.reasoning += delta.reasoning_content;
      if (delta.tool_calls) acc.tool_calls.push(...delta.tool_calls);
    }
  }
  acc.ok = Boolean(acc.content || acc.reasoning || acc.tool_calls.length);
  acc.request = acc.request || body;
  renderChatResult(out, acc, { streamed: true });
  return acc;
}

function mountChat(panel) {
  const form = chatForm();
  const out = h('div', { class: 'pg-output', id: 'chat-output', 'aria-live': 'polite' });
  panel.append(card('Chat / vision / tools', form), card('Result', out));
  const model = form.querySelector('#pg-model');
  const imgRow = form.querySelector('#pg-image-row');
  const warn = form.querySelector('#pg-max-warn');
  const updateModel = () => {
    imgRow.hidden = !cfg.vision_models.includes(model.value);
    warn.hidden = !(model.value === 'gx-max' && cfg.gxmax_state !== 'ready');
  };
  model.addEventListener('change', updateModel);
  updateModel();
  let imageData = null;
  form.querySelector('#pg-image').addEventListener('change', async (ev) => {
    const file = ev.target.files[0];
    const prev = form.querySelector('#pg-image-preview');
    if (!file) { imageData = null; prev.hidden = true; return; }
    if (file.size > 8 * 1024 * 1024) { toast('Image exceeds 8 MiB', 'crit'); ev.target.value = ''; return; }
    imageData = await readFileAsDataURL(file);
    prev.src = imageData;
    prev.hidden = false;
  });
  let ctrl = null;
  form.querySelector('#pg-stop').addEventListener('click', () => ctrl && ctrl.abort());
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      model: model.value,
      prompt: form.querySelector('#pg-prompt').value,
      system: form.querySelector('#pg-system').value,
      temperature: Number(form.querySelector('#pg-temp').value),
      max_tokens: Number(form.querySelector('#pg-max').value),
      stream: form.querySelector('#pg-stream').checked,
      tools: form.querySelector('#pg-tools').checked,
      confirm_takeover: form.querySelector('#pg-max-confirm').checked,
    };
    if (imageData && cfg.vision_models.includes(body.model)) body.image = imageData;
    if (!body.prompt.trim()) { toast('Enter a prompt', 'warn'); return; }
    const send = form.querySelector('#pg-send');
    const stop = form.querySelector('#pg-stop');
    send.disabled = true;
    stop.hidden = !body.stream;
    const t0 = Date.now();
    clear(out).append(h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spin', 'aria-hidden': 'true' }),
      body.model === 'gx-max' && cfg.gxmax_state !== 'ready' ? 'Acquiring gx-max (about 9 minutes)…' : 'Waiting for the model…'));
    const ticker = setInterval(() => {
      const p = out.querySelector('.loading');
      if (p) p.lastChild.textContent = `${p.lastChild.textContent.replace(/ \(\d+s\)$/, '')} (${Math.round((Date.now() - t0) / 1000)}s)`;
    }, 1000);
    try {
      if (body.stream) {
        ctrl = new AbortController();
        await streamChat(body, out, ctrl);
      } else {
        const res = await api.post('/api/playground/chat', body);
        renderChatResult(out, res);
      }
    } catch (err) {
      if (err.name !== 'AbortError') clear(out).append(errorBox(err));
    } finally {
      clearInterval(ticker);
      send.disabled = false;
      stop.hidden = true;
      ctrl = null;
      cfg = await api.get('/api/playground/config').catch(() => cfg);
      updateModel();
    }
  });
}

// ------------------------------------------------------------------ image
function mountImage(panel) {
  const form = h('form', { class: 'pg-form', id: 'image-form', novalidate: true },
    h('label', { for: 'img-prompt' }, 'Prompt'),
    h('textarea', { id: 'img-prompt', rows: 3, maxlength: 4000 }, 'a red fox sitting in fresh snow at sunrise, detailed fur, soft light'),
    h('div', { class: 'form-grid' },
      h('div', {}, h('label', { for: 'img-size' }, 'Size'),
        h('select', { id: 'img-size' }, cfg.image_sizes.map((s) => h('option', { value: s, selected: s === '1024x1024' }, s)))),
      h('div', {}, h('label', { for: 'img-quality' }, 'Quality'),
        h('select', { id: 'img-quality' }, h('option', { value: 'standard' }, 'standard (Lightning 4-step, ~15-30 s)'),
          h('option', { value: 'hd' }, 'hd (full sampling, ~4 min)'))),
      h('div', {}, h('label', { for: 'img-seed' }, 'Seed (optional)'), h('input', { id: 'img-seed', type: 'number', min: 0 }))),
    h('label', { for: 'img-neg' }, 'Negative prompt (optional)'),
    h('input', { id: 'img-neg', maxlength: 2000 }),
    h('div', { class: 'btn-row' }, h('button', { type: 'submit', class: 'btn btn-primary', id: 'img-send' }, 'Generate image')));
  const out = h('div', { class: 'pg-output', id: 'image-output', 'aria-live': 'polite' });
  panel.append(card('gx-image — text to image (LiteLLM /v1/images/generations)', form), card('Result', out));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      prompt: form.querySelector('#img-prompt').value,
      size: form.querySelector('#img-size').value,
      quality: form.querySelector('#img-quality').value,
      seed: form.querySelector('#img-seed').value,
      negative_prompt: form.querySelector('#img-neg').value,
    };
    const btn = form.querySelector('#img-send');
    btn.disabled = true;
    clear(out).append(h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), 'Generating on gx10-02…'));
    try {
      const res = await api.post('/api/playground/image', body);
      clear(out).append(
        h('div', { class: 'card-head' }, h('h3', {}, 'Generated image'), stateBadge(res.ok ? 'succeeded' : 'failed')),
        kv([
          ['Latency', `${(res.latency_ms / 1000).toFixed(1)} s`],
          ['Size', res.request.size],
          ['Bytes', res.images[0] ? bytes(res.images[0].bytes) : '—'],
          ['Router metadata', res.response_meta && res.response_meta.gx ? `${res.response_meta.gx.workflow} · seed ${res.response_meta.gx.seed} · ${res.response_meta.gx.elapsed_seconds} s on ${res.response_meta.gx.node}` : '—'],
        ]),
        ...res.images.map((img, i) => h('img', { src: img.data_url, alt: `Generated image ${i + 1} for: ${res.request.prompt}`, class: 'gen-image', id: `gen-image-${i}` })),
        h('details', {}, h('summary', {}, 'Request sent'), codeBlock(JSON.stringify(res.request, null, 2), 'json')),
        h('h3', {}, 'Use it from code'),
        snippetTabs(snippets('/images/generations', res.request, { gateway: cfg.gateway_url })),
      );
    } catch (err) {
      clear(out).append(errorBox(err));
    } finally {
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ video
async function frameCheck(video, samples = 6) {
  const canvas = h('canvas', { width: 64, height: 64 });
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const hashes = new Set();
  const dur = video.duration;
  if (!Number.isFinite(dur) || dur <= 0) return { sampled: 0, distinct: 0 };
  for (let i = 0; i < samples; i += 1) {
    const t = (dur * (i + 0.5)) / samples;
    await new Promise((resolve) => {
      const done = () => { video.removeEventListener('seeked', done); resolve(); };
      video.addEventListener('seeked', done);
      video.currentTime = t;
      setTimeout(done, 3000);
    });
    ctx.drawImage(video, 0, 0, 64, 64);
    const data = ctx.getImageData(0, 0, 64, 64).data;
    let hsh = 0;
    for (let j = 0; j < data.length; j += 16) hsh = (hsh * 31 + (data[j] >> 3)) >>> 0;
    hashes.add(hsh);
  }
  video.currentTime = 0;
  return { sampled: samples, distinct: hashes.size };
}

function mountVideo(panel) {
  const form = h('form', { class: 'pg-form', id: 'video-form', novalidate: true },
    h('label', { for: 'vid-prompt' }, 'Prompt'),
    h('textarea', { id: 'vid-prompt', rows: 3, maxlength: 4000 }, 'a paper boat drifting down a small stream, gentle camera pan, afternoon light'),
    h('div', { class: 'form-grid' },
      h('div', {}, h('label', { for: 'vid-seconds' }, 'Seconds (0.5–10)'),
        h('input', { id: 'vid-seconds', type: 'number', min: 0.5, max: 10, step: 0.5, value: 2 })),
      h('div', {}, h('label', { for: 'vid-size' }, 'Size'),
        h('select', { id: 'vid-size' }, cfg.video_sizes.map((s) => h('option', { value: s, selected: s === '640x640' }, s)))),
      h('div', {}, h('label', { for: 'vid-seed' }, 'Seed (optional)'), h('input', { id: 'vid-seed', type: 'number', min: 0 }))),
    h('div', { class: 'btn-row' }, h('button', { type: 'submit', class: 'btn btn-primary', id: 'vid-send' }, 'Generate video')));
  const out = h('div', { class: 'pg-output', id: 'video-output', 'aria-live': 'polite' });
  panel.append(card('gx-video — text to video (media router /v1/videos, asynchronous)', form), card('Result', out));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      prompt: form.querySelector('#vid-prompt').value,
      seconds: Number(form.querySelector('#vid-seconds').value),
      size: form.querySelector('#vid-size').value,
      seed: form.querySelector('#vid-seed').value,
    };
    const btn = form.querySelector('#vid-send');
    btn.disabled = true;
    const status = h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), 'Submitting…');
    clear(out).append(status);
    try {
      const sub = await api.post('/api/playground/video', body);
      const id = sub.job.id;
      const t0 = Date.now();
      const tick = async () => {
        try {
          const st = await api.get(`/api/playground/video/${encodeURIComponent(id)}`);
          const job = st.job;
          status.lastChild.textContent = `Job ${id}: ${job.status} (${Math.round((Date.now() - t0) / 1000)} s)`;
          if (job.status === 'completed') {
            const video = h('video', { controls: true, src: `/api/playground/video/${encodeURIComponent(id)}/content`, class: 'gen-video', id: 'gen-video', preload: 'auto', muted: true, playsinline: true });
            const check = h('p', { id: 'frame-check', class: 'muted' }, 'Checking frames…');
            clear(out).append(
              h('div', { class: 'card-head' }, h('h3', {}, 'Generated video'), stateBadge('succeeded')),
              kv([
                ['Job', id],
                ['Wall time', duration((Date.now() - t0) / 1000)],
                ['Router metadata', job.gx ? JSON.stringify(job.gx) : '—'],
              ]),
              video, check,
              h('details', {}, h('summary', {}, 'Request sent'), codeBlock(JSON.stringify(sub.request, null, 2), 'json')),
              h('h3', {}, 'Use it from code'),
              h('p', { class: 'muted small' }, 'gx-video uses the media router\'s own asynchronous contract (there is no OpenAI video standard). From gx10-01 its base URL is http://192.168.100.11:18800/v1 with GX_MEDIA_API_KEY.'),
              snippetTabs(snippets('/videos', sub.request, { gateway: 'http://192.168.100.11:18800/v1' })),
            );
            video.addEventListener('loadeddata', async () => {
              const r = await frameCheck(video);
              check.textContent = `Duration ${video.duration.toFixed(2)} s · ${video.videoWidth}x${video.videoHeight} · ${r.distinct} distinct of ${r.sampled} sampled frames`;
              check.dataset.distinct = String(r.distinct);
            }, { once: true });
            btn.disabled = false;
            return;
          }
          if (job.status === 'failed') {
            clear(out).append(errorBox(new Error(`video job failed: ${JSON.stringify(job.error || job)}`)));
            btn.disabled = false;
            return;
          }
          videoPoll = setTimeout(tick, 4000);
        } catch (err) {
          clear(out).append(errorBox(err));
          btn.disabled = false;
        }
      };
      tick();
    } catch (err) {
      clear(out).append(errorBox(err));
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------- page
function showTab(name) {
  activeTab = name;
  for (const b of root.querySelectorAll('.pg-tabs [role=tab]')) {
    b.setAttribute('aria-selected', String(b.dataset.tab === name));
    b.tabIndex = b.dataset.tab === name ? 0 : -1;
  }
  for (const p of root.querySelectorAll('.pg-panel')) p.hidden = p.dataset.tab !== name;
}

export default {
  title: 'API Playground',
  interval: 0,
  async mount(el, { params }) {
    root = el;
    cfg = await api.get('/api/playground/config');
    const tabs = h('div', { class: 'tabbar pg-tabs', role: 'tablist', 'aria-label': 'Playground mode' });
    const panels = [];
    for (const [key, label, mountFn] of [['chat', 'Chat / vision / tools', mountChat], ['image', 'gx-image', mountImage], ['video', 'gx-video', mountVideo]]) {
      const b = h('button', { type: 'button', role: 'tab', class: 'tab', 'data-tab': key, id: `tab-${key}` }, label);
      b.addEventListener('click', () => showTab(key));
      b.addEventListener('keydown', (ev) => {
        const keys = ['chat', 'image', 'video'];
        const i = keys.indexOf(key);
        if (ev.key === 'ArrowRight') { showTab(keys[(i + 1) % 3]); root.querySelector(`#tab-${keys[(i + 1) % 3]}`).focus(); }
        if (ev.key === 'ArrowLeft') { showTab(keys[(i + 2) % 3]); root.querySelector(`#tab-${keys[(i + 2) % 3]}`).focus(); }
      });
      tabs.append(b);
      const panel = h('div', { class: 'pg-panel', role: 'tabpanel', 'data-tab': key, 'aria-labelledby': `tab-${key}` });
      mountFn(panel);
      panels.push(panel);
    }
    clear(root).append(
      h('p', { class: 'lead' }, 'Requests go from this page to the control-UI backend, which calls the real LiteLLM gateway (or the media router for video) with the server-side credential. ',
        `Clients use ${cfg.gateway_url} with their own key.`),
      tabs, ...panels);
    showTab(params && ['chat', 'image', 'video'].includes(params[0]) ? params[0] : activeTab);
  },
  unmount() { if (videoPoll) clearTimeout(videoPoll); },
};
