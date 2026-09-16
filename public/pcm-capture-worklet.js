// Live microphone capture for the Real time tab.
// Runs on the audio render thread, resamples the mic signal to 16 kHz mono,
// converts to PCM16, and posts fixed-size frames to the page for streaming.
// This file is used ONLY by realtime.js; the Batch tab keeps using MediaRecorder.
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.ratio = sampleRate / this.targetRate; // sampleRate = actual context rate
    this.readPos = 0; // fractional read cursor carried across buffers
    this.frameSamples = 1024; // ~64 ms per frame at 16 kHz
    this.acc = new Int16Array(this.frameSamples);
    this.accLen = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (!ch || ch.length === 0) return true;

    const ratio = this.ratio;
    let idx = this.readPos;
    while (idx < ch.length) {
      const i0 = Math.floor(idx);
      const frac = idx - i0;
      const s0 = ch[i0];
      const s1 = i0 + 1 < ch.length ? ch[i0 + 1] : s0;
      let sample = s0 + (s1 - s0) * frac;
      sample = Math.max(-1, Math.min(1, sample));
      this.acc[this.accLen++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this.accLen === this.frameSamples) {
        const out = new Int16Array(this.acc); // copy before transfer
        this.port.postMessage(out.buffer, [out.buffer]);
        this.accLen = 0;
      }
      idx += ratio;
    }
    this.readPos = idx - ch.length; // keep fractional remainder for next block
    return true;
  }
}

registerProcessor("pcm-capture", PCMCaptureProcessor);
