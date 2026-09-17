// Microphone capture worklet (shared realtime code: Live, Call Agents).
// Runs on the audio thread: resamples the microphone to the wire rate with
// linear interpolation, packs signed 16-bit little-endian PCM and posts one
// transferable buffer per frame. Nothing is stored; the main thread decides
// what to send. `mute` keeps the graph alive but sends silence-free nothing,
// so the server never hears a muted microphone.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameSamples = Math.max(320, Math.round((opts.frameMs || 100) * this.targetRate / 1000));
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Int16Array(this.frameSamples);
    this.filled = 0;
    this.position = 0; // fractional read position in the incoming block
    this.last = 0; // final sample of the previous block, for interpolation
    this.peak = 0;
    this.muted = Boolean(opts.muted);
    this.port.onmessage = (event) => {
      const data = event.data || {};
      if (data.type === 'mute') this.muted = Boolean(data.muted);
    };
  }

  emit() {
    const frame = this.buffer.slice(0, this.filled);
    this.port.postMessage({ type: 'frame', pcm: frame.buffer, samples: this.filled, peak: this.peak },
      [frame.buffer]);
    this.filled = 0;
    this.peak = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    if (this.muted) {
      this.filled = 0;
      this.position = 0;
      this.last = channel[channel.length - 1];
      return true;
    }
    const n = channel.length;
    while (this.position < n) {
      const index = Math.floor(this.position);
      const frac = this.position - index;
      const a = index === 0 ? this.last : channel[index - 1];
      const b = channel[index];
      let value = a + (b - a) * frac;
      if (value > 1) value = 1; else if (value < -1) value = -1;
      const abs = value < 0 ? -value : value;
      if (abs > this.peak) this.peak = abs;
      this.buffer[this.filled] = Math.round(value * 32767);
      this.filled += 1;
      if (this.filled >= this.frameSamples) this.emit();
      this.position += this.ratio;
    }
    this.position -= n;
    this.last = channel[n - 1];
    return true;
  }
}

registerProcessor('gx-capture', CaptureProcessor);
