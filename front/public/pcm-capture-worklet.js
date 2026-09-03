class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.frameSize = Math.max(128, options.processorOptions?.frameSize || 1600)
    this.buffer = new Float32Array(this.frameSize)
    this.offset = 0
  }

  process(inputs) {
    const channel = inputs[0]?.[0]
    if (!channel) return true
    let sourceOffset = 0
    while (sourceOffset < channel.length) {
      const length = Math.min(channel.length - sourceOffset, this.buffer.length - this.offset)
      this.buffer.set(channel.subarray(sourceOffset, sourceOffset + length), this.offset)
      sourceOffset += length
      this.offset += length
      if (this.offset === this.buffer.length) {
        const frame = this.buffer
        this.port.postMessage(frame, [frame.buffer])
        this.buffer = new Float32Array(this.frameSize)
        this.offset = 0
      }
    }
    return true
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor)
