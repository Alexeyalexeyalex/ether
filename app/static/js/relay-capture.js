/* Bounded 40 ms mono PCM capture for the WebSocket fallback. */
class RelayCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.samples = new Int16Array(960);
    this.index = 0;
    this.peak = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    const length = channels[0].length;
    for (let i = 0; i < length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] || 0;
      this.sum += value / channels.length;
      this.count++;
      this.phase += 24000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        this.samples[this.index++] = Math.round(Math.max(-1, Math.min(1, this.sum / this.count)) * 32767);
        this.peak = Math.max(this.peak, Math.abs(this.samples[this.index - 1]));
        this.sum = 0;
        this.count = 0;
        if (this.index === 960) {
          // The existing microphone noise gate produces exact silence. Do not
          // send silence continuously through the laptop's upstream connection.
          if (this.peak > 1) {
            const buffer = this.samples.buffer;
            this.port.postMessage({ buffer: buffer, time: currentTime }, [buffer]);
            this.samples = new Int16Array(960);
          }
          this.index = 0;
          this.peak = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('efir-relay-capture', RelayCapture);
