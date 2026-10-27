// Microphone -> 16-bit PCM at 16 kHz, in 40 ms chunks.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pending = new Float32Array(0);
    this.pos = 0;
    this.out = new Int16Array(640);
    this.outLen = 0;
    this.sumSq = 0;
    this.count = 0;
  }

  push(sample) {
    const v = Math.max(-1, Math.min(1, sample));
    this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    this.sumSq += sample * sample;
    this.count++;
    if (this.outLen === this.out.length) {
      const rms = Math.sqrt(this.sumSq / this.count);
      this.port.postMessage({ pcm: this.out.buffer, rms }, [this.out.buffer]);
      this.out = new Int16Array(640);
      this.outLen = 0;
      this.sumSq = 0;
      this.count = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    const merged = new Float32Array(this.pending.length + ch.length);
    merged.set(this.pending);
    merged.set(ch, this.pending.length);
    this.pending = merged;

    // Average each window of input samples down to one 16 kHz sample.
    while (
      Math.floor(this.pos + this.ratio) <= this.pending.length &&
      Math.floor(this.pos) < this.pending.length
    ) {
      const start = Math.floor(this.pos);
      const end = Math.max(Math.floor(this.pos + this.ratio), start + 1);
      let sum = 0;
      for (let i = start; i < end; i++) sum += this.pending[i];
      this.push(sum / (end - start));
      this.pos += this.ratio;
    }

    const drop = Math.floor(this.pos);
    this.pending = this.pending.slice(drop);
    this.pos -= drop;
    return true;
  }
}
registerProcessor("capture", Capture);
