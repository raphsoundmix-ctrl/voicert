// The original AudioSource path, extracted from VoiceRTNpc so it and the FMOD
// sink are interchangeable. Behaviour is unchanged: a streaming AudioClip whose
// PCMReaderCallback pulls from a PcmRingBuffer, resampled from the bridge rate to
// Unity's output rate. Unity's own spatializer, mixer groups, reverb zones and
// occlusion apply untouched.

using UnityEngine;
using VoiceRT.Core;

namespace VoiceRT
{
    [AddComponentMenu("VoiceRT/VoiceRT AudioSource Sink")]
    [RequireComponent(typeof(AudioSource))]
    [DisallowMultipleComponent]
    public sealed class VoiceRTAudioSourceSink : MonoBehaviour, IVoiceRTAudioSink
    {
        [Tooltip("Seconds of voice held between the network and the audio thread. A GPU " +
                 "voice is synthesized far faster than it is spoken — a nine-second reply " +
                 "can arrive in under a second — so this has to hold a whole reply, not " +
                 "just the mixer's lookahead. It costs 48 KB per second per NPC.")]
        [Range(0.25f, 60f)] public float bufferSeconds = 30f;

        private PcmRingBuffer _ring;
        private AudioSource _source;
        private AudioClip _clip;
        private int _outputRate;
        private int _sourceRate = 16000;

        public long Underruns => _ring == null ? 0 : _ring.Underruns;
        public long Overruns => _ring == null ? 0 : _ring.Overruns;
        public int BufferedMilliseconds =>
            _ring == null ? 0 : (int)(1000L * _ring.Available / Mathf.Max(1, _sourceRate));

        private void Awake()
        {
            _source = GetComponent<AudioSource>();
            _outputRate = AudioSettings.outputSampleRate;
        }

        public void Configure(int sourceSampleRate)
        {
            _sourceRate = sourceSampleRate <= 0 ? 16000 : sourceSampleRate;
            int capacity = Mathf.CeilToInt(_sourceRate * bufferSeconds);
            if (_ring == null || _ring.Capacity != capacity) _ring = new PcmRingBuffer(capacity);
        }

        public void Begin()
        {
            if (_ring == null) Configure(_sourceRate);
            if (_source.isPlaying) return;
            // A streaming clip loops over a small window and keeps asking the callback
            // for more samples, which is exactly what a live voice needs.
            _clip = AudioClip.Create($"VoiceRT-{name}", _outputRate, 1, _outputRate, true, OnPcmRead);
            _source.clip = _clip;
            _source.loop = true;
            _source.Play();
        }

        public void Write(byte[] pcm16le, int offset, int count) => _ring?.WritePcm16(pcm16le, offset, count);
        public void Write(byte[] pcm16le) => _ring?.WritePcm16(pcm16le);

        public void Flush() => _ring?.EndBargeIn();
        public void BargeIn() => _ring?.BeginBargeIn();

        public void End()
        {
            if (_source != null && _source.isPlaying) _source.Stop();
            _ring?.EndBargeIn();   // clears the queue AND lifts any pending suppression
            // A streaming clip is created per Begin(); without this every
            // walk-up-and-away cycle leaks one until the next scene load.
            if (_source != null) _source.clip = null;
            if (_clip != null) { Destroy(_clip); _clip = null; }
        }

        private void OnPcmRead(float[] data)
        {
            // Unity audio thread. Mono clip, so channels == 1; Unity spatializes after.
            _ring?.ReadResampled(data, 1, _sourceRate, _outputRate);
        }
    }
}
