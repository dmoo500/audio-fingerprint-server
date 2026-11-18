globalThis.FingerprintCapture = {
  async record({ workletUrl, signal, onStart = () => {} }) {
    let stream;
    let context;
    let node;
    let source;
    let onAbort;
    try {
      signal?.throwIfAborted();
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      signal?.throwIfAborted();
      context = new AudioContext();
      if (context.sampleRate < 8000 || context.sampleRate > 192000) {
        throw new Error("This microphone's sample rate is not supported.");
      }
      await context.audioWorklet.addModule(workletUrl);
      await context.resume();
      signal?.throwIfAborted();
      const sampleRate = context.sampleRate;
      const frames = sampleRate * 10;
      node = new AudioWorkletNode(context, "bounded-capture", { processorOptions: { frames } });
      source = context.createMediaStreamSource(stream);
      onStart({ microphone: stream.getAudioTracks()[0].label });
      const samples = await new Promise((resolve, reject) => {
        const captured = new Float32Array(frames);
        let position = 0;
        const timer = setTimeout(() => reject(new Error("Microphone capture timed out.")), 15000);
        const fail = (error) => { clearTimeout(timer); reject(error); };
        onAbort = () => fail(new DOMException("Recording cancelled.", "AbortError"));
        signal?.addEventListener("abort", onAbort, { once: true });
        if (signal?.aborted) { onAbort(); return; }
        node.onprocessorerror = () => fail(new Error("Microphone audio processing failed."));
        node.port.onmessage = ({ data }) => {
          if (!(data.samples instanceof Float32Array) || position + data.samples.length > frames) {
            fail(new Error("Invalid microphone capture."));
            return;
          }
          captured.set(data.samples, position);
          position += data.samples.length;
          if (data.done) {
            clearTimeout(timer);
            if (position !== frames) reject(new Error("Incomplete microphone capture."));
            else resolve(captured);
          }
        };
        source.connect(node);
        node.connect(context.destination); // The worklet outputs silence, not microphone audio.
      });
      const bytes = new ArrayBuffer(samples.length * 4);
      const view = new DataView(bytes);
      samples.forEach((value, index) => view.setFloat32(index * 4, value, true));
      return { bytes, sampleRate };
    } finally {
      if (onAbort) signal?.removeEventListener("abort", onAbort);
      stream?.getTracks().forEach((track) => track.stop());
      node?.disconnect();
      source?.disconnect();
      if (context && context.state !== "closed") await context.close();
    }
  },
};
