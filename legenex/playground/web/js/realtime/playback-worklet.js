// Assistant audio playback worklet (shared realtime code: Live, Call Agents).
// Queues signed 16-bit PCM chunks that arrive at the wire rate, resamples them
// to the output rate and plays them gaplessly. `flush` drops everything that
// is queued, which is what makes barge-in feel instant: the moment the person
// starts talking the assistant stops mid-word.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.sourceRate = opts.sourceRate || 24000;
    this.step = this.sourceRate / sampleRate;
    this.queue = []; // [{ data: Float32Array, response: number }]
    this.position = 0;
    this.queued = 0; // samples at the source rate
    this.playing = false;
    this.reported = -1;
    this.sinceReport = 0;
    this.port.onmessage = (event) => {
      const data = event.data || {};
      if (data.type === 'push' && data.pcm) {
        const pcm = new Int16Array(data.pcm);
        const floats = new Float32Array(pcm.length);
        for (let i = 0; i < pcm.length; i += 1) floats[i] = pcm[i] / 32768;
        this.queue.push({ data: floats, response: data.response || 0 });
        this.queued += floats.length;
      } else if (data.type === 'flush') {
        this.queue = [];
        this.position = 0;
        this.queued = 0;
      }
    };
  }

  bufferedMs() {
    return Math.round(((this.queued - this.position) / this.sourceRate) * 1000);
  }

  report(force) {
    const ms = this.bufferedMs();
    this.sinceReport += 1;
    if (!force && this.sinceReport < 8 && ms === this.reported) return;
    this.sinceReport = 0;
    this.reported = ms;
    this.port.postMessage({ type: 'state', playing: this.playing, bufferedMs: ms });
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    const channel = output[0];
    const frames = channel.length;
    let produced = 0;
    while (produced < frames && this.queue.length) {
      const head = this.queue[0];
      const index = Math.floor(this.position);
      if (index >= head.data.length) {
        this.queue.shift();
        this.queued -= head.data.length;
        this.position -= head.data.length;
        if (this.position < 0) this.position = 0;
        continue;
      }
      const frac = this.position - index;
      const a = head.data[index];
      const b = index + 1 < head.data.length ? head.data[index + 1]
        : (this.queue[1] ? this.queue[1].data[0] : a);
      channel[produced] = a + (b - a) * frac;
      produced += 1;
      this.position += this.step;
    }
    for (let i = produced; i < frames; i += 1) channel[i] = 0;
    for (let c = 1; c < output.length; c += 1) output[c].set(channel);
    const playing = produced > 0;
    if (playing !== this.playing) {
      this.playing = playing;
      this.report(true);
    } else {
      this.report(false);
    }
    return true;
  }
}

registerProcessor('gx-playback', PlaybackProcessor);
