// PCM ring buffer between the network thread and the audio thread.
//
// Writer: the VoiceRT reader thread pushes 16-bit mono PCM at the bridge rate.
// Readers, pick exactly one per instance:
//   * ReadPcm16     - FMOD mixer thread. Raw PCM16, NO rate conversion: the user
//                     sound is declared at the bridge rate and FMOD's channel
//                     resampler takes it to the mixer rate. This is also what
//                     makes pitch and doppler correct, because FMOD then asks
//                     for exactly as many source samples as it needs.
//   * ReadResampled - Unity audio thread (the plain AudioSource path).
//
// Realtime contract: the lock is held for a bounded, allocation-free span. The
// writer copies in two BlockCopy segments rather than looping per sample, because
// the real TTS emits one AudioFrame per whole utterance - an 80,000-sample write
// is normal, and the mixer thread blocks on this same lock.
//
// Barge-in has two entry points on purpose:
//   BeginBargeIn() - local, main thread, fired before the round trip. Also
//                    SUPPRESSES writes, because the server has already put
//                    pre-interrupt audio on the wire and the reader thread is
//                    about to deliver it.
//   EndBargeIn()   - the server's FLUSH, reader thread. The real cut point.

using System;
using System.Threading;

namespace VoiceRT.Core
{
    public sealed class PcmRingBuffer
    {
        private readonly short[] _buf;
        private readonly object _lock = new object();
        private int _read;       // index of next sample to read
        private int _count;      // samples available
        private double _phase;   // fractional read position (ReadResampled only)
        private bool _suppress;  // barge-in in flight: drop incoming writes
        private long _underruns;
        private long _overruns;
        private long _suppressed;

        public int Capacity => _buf.Length;
        public int Available { get { lock (_lock) return _count; } }
        public bool BargeInPending { get { lock (_lock) return _suppress; } }

        // Interlocked.Read, not a plain field read: these are written under the lock
        // by one thread and read without it by another.
        public long Underruns => Interlocked.Read(ref _underruns);
        public long Overruns => Interlocked.Read(ref _overruns);
        public long SuppressedSamples => Interlocked.Read(ref _suppressed);

        public PcmRingBuffer(int capacitySamples)
        {
            if (capacitySamples < 16) throw new ArgumentOutOfRangeException(nameof(capacitySamples));
            _buf = new short[capacitySamples];
        }

        // -- writer (network reader thread) --------------------------------

        /// <summary>Append little-endian PCM16 bytes (the bridge's AUDIO_OUT payload).</summary>
        public void WritePcm16(byte[] pcm, int offset, int byteCount)
        {
            if (pcm == null) return;
            int samples = byteCount / 2;
            if (samples <= 0) return;

            lock (_lock)
            {
                if (_suppress) { _suppressed += samples; return; }

                // A single write larger than the whole ring: keep only its tail.
                if (samples > _buf.Length)
                {
                    int skip = samples - _buf.Length;
                    offset += skip * 2;
                    samples = _buf.Length;
                    _overruns += skip;
                }

                // Drop the oldest in ONE step, not once per sample. Keeps latency bounded
                // if FMOD stalls, at the cost of the oldest audio - which is the right
                // trade for a live voice.
                int overflow = _count + samples - _buf.Length;
                if (overflow > 0)
                {
                    _read = (_read + overflow) % _buf.Length;
                    _count -= overflow;
                    _overruns += overflow;
                    _phase = 0;
                }

                int w = (_read + _count) % _buf.Length;
                int first = Math.Min(samples, _buf.Length - w);
                CopyLe(pcm, offset, _buf, w, first);
                if (samples > first) CopyLe(pcm, offset + first * 2, _buf, 0, samples - first);
                _count += samples;
            }
        }

        public void WritePcm16(byte[] pcm) => WritePcm16(pcm, 0, pcm == null ? 0 : pcm.Length);

        private static void CopyLe(byte[] src, int srcByteOffset, short[] dst, int dstIndex, int samples)
        {
            if (samples <= 0) return;
            if (BitConverter.IsLittleEndian)
            {
                // One memcpy. Every Unity target that matters is little-endian, so this
                // is the path that actually runs; the loop below is the correctness net.
                Buffer.BlockCopy(src, srcByteOffset, dst, dstIndex * 2, samples * 2);
                return;
            }
            for (int i = 0; i < samples; i++)
                dst[dstIndex + i] = (short)(src[srcByteOffset + 2 * i] | (src[srcByteOffset + 2 * i + 1] << 8));
        }

        // -- barge-in ------------------------------------------------------

        /// <summary>Drop everything queued. Safe from any thread.</summary>
        public void Clear()
        {
            lock (_lock) { _read = 0; _count = 0; _phase = 0; }
        }

        /// <summary>MAIN THREAD. Local barge-in: drop the queue and refuse further writes
        /// until the server's FLUSH confirms the cut. Without the suppression the NPC
        /// audibly resumes talking while the socket drains pre-interrupt audio.</summary>
        public void BeginBargeIn()
        {
            lock (_lock) { _read = 0; _count = 0; _phase = 0; _suppress = true; }
        }

        /// <summary>READER THREAD. The server's FLUSH landed. Everything before it is
        /// already written, everything after it is new, so this is safe and ordered.</summary>
        public void EndBargeIn()
        {
            lock (_lock) { _read = 0; _count = 0; _phase = 0; _suppress = false; }
        }

        // -- readers -------------------------------------------------------

        /// <summary>
        /// FMOD MIXER THREAD. Copy <paramref name="count"/> samples at the SOURCE rate.
        /// Shortfall is written as silence and counted as underruns - never an error,
        /// because the stream must outlive any single utterance.
        /// Returns the number of real (non-silence) samples served.
        /// </summary>
        public int ReadPcm16(short[] dst, int offset, int count)
        {
            if (dst == null || count <= 0) return 0;
            int served;
            lock (_lock)
            {
                served = Math.Min(count, _count);
                if (served > 0)
                {
                    int first = Math.Min(served, _buf.Length - _read);
                    Array.Copy(_buf, _read, dst, offset, first);
                    if (served > first) Array.Copy(_buf, 0, dst, offset + first, served - first);
                    _read = (_read + served) % _buf.Length;
                    _count -= served;
                }
                int missing = count - served;
                if (missing > 0) { _underruns += missing; _phase = 0; }
            }
            // Zero-fill outside the lock: the writer must not wait on our memset.
            if (count - served > 0) Array.Clear(dst, offset + served, count - served);
            return served;
        }

        /// <summary>
        /// UNITY AUDIO THREAD (plain AudioSource path). Fill <paramref name="dst"/>
        /// (interleaved, <paramref name="channels"/> wide) with linearly resampled audio.
        /// Unused by the FMOD sink, which lets FMOD resample instead.
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
                        _underruns++;
                        continue;
                    }
                    float a = _buf[_read] / 32768f;
                    // With only one sample left there is nothing to interpolate towards:
                    // hold it, then consume it, or the final sample of every utterance
                    // would sit in the ring forever.
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
