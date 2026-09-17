// Shared realtime browser audio (Live today, Call Agents next): microphone
// capture into wire-rate PCM, and gapless playback of the assistant's speech
// with an instant flush for barge-in. Both halves run in AudioWorklets so a
// busy main thread never stutters the conversation. Nothing is recorded: the
// frames are handed to the caller and dropped.
//
// getUserMedia needs a secure context. Callers check secureContextInfo() from
// ../realtime.js first and render secureContextCallout() when it is not one.
const CAPTURE_URL = new URL('./capture-worklet.js', import.meta.url);
const PLAYBACK_URL = new URL('./playback-worklet.js', import.meta.url);

export function audioSupported() {
  return typeof window !== 'undefined' && typeof window.AudioContext === 'function'
    && typeof AudioWorkletNode === 'function';
}

function makeContext(options) {
  const Ctor = window.AudioContext || window.webkitAudioContext;
  return new Ctor(options);
}

// Turns a getUserMedia error into something a person can act on.
export function mediaErrorText(err) {
  const name = (err && err.name) || '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Permission was refused. Allow the microphone (and camera, if you want vision) for this site in the browser address bar, then try again.';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No microphone or camera was found on this device.';
  if (name === 'NotReadableError') return 'The microphone or camera is already in use by another application.';
  return (err && err.message) || 'The microphone could not be opened.';
}

/**
 * Open the microphone and deliver Int16 PCM frames at `rate`.
 * onFrame(Int16Array) is called every `frameMs`; onLevel(0..1) follows it.
 */
export async function startMicCapture({ onFrame, onLevel, rate = 16000, frameMs = 100, deviceId } = {}) {
  if (!audioSupported()) throw new Error('This browser cannot capture audio in the background.');
  const constraints = {
    audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    },
  };
  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  const context = makeContext({ latencyHint: 'interactive' });
  try {
    await context.audioWorklet.addModule(CAPTURE_URL);
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    await context.close().catch(() => {});
    throw err;
  }
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, 'gx-capture', {
    numberOfInputs: 1, numberOfOutputs: 0,
    processorOptions: { targetRate: rate, frameMs },
  });
  node.port.onmessage = (event) => {
    const data = event.data || {};
    if (data.type !== 'frame') return;
    if (onLevel) onLevel(data.peak || 0);
    if (onFrame) onFrame(new Int16Array(data.pcm));
  };
  source.connect(node);
  let muted = false;
  let stopped = false;
  return {
    stream,
    context,
    get muted() { return muted; },
    setMuted(value) {
      muted = Boolean(value);
      node.port.postMessage({ type: 'mute', muted });
      stream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
      if (muted && onLevel) onLevel(0);
    },
    async stop() {
      if (stopped) return;
      stopped = true;
      node.port.onmessage = null;
      try { source.disconnect(); } catch { /* already gone */ }
      try { node.disconnect(); } catch { /* already gone */ }
      stream.getTracks().forEach((t) => t.stop());
      await context.close().catch(() => {});
    },
  };
}

/** Gapless playback of assistant PCM with an instant flush for barge-in. */
export async function createSpeaker({ rate = 24000, onState } = {}) {
  if (!audioSupported()) throw new Error('This browser cannot play the assistant audio.');
  const context = makeContext({ latencyHint: 'interactive' });
  await context.audioWorklet.addModule(PLAYBACK_URL);
  const node = new AudioWorkletNode(context, 'gx-playback', {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    processorOptions: { sourceRate: rate },
  });
  const gain = context.createGain();
  node.connect(gain).connect(context.destination);
  let playing = false;
  let bufferedMs = 0;
  node.port.onmessage = (event) => {
    const data = event.data || {};
    if (data.type !== 'state') return;
    playing = Boolean(data.playing);
    bufferedMs = data.bufferedMs || 0;
    if (onState) onState({ playing, bufferedMs });
  };
  let stopped = false;
  return {
    context,
    get playing() { return playing; },
    get bufferedMs() { return bufferedMs; },
    /** `pcm` is an Int16Array of `rate` Hz mono samples. */
    push(pcm, response = 0) {
      if (stopped || !pcm || !pcm.length) return;
      const copy = new Int16Array(pcm);
      node.port.postMessage({ type: 'push', pcm: copy.buffer, response }, [copy.buffer]);
    },
    flush() {
      if (stopped) return;
      node.port.postMessage({ type: 'flush' });
      playing = false;
      bufferedMs = 0;
      if (onState) onState({ playing, bufferedMs });
    },
    setVolume(value) { gain.gain.value = Math.max(0, Math.min(1, value)); },
    resume() { return context.state === 'suspended' ? context.resume() : Promise.resolve(); },
    async stop() {
      if (stopped) return;
      stopped = true;
      node.port.onmessage = null;
      try { node.disconnect(); } catch { /* already gone */ }
      try { gain.disconnect(); } catch { /* already gone */ }
      await context.close().catch(() => {});
    },
  };
}
