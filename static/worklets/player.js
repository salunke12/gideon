// Plays queued Float32 chunks (24 kHz context) and reports level + backlog.
class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.queued = 0;
    this.blocks = 0;
    this.sumSq = 0;
    this.n = 0;
    this.port.onmessage = (e) => {
      if (e.data.clear) {
        this.queue = [];
        this.offset = 0;
        this.queued = 0;
      } else if (e.data.pcm) {
        this.queue.push(e.data.pcm);
        this.queued += e.data.pcm.length;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    let i = 0;
    while (i < out.length && this.queue.length) {
      const cur = this.queue[0];
      const take = Math.min(out.length - i, cur.length - this.offset);
      for (let k = 0; k < take; k++) {
        const s = cur[this.offset + k];
        out[i + k] = s;
        this.sumSq += s * s;
      }
      i += take;
      this.offset += take;
      this.queued -= take;
      if (this.offset >= cur.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    this.n += out.length;
    if (++this.blocks % 6 === 0) {
      this.port.postMessage({ rms: Math.sqrt(this.sumSq / this.n), queued: this.queued });
      this.sumSq = 0;
      this.n = 0;
    }
    return true;
  }
}
registerProcessor("player", Player);
