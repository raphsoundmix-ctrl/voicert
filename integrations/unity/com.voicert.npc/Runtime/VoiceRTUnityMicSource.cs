// The fallback capture backend: UnityEngine.Microphone.
//
// This is the code that used to live inside VoiceRTVoiceInput, lifted out
// unchanged in behaviour. It stays in the package because it is the only backend
// that needs nothing installed, and it is the FALLBACK rather than the primary
// because it has a failure mode nothing here can fix: when Unity's audio system
// comes up without a recording backend, Microphone.devices is an empty array for
// the rest of the process and Microphone.Start returns null for every device,
// including ones the OS is happily listing. Reopening does not help;
// AudioSettings.Reset does not help. FMOD, in the same process, enumerates all
// eight of them.
//
// Everything else — resampling, mixdown, metering, the silence verdict — belongs
// to VoiceRTVoiceInput and is deliberately absent here.

using System;
using UnityEngine;

namespace VoiceRT
{
    public sealed class VoiceRTUnityMicSource : IVoiceRTMicSource
    {
        /// <summary>Seconds of loop buffer to ask the device for.
        ///
        /// One second was the original value and it is a trap: the recorder keeps
        /// writing while the game hitches, and the moment the writer laps the
        /// reader the next read returns a mixture of two sentences. Ten seconds
        /// costs 320 kB and turns "a frame took too long" into a non-event.</summary>
        private const int LoopSeconds = 10;

        private AudioClip _clip;
        private string _opened;
        private int _rate;
        private int _readPos;

        public string BackendName => "Unity";
        public bool IsOpen => _clip != null;
        public string OpenedDevice => _opened;
        public int SampleRate => _rate;
        public int Channels => _clip != null ? _clip.channels : 0;
        public bool IsFaulted { get; private set; }

        // UnityEngine.Microphone exposes no metering of its own: the only level
        // available is the one the caller computes from the samples it was handed.
        public float BackendPeak => -1f;
        public string BackendMeter => null;

        public string[] Devices
        {
            get
            {
                // Unity exposes no way to tell a capsule from a loopback, so the
                // shared name heuristic is all there is.
                try { return VoiceRTMicNames.Prefer(Microphone.devices); }
                catch (Exception) { return Array.Empty<string>(); }
            }
        }

        public bool Open(string device)
        {
            Close();
            IsFaulted = false;

            string[] present;
            try { present = Microphone.devices; }
            catch (Exception e)
            {
                Debug.LogWarning($"[VoiceRT] Unity capture: enumerating devices threw {e.GetType().Name}.");
                IsFaulted = true;
                return false;
            }

            if (present.Length == 0)
            {
                // Not "no microphone is plugged in" — the OS may be listing several.
                // Unity's own recording backend is simply not up in this process.
                IsFaulted = true;
                return false;
            }

            string name = string.IsNullOrEmpty(device) ? null : device;
            if (name != null && Array.IndexOf(present, name) < 0) return false;

            // A device that cannot record at 16 kHz reports its own range, and
            // Microphone.Start silently substitutes a rate it can do. Asking for a
            // supported one keeps the substitution predictable; the caller resamples
            // whatever comes back either way.
            Microphone.GetDeviceCaps(name, out int min, out int max);
            int rate = VoiceRTVoiceInput.TargetRate;
            if (min > 0 && rate < min) rate = min;
            if (max > 0 && rate > max) rate = max;

            try { _clip = Microphone.Start(name, true, LoopSeconds, rate); }
            catch (Exception) { _clip = null; }
            if (_clip == null) return false;

            _opened = name;
            _rate = _clip.frequency;
            _readPos = Microphone.GetPosition(_opened);
            if (_readPos < 0) _readPos = 0;
            return true;
        }

        public void Close()
        {
            // `device` may have changed since Start; ending the wrong one leaves the
            // real capture running forever.
            if (_clip != null)
            {
                try { Microphone.End(_opened); } catch (Exception) { /* already gone */ }
            }
            _clip = null;
            _opened = null;
            _rate = 0;
            _readPos = 0;
        }

        public void DropPending()
        {
            if (_clip == null) return;
            int live = Microphone.GetPosition(_opened);
            if (live >= 0) _readPos = live;
        }

        public int Read(ref float[] into)
        {
            if (_clip == null) return 0;

            int pos = Microphone.GetPosition(_opened);
            // A driver can stall: the device opens and the read position never moves.
            // That is silence, not an error — the caller's dead-input timer decides.
            if (pos < 0 || pos == _readPos) return 0;

            int total = _clip.samples;                       // frames, not samples
            int frames = pos >= _readPos ? pos - _readPos : (total - _readPos) + pos;
            if (frames <= 0) return 0;

            // More than most of the buffer means the recorder lapped us while the
            // main thread was elsewhere. Whatever is in there now is part of some
            // earlier sentence; sending it answers the wrong question.
            if (frames >= total - total / 8)
            {
                _readPos = pos;
                return -frames;
            }

            int channels = _clip.channels;
            int wanted = frames * channels;
            // GetData fills the whole array, so the array can never be longer than the
            // clip: one long frame would otherwise grow it past the loop and Unity
            // would warn on every read after that, forever.
            int cap = total * channels;
            if (into == null || into.Length < wanted || into.Length > cap)
                into = new float[Mathf.Min(Mathf.NextPowerOfTwo(wanted), cap)];

            // GetData wraps around the loop clip, so one read covers the split.
            _clip.GetData(into, _readPos);
            _readPos = pos;
            return Mathf.Min(wanted, into.Length);
        }
    }
}
