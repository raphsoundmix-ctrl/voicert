// PCM ring buffer between the network thread and the audio thread.
//
// Writer: network thread pushes 16-bit mono PCM at the bridge rate (16 kHz).
// Reader: audio thread pulls float samples at the engine's output rate,
// linearly resampled, and fans mono out to N interleaved channels.
// Flush() is the barge-in path: it must be instant, which is why the
// buffer is a plain ring under a short lock rather than a queue of arrays.

using System;

namespace VoiceRT.Core
{
    public sealed class PcmRingBuffer
    {
        private readonly short[] _buf;
        private readonly object _lock = new object();
        private int _read;      // index of next sample to read
        private int _count;     // samples available
        private double _phase;  // fractional read position between samples
        public long Underruns { get; private set; }
        public long Overruns { get; private set; }

        public int Capacity => _buf.Length;
        public int Available { get { lock (_lock) return _count; } }

        public PcmRingBuffer(int capacitySamples)
        {
            if (capacitySamples < 16) throw new ArgumentOutOfRangeException(nameof(capacitySamples));
            _buf = new short[capacitySamples];
        }

        /// <summary>Append little-endian PCM16 bytes (the bridge's AUDIO_OUT payload).</summary>
        public void WritePcm16(byte[] pcm, int offset, int byteCount)
        {
            int samples = byteCount / 2;
            lock (_lock)
            {
                for (int i = 0; i < samples; i++)
                {
                    short s = (short)(pcm[offset + 2 * i] | (pcm[offset + 2 * i + 1] << 8));
                    if (_count == _buf.Length)
                    {
                        // overwrite oldest: keeps latency bounded if the engine stalls
                        _read = (_read + 1) % _buf.Length;
                        _count--;
                        Overruns++;
                    }
                    _buf[(_read + _count) % _buf.Length] = s;
                    _count++;
                }
            }
        }

        public void WritePcm16(byte[] pcm) => WritePcm16(pcm, 0, pcm.Length);

        /// <summary>Drop everything queued. Called on FLUSH (barge-in).</summary>
        public void Clear()
        {
            lock (_lock)
            {
                _read = 0; _count = 0; _phase = 0;
            }
        }

        /// <summary>
        /// Fill <paramref name="dst"/> (interleaved, <paramref name="channels"/> wide) with
        /// resampled audio. Frames that cannot be served are written as silence and
        /// counted as underruns. Returns the number of frames actually served.
        /// </summary>
        public int ReadResampled(float[] dst, int channels, int srcRate, int dstRate)
        {
            if (channels < 1) throw new ArgumentOutOfRangeException(nameof(channels));
            int frames = dst.Length / channels;
            double step = (double)srcRate / dstRate;
            int served = 0;
            lock (_lock)
            {
                for (int f = 0; f < frames; f++)
                {
                    if (_count == 0)
                    {
                        for (int c = 0; c < channels; c++) dst[f * channels + c] = 0f;
                        Underruns++;
                        continue;
                    }
                    float a = _buf[_read] / 32768f;
                    // With only one sample left there is nothing to interpolate
                    // towards: hold it, then consume it. Otherwise the final
                    // sample of every utterance would sit in the ring forever.
                    float b = _count > 1 ? _buf[(_read + 1) % _buf.Length] / 32768f : a;
                    float v = a + (b - a) * (float)_phase;
                    for (int c = 0; c < channels; c++) dst[f * channels + c] = v;
                    served++;

                    _phase += step;
                    while (_phase >= 1.0 && _count > 0)
                    {
                        _phase -= 1.0;
                        _read = (_read + 1) % _buf.Length;
                        _count--;
                    }
                    if (_count == 0) _phase = 0;
                }
            }
            return served;
        }
    }
}
