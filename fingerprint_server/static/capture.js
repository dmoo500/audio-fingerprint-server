class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.remaining = options.processorOptions.frames;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length || this.remaining <= 0) return this.remaining > 0;
    const length = Math.min(channels[0].length, this.remaining);
    const mono = new Float32Array(length);
    for (const channel of channels) {
      for (let index = 0; index < length; index++) mono[index] += channel[index] / channels.length;
    }
    this.remaining -= length;
    this.port.postMessage({ samples: mono, done: this.remaining === 0 }, [mono.buffer]);
    return this.remaining > 0;
  }
}
registerProcessor("bounded-capture", CaptureProcessor);
