// One microphone, one listener: the player's voice, streamed to whichever NPC
// they are actually talking to.
//
// This replaces the old per-NPC VoiceRTMicrophone. A component that required a
// VoiceRTNpc on the same object meant every character in earshot opened the
// device and streamed the same audio, and the server then ran a VAD per NPC on
// identical samples. A player has one mouth; this has one microphone, and a
// Target the game points at the character being spoken to.
//
// It deliberately reads no keys. "Active Input Handling" differs per project and
// the package must not force a dependency on either input backend, so the game
// calls BeginUtterance/EndUtterance from whatever it uses.
//
// Feedback is the thing that decides the default. An open microphone next to a
// speaker hears the NPC, the server's VAD calls that a player talking, and the
// NPC barges in on itself for as long as you let it. Push-to-talk cannot do
// that, so push-to-talk is the default; open-mic barge-in is available and
// wants headphones.
//
// CAPTURE BACKEND. This component no longer talks to UnityEngine.Microphone
// directly. It drives an IVoiceRTMicSource, and it tries FMOD first — the product
// is Unity + FMOD first, and more concretely, Unity's recording backend is the
// less reliable of the two on the machines this has been run on. Two failures,
// both measured, both of which this file now handles rather than reports:
//
//   * Unity's Microphone.devices returned an EMPTY array, and Microphone.Start
//     returned null for every device, while the OS listed four active capture
//     endpoints and FMOD in the same process enumerated eight. Nothing short of
//     restarting the editor brought it back.
//
//   * The endpoint Windows calls "default" was a virtual loopback that opened
//     cleanly and recorded 72000 frames at a peak of exactly zero, while the
//     physical capsule beside it in the list was held by another process and
//     returned ERR_RECORD. The only endpoint carrying audio was a third one.
//
// So "it opened" is not evidence that it works, and neither is "the system says
// this one is default". The device is judged by whether audio actually arrives,
// and when it does not, this walks to the next candidate on its own.

using System;
using System.Collections.Generic;
using UnityEngine;

namespace VoiceRT
{
    public enum VoiceRTMicMode
    {
        /// <summary>Capture only between BeginUtterance and EndUtterance.</summary>
        PushToTalk,
        /// <summary>Always capture; the server's VAD finds the utterances.</summary>
        OpenMic,
    }

    public enum VoiceRTMicBackendMode
    {
        /// <summary>Registered backends first (FMOD when present), Unity last.</summary>
        Auto,
        /// <summary>FMOD only. Fails loudly rather than falling back — for proving
        /// the FMOD path works, which a silent fallback would hide.</summary>
        FmodOnly,
        /// <summary>UnityEngine.Microphone only.</summary>
        UnityOnly,
    }

    [AddComponentMenu("VoiceRT/VoiceRT Voice Input")]
    [DisallowMultipleComponent]
    public sealed class VoiceRTVoiceInput : MonoBehaviour
    {
        /// <summary>The bridge wants 16 kHz mono; anything else is resampled here.</summary>
        public const int TargetRate = 16000;

        [Tooltip("Empty = choose automatically. Automatic does NOT mean the system " +
                 "default: the system default is regularly a silent virtual device.")]
        public string device = "";

        [Tooltip("Which capture backend to use. FMOD is the primary path; Unity is " +
                 "the fallback for projects with no FMOD installed.")]
        public VoiceRTMicBackendMode backendMode = VoiceRTMicBackendMode.Auto;

        [Tooltip("A device that opens but delivers nothing is a dead device. Move to " +
                 "the next candidate instead of waiting for the player to notice.")]
        public bool autoAdvanceOnSilence = true;

        [Tooltip("Push-to-talk cannot feed the NPC's own voice back into the microphone. " +
                 "Open mic can, unless the player is on headphones.")]
        public VoiceRTMicMode mode = VoiceRTMicMode.PushToTalk;

        [Tooltip("Open mic only: let the player talk over the NPC. Needs headphones — " +
                 "on speakers the microphone hears the NPC and interrupts it constantly.")]
        public bool allowBargeIn = false;

        [Tooltip("Open mic only: how loud the room must get before audio is sent while the " +
                 "NPC is speaking. Above the NPC's own level in the room, below the player's.")]
        [Range(0f, 0.5f)] public float bargeInThreshold = 0.08f;

        [Tooltip("Milliseconds of audio per packet. Smaller reacts sooner and costs more syscalls.")]
        [Range(10, 100)] public int chunkMs = 30;

        /// <summary>Who hears this microphone. Set by whatever picks the active NPC.</summary>
        public VoiceRTNpc Target { get; set; }

        /// <summary>Loudest sample in the last chunk, 0..1 — a level meter for the UI.</summary>
        public float Level { get; private set; }

        /// <summary>RMS of the last chunk, 0..1. Peak says "something happened";
        /// this says how loud it actually is, which is what a person reads.</summary>
        public float Rms { get; private set; }

        /// <summary>The last chunk's peak in dBFS, floored at -100. A capsule in a
        /// quiet room sits around -50; a dead input reads the floor.</summary>
        public float PeakDb => Level > 1e-5f ? 20f * Mathf.Log10(Level) : -100f;

        /// <summary>Everything below this counts as a dead input (dBFS).</summary>
        public static float SilenceFloorDb => 20f * Mathf.Log10(SilentPeak);

        /// <summary>Whether audio is being sent right now.</summary>
        public bool IsCapturing { get; private set; }

        /// <summary>The device has been open for a moment and nothing above the
        /// noise floor of a dead input has ever arrived through it.
        ///
        /// Deliberately not gated on push-to-talk: a player who hears nothing needs
        /// to know the microphone is dead *before* they hold the key and talk to a
        /// character for ten seconds. Any real signal clears it for as long as the
        /// device stays open.</summary>
        public bool IsSilent { get; private set; }

        public bool HasMicrophone => _source != null && _source.IsOpen;

        /// <summary>The device's own rate. 0 until the microphone starts.</summary>
        public int DeviceRate => _source != null ? _source.SampleRate : 0;

        /// <summary>Which device is actually open — null means the backend chose.
        /// The name asked for and the name opened differ whenever the first choice
        /// will not start, so this is the one worth showing.</summary>
        public string OpenedDevice => _source != null ? _source.OpenedDevice : null;

        /// <summary>The open device as a person would name it.</summary>
        public string OpenedDeviceLabel =>
            !HasMicrophone ? "(none)" : (_source.OpenedDevice ?? "default device");

        /// <summary>Which backend is actually carrying the audio — "FMOD" or "Unity".
        /// Worth showing: the two fail in completely different ways.</summary>
        public string Backend => _source != null ? _source.BackendName : "(none)";

        /// <summary>How many candidates were rejected before this one took. Non-zero
        /// means the obvious choice was wrong, which is worth saying out loud.</summary>
        public int DevicesRejected { get; private set; }

        /// <summary>Peak the AUDIO ENGINE measured, 0..1, or negative when the backend
        /// has no meter. Distinct from Level, which this component computes by scanning
        /// the samples it was given — when the two disagree, the disagreement is the
        /// diagnosis.</summary>
        public float BackendPeak => _source != null ? _source.BackendPeak : -1f;

        /// <summary>BackendPeak in dBFS, floored at -100. Negative infinity is reported
        /// as the floor so a meter can draw it.</summary>
        public float BackendPeakDb
        {
            get
            {
                float p = BackendPeak;
                if (p < 0f) return float.NaN;          // no backend meter at all
                return p > 1e-5f ? 20f * Mathf.Log10(p) : -100f;
            }
        }

        /// <summary>Where the engine is measuring — e.g. "bus:/VoiceInput". Null when
        /// the backend has no meter of its own.</summary>
        public string BackendMeter => _source != null ? _source.BackendMeter : null;

        /// <summary>The audio engine itself confirms signal. This is the check that
        /// does not depend on our own sample scan.</summary>
        public bool BackendHasSignal => BackendPeak > SilentPeak;

        /// <summary>Where the chosen device is remembered between runs. The game
        /// cannot change a Windows default and should not try; it can remember
        /// which input the player told it to use.</summary>
        public const string DevicePreferenceKey = "VoiceRT.InputDevice";

        // -- capture state ----------------------------------------------------

        private readonly struct Candidate
        {
            public readonly IVoiceRTMicSource Source;
            public readonly string Device;
            public Candidate(IVoiceRTMicSource source, string device) { Source = source; Device = device; }
        }

        /// <summary>Devices that opened this session and carried nothing. Skipped on
        /// later passes so the walk converges instead of cycling through the same duds.
        /// Cleared whenever a human explicitly asks for a retry.</summary>
        private readonly HashSet<string> _deadThisSession = new HashSet<string>();

        /// <summary>The open device has not yet carried audio, so it is not worth
        /// writing to disk. See ProveDevice.</summary>
        private bool _pinUnproven;

        private readonly List<Candidate> _candidates = new List<Candidate>();
        private readonly List<IVoiceRTMicSource> _sources = new List<IVoiceRTMicSource>();
        private int _at = -1;                  // index into _candidates
        private IVoiceRTMicSource _source;

        private float[] _scratch = Array.Empty<float>();
        private readonly List<float> _pending = new List<float>(4096);
        private readonly List<byte> _outbox = new List<byte>(8192);
        private double _phase;
        private bool _talking;
        private bool _warned;
        private float _nextDeviceAttempt;
        private float _deviceOpenedAt;         // Time.unscaledTime when the device opened
        private float _loudestSinceOpen;       // the verdict is about the device, not the key
        private bool _announcedSilent;

        /// <summary>Silence for this long while the gate is open is a dead device,
        /// not a quiet player.</summary>
        private const float SilentAfterSeconds = 1.5f;

        /// <summary>-60 dBFS. A capsule in a room sits well above this; a virtual or
        /// unplugged input reads zero or a single LSB of dither (3e-5), and a
        /// microphone quieter than this could not open the server's VAD anyway.</summary>
        private const float SilentPeak = 0.001f;

        /// <summary>How often to try opening a device again while there is none.</summary>
        private const float DeviceRetrySeconds = 2f;

        // -- lifecycle ------------------------------------------------------

        private void OnEnable()
        {
            // A device chosen in a previous session outranks the inspector's
            // default, which is empty ("choose for me").
            if (string.IsNullOrEmpty(device))
                device = PlayerPrefs.GetString(DevicePreferenceKey, "");
            StartDevice();
        }

        private void OnDisable()
        {
            StopDevice();
            _talking = false;
            IsCapturing = false;
            Level = 0f;
            Rms = 0f;
        }

        /// <summary>Use this recording device from now on, and remember it.
        /// An empty name means "choose for me".</summary>
        public void SelectDevice(string deviceName)
        {
            device = deviceName ?? "";
            // Deliberately NOT written to disk here. A device earns that by carrying
            // audio (ProveDevice); writing it on the click is what pins a dud across
            // restarts, where it then outranks the automatic ranking forever.
            _deadThisSession.Clear();      // an explicit ask is a fresh start
            Restart();
        }

        /// <summary>Move to the next candidate device, across every backend.
        /// One key the player can press when they hear nothing.</summary>
        public void CycleDevice()
        {
            bool wasTalking = _talking;
            if (_candidates.Count == 0) BuildCandidates();
            if (_candidates.Count == 0) { Restart(); return; }

            // Set for this session; remembered only once it proves it carries audio.
            if (TryOpenFrom(_at + 1, wrap: true, announce: true))
                device = _source.OpenedDevice ?? "";
            _talking = wasTalking && HasMicrophone;
        }

        /// <summary>Close and reopen — after changing the device, or after the
        /// player has fixed something in the system settings.</summary>
        public void Restart()
        {
            bool wasTalking = _talking;
            StopDevice();
            _deadThisSession.Clear();    // a human asking again means try everything
            _warned = false;             // a new device deserves a fresh complaint
            _nextDeviceAttempt = 0f;
            StartDevice();
            _talking = wasTalking && HasMicrophone;
        }

        // -- device selection --------------------------------------------------

        /// <summary>Every (backend, device) pair worth trying, best first.
        ///
        /// Flat across backends on purpose: "the next device" should keep walking
        /// into Unity's list once FMOD's is exhausted, rather than stopping at a
        /// backend boundary the player cannot see.</summary>
        private void BuildCandidates()
        {
            _candidates.Clear();
            _sources.Clear();

            foreach (var source in VoiceRTMicBackends.CreateAll())
            {
                bool isUnity = source.BackendName == "Unity";
                if (backendMode == VoiceRTMicBackendMode.UnityOnly && !isUnity) continue;
                if (backendMode == VoiceRTMicBackendMode.FmodOnly && isUnity) continue;
                _sources.Add(source);
            }

            // An explicit choice goes first, on whichever backend will take it.
            if (!string.IsNullOrEmpty(device))
                foreach (var source in _sources)
                    _candidates.Add(new Candidate(source, device));

            foreach (var source in _sources)
            {
                string[] devices;
                try { devices = source.Devices; }
                catch (Exception) { devices = Array.Empty<string>(); }

                foreach (string name in devices)
                {
                    if (!string.IsNullOrEmpty(device) && name == device) continue;   // already first
                    _candidates.Add(new Candidate(source, name));
                }
                // A backend that enumerates nothing still gets one shot at its own
                // idea of a default, rather than being skipped silently.
                if (devices.Length == 0) _candidates.Add(new Candidate(source, null));
            }
        }

        private void StartDevice()
        {
            // Opening is retried from Update() when a device appears; without a
            // gate that is one attempt per device per frame, each with a warning.
            _nextDeviceAttempt = Time.unscaledTime + DeviceRetrySeconds;
            BuildCandidates();
            _at = -1;
            DevicesRejected = 0;

            if (_candidates.Count == 0)
            {
                if (!_warned)
                {
                    _warned = true;
                    Debug.LogWarning("[VoiceRT] no recording device on any backend; the voice input is idle.");
                }
                return;
            }

            if (!TryOpenFrom(0, wrap: false, announce: true) && !_warned)
            {
                _warned = true;
                Debug.LogWarning($"[VoiceRT] none of {_candidates.Count} recording candidate(s) would open; " +
                                 "the voice input is idle. Backends tried: " +
                                 string.Join(", ", BackendNames()) + ".");
            }
        }

        private string[] BackendNames()
        {
            var names = new string[_sources.Count];
            for (int i = 0; i < _sources.Count; i++) names[i] = _sources[i].BackendName;
            return names;
        }

        /// <summary>Open the first candidate at or after <paramref name="from"/> that
        /// will actually start. Returns false when every one of them refused.</summary>
        private bool TryOpenFrom(int from, bool wrap, bool announce)
        {
            if (_candidates.Count == 0) return false;
            StopDevice();

            int count = _candidates.Count;
            int limit = wrap ? count : count - Mathf.Clamp(from, 0, count);
            for (int step = 0; step < Mathf.Max(limit, 0); step++)
            {
                int i = wrap ? ((from + step) % count + count) % count : from + step;
                if (i < 0 || i >= count) break;

                var candidate = _candidates[i];
                // Already opened this session and carried nothing. Trying it again
                // just burns 1.5 s of the player's time to reach the same verdict.
                if (candidate.Device != null && _deadThisSession.Contains(candidate.Device)) continue;
                bool opened;
                try { opened = candidate.Source.Open(candidate.Device); }
                catch (Exception e)
                {
                    Debug.LogWarning($"[VoiceRT] {candidate.Source.BackendName} capture threw " +
                                     $"{e.GetType().Name} opening '{candidate.Device ?? "default"}'.");
                    opened = false;
                }
                if (!opened) { DevicesRejected++; continue; }

                _source = candidate.Source;
                _at = i;
                OnDeviceOpened(announce);
                return true;
            }
            return false;
        }

        private void OnDeviceOpened(bool announce)
        {
            _pending.Clear();
            _outbox.Clear();
            _phase = 0;
            _lowPass.Configure(_source.SampleRate, TargetRate);
            IsSilent = false;
            _announcedSilent = false;
            _deviceOpenedAt = Time.unscaledTime;
            _loudestSinceOpen = 0f;
            _pinUnproven = true;         // remembered only once it carries audio
            _warned = false;             // it works now; say so again if it stops

            if (!announce) return;
            // Say which backend and which device, every time: a wrong device looks
            // exactly like a muted one, and a fallback to Unity looks exactly like
            // FMOD working until you read this line.
            string resample = _source.SampleRate != TargetRate ? $" -> {TargetRate}" : "";
            string mixdown = _source.Channels > 1 ? $", {_source.Channels}ch -> mono" : "";
            string skipped = DevicesRejected > 0 ? $" (after {DevicesRejected} that would not open)" : "";
            Debug.Log($"[VoiceRT] microphone: {OpenedDeviceLabel} via {_source.BackendName} " +
                      $"at {_source.SampleRate} Hz{resample}{mixdown}{skipped}.");
        }

        /// <summary>The open device has carried real audio, so it is finally worth
        /// remembering across runs.
        ///
        /// Persisting on selection instead of on proof is what wedged this before:
        /// clicking "Next device" onto a dud wrote that dud to PlayerPrefs, and an
        /// explicitly remembered device is tried FIRST on every later run — ahead of
        /// the ranking that would have picked a working one.</summary>
        private void ProveDevice()
        {
            _pinUnproven = false;
            string proven = _source?.OpenedDevice ?? "";
            if (PlayerPrefs.GetString(DevicePreferenceKey, "") == proven) return;
            PlayerPrefs.SetString(DevicePreferenceKey, proven);
            PlayerPrefs.Save();
            Debug.Log($"[VoiceRT] remembering {OpenedDeviceLabel} — it carries audio.");
        }

        private void StopDevice()
        {
            if (_source != null)
            {
                try { _source.Close(); } catch (Exception) { /* already gone */ }
            }
            _source = null;
        }

        /// <summary>The open device delivers nothing. Walk to the next candidate.
        /// This is the recovery for both measured failures: a virtual default that
        /// records digital silence, and a capsule another process is holding.</summary>
        private void AdvanceAfterSilence(bool faulted = false)
        {
            int from = _at + 1;
            string dead = OpenedDeviceLabel;
            string backend = Backend;
            string deadName = _source != null ? _source.OpenedDevice : null;
            if (!string.IsNullOrEmpty(deadName)) _deadThisSession.Add(deadName);

            // A remembered device that no longer carries audio must stop being
            // remembered. Otherwise every later run starts on it, and because an
            // explicit choice is tried before the ranking, the player stays stuck on
            // it until they clear PlayerPrefs by hand.
            if (!string.IsNullOrEmpty(deadName) && device == deadName)
            {
                Debug.LogWarning($"[VoiceRT] forgetting {dead}: it was the remembered " +
                                 "input and it no longer carries audio.");
                device = "";
                PlayerPrefs.SetString(DevicePreferenceKey, "");
                PlayerPrefs.Save();
            }

            if (from >= _candidates.Count)
            {
                // One full pass, nothing carried audio. Stop churning and say so;
                // the player can still force a device with F9 or the monitor.
                if (!_announcedSilent)
                {
                    _announcedSilent = true;
                    Debug.LogWarning($"[VoiceRT] every recording candidate is silent " +
                                     $"({_candidates.Count} tried). The last was {dead} via {backend}. " +
                                     "Check that nothing else is holding the interface, then " +
                                     "VoiceRT > Microphone monitor.");
                }
                // A device that merely sounds silent is worth keeping open — the
                // player may still be about to speak into it. One that has faulted
                // is not: close it so the retry timer starts a fresh pass, in case
                // whatever was holding it lets go.
                if (faulted) StopDevice();
                return;
            }

            Debug.LogWarning($"[VoiceRT] {dead} via {backend} delivers no usable signal " +
                             $"(peak {_loudestSinceOpen:0.00000}); trying the next input.");
            if (!TryOpenFrom(from, wrap: false, announce: true))
            {
                _announcedSilent = true;
                Debug.LogWarning("[VoiceRT] no further recording candidate would open; the voice input is idle.");
            }
        }

        // -- the game's controls ---------------------------------------------

        /// <summary>The player started talking (push-to-talk pressed).
        /// Cuts the NPC off first: the local flush happens this frame, the
        /// server's FLUSH confirms it a round trip later.</summary>
        public void BeginUtterance()
        {
            if (_talking) return;
            _talking = true;
            // Start from what the microphone is hearing *now*. Anything already in
            // the loop was recorded before the player pressed the key — it belongs
            // to the room, or to the sentence before this one.
            _source?.DropPending();
            _pending.Clear();
            _outbox.Clear();
            _phase = 0;
            _lowPass.Reset();
            if (Target != null && Target.IsSpeaking) Target.Interrupt();
        }

        /// <summary>The player stopped talking. Ends the utterance immediately
        /// instead of waiting for the server's silence timer.</summary>
        public void EndUtterance()
        {
            if (!_talking) return;
            _talking = false;
            Flush();
            if (Target != null) Target.EndUtterance();
        }

        /// <summary>Stop mid-utterance without asking for an answer — used when the
        /// player walks away or the target changes.</summary>
        public void Cancel()
        {
            _talking = false;
            _pending.Clear();
            _outbox.Clear();
            // _phase and the device's own queue matter as much as the buffers: leave
            // them and the next utterance opens with interpolation history from
            // before the cancel, stitched onto its first word.
            _phase = 0;
            _lowPass.Reset();
            _source?.DropPending();
            IsCapturing = false;
        }

        // -- capture ----------------------------------------------------------

        private void Update()
        {
            if (_source == null || !_source.IsOpen)
            {
                // A device may be plugged in later, and FMOD's system needs a few
                // frames to come up before it can enumerate anything at all. Retry
                // on a timer, because "try every device every frame" is a warning
                // per device per frame.
                if (Time.unscaledTime >= _nextDeviceAttempt)
                {
                    _nextDeviceAttempt = Time.unscaledTime + DeviceRetrySeconds;
                    StartDevice();
                }
                return;
            }

            // A faulted backend is not going to recover on its own — the device was
            // unplugged, or something took it. Move on rather than reading zeroes.
            if (_source.IsFaulted) { AdvanceAfterSilence(faulted: true); return; }

            int count = _source.Read(ref _scratch);
            if (count < 0)
            {
                int frames = -count / Mathf.Max(1, _source.Channels);
                Debug.LogWarning($"[VoiceRT] dropped {frames * 1000 / Mathf.Max(1, _source.SampleRate)} ms " +
                                 "of microphone audio: the game stalled longer than the capture buffer. " +
                                 "The next utterance starts from now.");
                _pending.Clear();
                _outbox.Clear();
                _phase = 0;
                _lowPass.Reset();
                count = 0;
            }

            // A driver can stall: the device opens, the read position never moves,
            // and no sample ever arrives. That is silence too — so everything below
            // this runs whether or not there was new audio.
            int mono = count > 0 ? Downmix(_scratch, count, _source.Channels) : 0;
            if (mono > 0)
            {
                Level = Peak(_scratch, mono, out float rms);
                Rms = rms;
                if (Level > _loudestSinceOpen) _loudestSinceOpen = Level;
                if (_pinUnproven && _loudestSinceOpen > SilentPeak) ProveDevice();
            }

            // Wall time, not a sum of frame deltas: this runs only on frames that
            // carry new microphone data, which is one in several at a high frame rate.
            bool dead = Time.unscaledTime - _deviceOpenedAt >= SilentAfterSeconds
                        && _loudestSinceOpen <= SilentPeak;
            if (dead && !IsSilent)
            {
                IsSilent = true;
                if (autoAdvanceOnSilence && !_announcedSilent) { AdvanceAfterSilence(); return; }
                if (!_announcedSilent)
                {
                    _announcedSilent = true;
                    Debug.LogWarning($"[VoiceRT] {OpenedDeviceLabel} via {Backend} delivers no usable " +
                                     $"signal (peak {_loudestSinceOpen:0.00000}); pick another input " +
                                     "(F9 in the demo, or VoiceRT > Microphone monitor).");
                }
            }
            IsSilent = dead;

            IsCapturing = ShouldStream();
            if (!IsCapturing)
            {
                // Keep the resampler's history clean: audio nobody sent must not
                // be interpolated into the first samples of the next utterance —
                // and neither must a part-full packet of it, which would arrive
                // stitched to the front of the next thing the player says, right
                // where the first word is.
                _pending.Clear();
                _outbox.Clear();
                _phase = 0;
                _lowPass.Reset();
                return;
            }
            if (mono <= 0) return;      // capturing, but this frame brought nothing

            // Filter at the DEVICE rate, before the interpolator throws two samples
            // in three away — after decimation the aliases are already in the band
            // and no filter can separate them from speech.
            for (int i = 0; i < mono; i++) _pending.Add(_lowPass.Process(_scratch[i]));
            Resample();
            if (_outbox.Count >= BytesPerChunk) Flush();
        }

        private int BytesPerChunk => 2 * (TargetRate * chunkMs / 1000);

        private bool ShouldStream()
        {
            var npc = Target;
            if (npc == null || !npc.IsConnected) return false;
            if (mode == VoiceRTMicMode.PushToTalk) return _talking;
            if (!npc.IsSpeaking) return true;
            // The NPC is talking. On speakers this microphone can hear it, and
            // sending that back is how an NPC interrupts itself forever.
            return allowBargeIn && Level >= bargeInThreshold;
        }

        /// <summary>Interleaved N-channel to mono, in place at the front of the same
        /// buffer. Capture devices commonly report two channels — the FMOD record
        /// drivers on the development machine all do — and the bridge wants one.
        /// Averaging rather than taking channel 0, because an interface that puts
        /// the only connected input on the right channel is a normal thing to meet.</summary>
        private static int Downmix(float[] buffer, int count, int channels)
        {
            if (channels <= 1) return count;
            int frames = count / channels;
            float scale = 1f / channels;
            for (int f = 0; f < frames; f++)
            {
                float sum = 0f;
                int at = f * channels;
                for (int c = 0; c < channels; c++) sum += buffer[at + c];
                buffer[f] = sum * scale;
            }
            return frames;
        }

        /// <summary>The anti-alias filter that has to exist now that FMOD is primary.
        ///
        /// UnityEngine.Microphone was asked for 16 kHz and, on a device that supports
        /// it, opened at 16 kHz — Unity resampled inside the driver layer and the
        /// interpolation below only ever had to clean up small ratios. FMOD's record
        /// API does not take a rate: it hands back the driver's native format, which
        /// on every endpoint on the development machine is 48 kHz. Decimating 3:1 with
        /// nothing but linear interpolation folds everything between 8 and 24 kHz back
        /// down into the speech band, and it lands on sibilants first, which is exactly
        /// what transcription accuracy is most sensitive to.
        ///
        /// Two cascaded RBJ biquads at 0.45 x the target rate: 24 dB/octave past
        /// cutoff, a handful of multiplies per sample, and no design step at runtime.
        /// Deliberately not a windowed-sinc FIR — this runs on every captured sample
        /// and the extra stopband rejection would not survive a 16 kHz mono codec.</summary>
        private sealed class DecimationLowPass
        {
            private float _b0, _b1, _b2, _a1, _a2;
            private float _x1a, _x2a, _y1a, _y2a;   // stage A
            private float _x1b, _x2b, _y1b, _y2b;   // stage B
            private bool _active;

            public void Configure(int sourceRate, int targetRate)
            {
                Reset();
                // Nothing to fold down: the device is already at or below the target.
                _active = sourceRate > targetRate && sourceRate > 0;
                if (!_active) return;

                float cutoff = 0.45f * targetRate;
                float w0 = 2f * Mathf.PI * cutoff / sourceRate;
                float cos = Mathf.Cos(w0);
                float alpha = Mathf.Sin(w0) / (2f * 0.70710678f);   // Q = 1/sqrt(2)

                float a0 = 1f + alpha;
                _b0 = (1f - cos) * 0.5f / a0;
                _b1 = (1f - cos) / a0;
                _b2 = _b0;
                _a1 = -2f * cos / a0;
                _a2 = (1f - alpha) / a0;
            }

            public void Reset()
            {
                _x1a = _x2a = _y1a = _y2a = 0f;
                _x1b = _x2b = _y1b = _y2b = 0f;
            }

            public float Process(float x)
            {
                if (!_active) return x;
                float ya = _b0 * x + _b1 * _x1a + _b2 * _x2a - _a1 * _y1a - _a2 * _y2a;
                _x2a = _x1a; _x1a = x; _y2a = _y1a; _y1a = ya;

                float yb = _b0 * ya + _b1 * _x1b + _b2 * _x2b - _a1 * _y1b - _a2 * _y2b;
                _x2b = _x1b; _x1b = ya; _y2b = _y1b; _y1b = yb;
                return yb;
            }
        }

        private readonly DecimationLowPass _lowPass = new DecimationLowPass();

        /// <summary>Device rate to 16 kHz, carrying the fractional position across
        /// chunks so a 48 kHz device does not drift a sample every few frames.</summary>
        private void Resample()
        {
            int rate = _source != null ? _source.SampleRate : TargetRate;
            if (rate == TargetRate || rate <= 0)
            {
                for (int i = 0; i < _pending.Count; i++) Write(_pending[i]);
                _pending.Clear();
                return;
            }
            double step = (double)rate / TargetRate;
            while (_phase + 1 < _pending.Count)
            {
                int index = (int)_phase;
                float frac = (float)(_phase - index);
                float a = _pending[index];
                float b = _pending[index + 1];
                Write(a + (b - a) * frac);
                _phase += step;
            }
            int consumed = (int)_phase;
            if (consumed > 0)
            {
                _pending.RemoveRange(0, consumed);
                _phase -= consumed;
            }
        }

        private void Write(float sample)
        {
            short s = (short)Mathf.Clamp(sample * 32767f, -32768f, 32767f);
            _outbox.Add((byte)(s & 0xFF));
            _outbox.Add((byte)((s >> 8) & 0xFF));
        }

        private void Flush()
        {
            if (_outbox.Count == 0) return;
            var npc = Target;
            if (npc != null && npc.IsConnected) npc.SendMicAudio(_outbox.ToArray());
            _outbox.Clear();
        }

        private static float Peak(float[] data, int count, out float rms)
        {
            // Every 4th sample: a level meter does not need the other three, and
            // this runs on 48 kHz audio every frame.
            float peak = 0f;
            double sum = 0;
            int n = 0;
            for (int i = 0; i < count; i += 4)
            {
                float s = data[i];
                float v = s < 0 ? -s : s;
                if (v > peak) peak = v;
                sum += (double)s * s;
                n++;
            }
            rms = n > 0 ? (float)System.Math.Sqrt(sum / n) : 0f;
            return peak > 1f ? 1f : peak;
        }
    }
}
