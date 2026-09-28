// VoiceRT -> FMOD programmer-instrument sink.
//
// Replaces the plain-AudioSource path with an FMOD Studio event whose Programmer
// Instrument is fed, live, from the same PcmRingBuffer the TCP reader thread fills.
// Everything downstream of the event - spatializer, bus, snapshots, ducking,
// reverb sends - is authored in FMOD Studio, not in Unity.
//
// Ownership: this component CREATES and OWNS one EventInstance from an
// EventReference assigned in the inspector. It does not attach to an instance
// somebody else already started, because CREATE_PROGRAMMER_SOUND fires during
// start() - install the callback after start and the first utterance is silent -
// and because the FMOD.Sound handed to the event must be released by whoever
// created it. Adopt() exists for callers that must own the instance, and must be
// called before Begin().
//
// Threads, and they matter:
//   main   - Unity. Configure/Begin/BargeIn/End/LateUpdate. The only place that
//            may touch UnityEngine API.
//   reader - VoiceRTClient's socket thread. Write() and Flush() only.
//   FMOD   - OnPcmRead runs on the mixer thread; OnEventCallback runs on the
//            Studio update thread (async by default in FMOD-for-Unity). Neither
//            may touch UnityEngine API, allocate, or let an exception escape.

#if VOICERT_FMOD

using System;
using System.Runtime.InteropServices;
using UnityEngine;
using VoiceRT.Core;

namespace VoiceRT.Fmod
{
    [AddComponentMenu("VoiceRT/VoiceRT FMOD Sink")]
    [DisallowMultipleComponent]
    public sealed class VoiceRTFmodSink : MonoBehaviour, IVoiceRTAudioSink
    {
        // ------------------------------------------------------------ inspector

        [Header("Event")]
        [Tooltip("An FMOD event whose only instrument is a Programmer Instrument, e.g. event:/NPC/Dialogue")]
        public FMODUnity.EventReference dialogueEvent;

        [Tooltip("Let RuntimeManager drive the event's 3D attributes from this transform every frame.")]
        public bool attachToTransform = true;

        [Header("Buffering")]
        [Tooltip("Seconds of voice held between the network thread and FMOD's mixer. A GPU " +
                 "voice is synthesized far faster than it is spoken — a nine-second reply can " +
                 "arrive in under a second — so this has to hold a whole reply, not just the " +
                 "mixer's lookahead. It costs 48 KB per second per NPC.")]
        [Range(0.25f, 60f)] public float bufferSeconds = 30f;

        [Tooltip("PCM samples FMOD decodes ahead. This is the audio a FLUSH cannot reach: " +
                 "1024 @ 16 kHz = 64 ms of un-cuttable tail. Lower = tighter barge-in, more callbacks.")]
        [Range(256, 8192)] public int decodeBufferSamples = 1024;

        [Tooltip("Seconds of PCM the user sound advertises before it loops. Only controls how " +
                 "often FMOD wraps the read cursor; the stream itself never ends.")]
        [Range(0.25f, 4f)] public float loopWindowSeconds = 1f;

        [Header("Barge-in")]
        [Tooltip("On cut, also stop+restart the event, which throws away FMOD's decode buffer. " +
                 "Costs an event restart. Enable only if the tail is audible.")]
        public bool hardCutOnFlush = false;

        // ------------------------------------------------------------ public surface

        /// <summary>The owned instance. Valid between Begin() and End().</summary>
        public FMOD.Studio.EventInstance Instance => _instance;

        public bool IsRunning => _running;
        public int SourceSampleRate => _sourceRate;
        public long Underruns => _ring == null ? 0 : _ring.Underruns;
        public long Overruns => _ring == null ? 0 : _ring.Overruns;
        public int BufferedMilliseconds =>
            _ring == null ? 0 : (int)(1000L * _ring.Available / Math.Max(1, _sourceRate));

        /// <summary>MAIN THREAD. The event could not be created: bank not loaded,
        /// event path wrong, or FMOD not initialised.</summary>
        public event Action<string> SetupFailed;

        /// <summary>Report a setup failure, and make sure it is heard.
        ///
        /// A silent programmer-sound failure is indistinguishable from an NPC
        /// that had nothing to say, so with no subscriber this goes to the
        /// console rather than into an empty delegate.</summary>
        private void Fail(string message)
        {
            if (SetupFailed != null) SetupFailed.Invoke(message);
            else Debug.LogError($"[VoiceRT] FMOD sink on {name}: {message}");
        }

        /// <summary>Take ownership of an instance created elsewhere. Before Begin(),
        /// and before the instance is started.</summary>
        public void Adopt(FMOD.Studio.EventInstance instance)
        {
            if (_running) throw new InvalidOperationException("Adopt() after Begin()");
            _instance = instance;
            _adopted = true;
        }

        // ------------------------------------------------------------ private state

        private PcmRingBuffer _ring;
        private FMOD.Studio.EventInstance _instance;
        private FMOD.Sound _sound;      // created in CREATE_PROGRAMMER_SOUND, released in DESTROY_
        private GCHandle _self;         // `this`, reachable from the static callbacks
        private short[] _scratch;       // mixer-thread staging, allocated once, never on the audio thread
        private int _sourceRate = 16000;
        private volatile bool _running;
        private bool _adopted;
        private bool _configured;
        private int _retriesLeft;

        // Delegates must be kept alive for as long as native code can call them, and must
        // be static so IL2CPP can AOT-compile the reverse P/Invoke stubs. A local lambda
        // here is the classic "works in the editor, crashes on device" bug.
        private static readonly FMOD.Studio.EVENT_CALLBACK EventCallback = OnEventCallback;
        private static readonly FMOD.SOUND_PCMREAD_CALLBACK PcmReadCallback = OnPcmRead;
        private static readonly FMOD.SOUND_PCMSETPOS_CALLBACK PcmSetPosCallback = OnPcmSetPos;

        private static readonly short[] Silence = new short[2048];
        private const int RetryFrames = 300;   // ~5 s of grace while banks finish loading

        // ------------------------------------------------------------ IVoiceRTAudioSink

        public void Configure(int sourceSampleRate)
        {
            if (sourceSampleRate <= 0) sourceSampleRate = 16000;
            _sourceRate = sourceSampleRate;

            int capacity = Mathf.CeilToInt(_sourceRate * bufferSeconds);
            if (_ring == null || _ring.Capacity != capacity) _ring = new PcmRingBuffer(capacity);

            int scratch = Mathf.Max(decodeBufferSamples * 2, 2048);
            if (_scratch == null || _scratch.Length < scratch) _scratch = new short[scratch];

            _configured = true;
        }

        public void Begin()
        {
            if (_running) return;
            if (!_configured) Configure(_sourceRate);
            if (!TryCreateInstance()) { _retriesLeft = RetryFrames; return; }
            StartInstance();
        }

        /// <summary>NETWORK READER THREAD.</summary>
        public void Write(byte[] pcm16le, int offset, int count)
        {
            var ring = _ring;
            if (ring != null) ring.WritePcm16(pcm16le, offset, count);
        }

        public void Write(byte[] pcm16le) => Write(pcm16le, 0, pcm16le == null ? 0 : pcm16le.Length);

        /// <summary>NETWORK READER THREAD. The server's FLUSH is the authoritative cut
        /// point and arrives in stream order, so everything before it is already written
        /// and everything after it is new. Drop the queue, lift local suppression.</summary>
        public void Flush()
        {
            _ring?.EndBargeIn();
            if (hardCutOnFlush && _running) HardCut();
        }

        /// <summary>MAIN THREAD. Local barge-in, before the round trip.</summary>
        public void BargeIn()
        {
            _ring?.BeginBargeIn();
            if (hardCutOnFlush && _running) HardCut();
        }

        public void End()
        {
            _running = false;
            _retriesLeft = 0;
            // Never leave the ring suppressed: a barge-in with no reply in flight gets
            // no FLUSH back, and a suppressed ring silently swallows every later turn.
            _ring?.EndBargeIn();

            if (_instance.isValid())
            {
                if (attachToTransform) FMODUnity.RuntimeManager.DetachInstanceFromGameObject(_instance);
                _instance.stop(FMOD.Studio.STOP_MODE.IMMEDIATE);
                _instance.release();
            }
            _instance.clearHandle();

            // release() is deferred: DESTROY_PROGRAMMER_SOUND has NOT fired yet. flushCommands()
            // blocks until the Studio command queue drains, so the callback runs - and stops
            // dereferencing our GCHandle - before we free it. Freeing first is a use-after-free
            // on FMOD's own thread, and it only ever reproduces on scene unload.
            if (FMODUnity.RuntimeManager.IsInitialized)
                FMODUnity.RuntimeManager.StudioSystem.flushCommands();

            if (_sound.hasHandle()) { _sound.release(); _sound.clearHandle(); }
            if (_self.IsAllocated) _self.Free();

            _ring?.Clear();
            _adopted = false;
        }

        // ------------------------------------------------------------ unity lifecycle

        private void LateUpdate()
        {
            if (_retriesLeft > 0)
            {
                _retriesLeft--;
                if (TryCreateInstance()) StartInstance();
                else if (_retriesLeft == 0)
                    Fail($"event '{dialogueEvent}' unavailable after {RetryFrames} frames " +
                                        "(bank not loaded, or FMOD not initialised)");
                return;
            }

            if (_running && !attachToTransform && _instance.isValid())
                _instance.set3DAttributes(FMODUnity.RuntimeUtils.To3DAttributes(transform));
        }

        private void OnDisable() => End();
        private void OnDestroy() => End();

        // ------------------------------------------------------------ wiring

        private bool TryCreateInstance()
        {
            if (!FMODUnity.RuntimeManager.IsInitialized) return false;
            if (!_adopted)
            {
                if (dialogueEvent.IsNull) { Fail("dialogueEvent is not assigned"); return false; }
                try { _instance = FMODUnity.RuntimeManager.CreateInstance(dialogueEvent); }
                catch (Exception) { return false; }   // EventNotFoundException while banks load: retry
            }
            return _instance.isValid();
        }

        private void StartInstance()
        {
            // 1. one handle, reachable from both static callbacks
            if (!_self.IsAllocated) _self = GCHandle.Alloc(this, GCHandleType.Normal);
            IntPtr userData = GCHandle.ToIntPtr(_self);

            // 2. userdata BEFORE the callback. Reverse the order and an early
            //    CREATE_PROGRAMMER_SOUND finds IntPtr.Zero and hands the event nothing.
            _instance.setUserData(userData);
            _instance.setCallback(EventCallback,
                FMOD.Studio.EVENT_CALLBACK_TYPE.CREATE_PROGRAMMER_SOUND |
                FMOD.Studio.EVENT_CALLBACK_TYPE.DESTROY_PROGRAMMER_SOUND);

            // 3. position before start. RuntimeManager.CreateInstance parks a 3D event at
            //    1e17 in the editor until somebody sets attributes; skip this and the NPC
            //    is inaudible for reasons that look like a bug in FMOD.
            _instance.set3DAttributes(FMODUnity.RuntimeUtils.To3DAttributes(transform));
            if (attachToTransform) FMODUnity.RuntimeManager.AttachInstanceToGameObject(_instance, gameObject);

            _running = true;
            _instance.start();
        }

        private void HardCut()
        {
            // Studio API calls are queued and thread-safe, so this is legal from the reader
            // thread. It destroys and recreates the programmer sound, which is the only way
            // to discard FMOD's decode buffer. ALLOWFADEOUT lets the event's own AHDSR
            // release (60 ms on bus:/Dialogue) run, so a barge-in ducks out instead of
            // clicking; the decode tail is about that long anyway.
            _instance.stop(FMOD.Studio.STOP_MODE.ALLOWFADEOUT);
            _instance.start();
        }

        // ------------------------------------------------------------ FMOD callbacks

        [AOT.MonoPInvokeCallback(typeof(FMOD.Studio.EVENT_CALLBACK))]
        private static FMOD.RESULT OnEventCallback(FMOD.Studio.EVENT_CALLBACK_TYPE type, IntPtr eventPtr, IntPtr parameters)
        {
            var instance = new FMOD.Studio.EventInstance(eventPtr);
            if (instance.getUserData(out IntPtr userData) != FMOD.RESULT.OK || userData == IntPtr.Zero)
                return FMOD.RESULT.OK;

            var self = GCHandle.FromIntPtr(userData).Target as VoiceRTFmodSink;
            if (self == null) return FMOD.RESULT.OK;

            try
            {
                switch (type)
                {
                    case FMOD.Studio.EVENT_CALLBACK_TYPE.CREATE_PROGRAMMER_SOUND:
                    {
                        var props = (FMOD.Studio.PROGRAMMER_SOUND_PROPERTIES)
                            Marshal.PtrToStructure(parameters, typeof(FMOD.Studio.PROGRAMMER_SOUND_PROPERTIES));

                        var exinfo = new FMOD.CREATESOUNDEXINFO
                        {
                            cbsize            = Marshal.SizeOf(typeof(FMOD.CREATESOUNDEXINFO)),
                            numchannels       = 1,
                            defaultfrequency  = self._sourceRate,   // 16 kHz; FMOD resamples to the mixer rate
                            format            = FMOD.SOUND_FORMAT.PCM16,
                            // length is in BYTES for OPENUSER. With LOOP_NORMAL this is only the
                            // wrap window, not the end of the stream.
                            length            = (uint)(self._sourceRate * 2 * Mathf.Max(0.25f, self.loopWindowSeconds)),
                            decodebuffersize  = (uint)Mathf.Max(256, self.decodeBufferSamples),
                            pcmreadcallback   = PcmReadCallback,
                            pcmsetposcallback = PcmSetPosCallback,
                            userdata          = userData,           // how OnPcmRead finds `self`
                        };

                        // CREATESTREAM is load-bearing: without it FMOD calls pcmreadcallback
                        // exactly ONCE, at creation, and we play one buffer of silence forever.
                        // LOOP_NORMAL keeps the stream from ever reaching EOF.
                        // No _3D here - the event's Spatializer owns positioning.
                        const FMOD.MODE mode = FMOD.MODE.OPENUSER
                                             | FMOD.MODE.CREATESTREAM
                                             | FMOD.MODE.LOOP_NORMAL;

                        var result = FMODUnity.RuntimeManager.CoreSystem.createSound(
                            IntPtr.Zero, mode, ref exinfo, out FMOD.Sound sound);
                        if (result != FMOD.RESULT.OK) return result;

                        self._sound = sound;
                        props.sound = sound.handle;
                        props.subsoundIndex = -1;   // the sound itself, not an FSB subsound
                        Marshal.StructureToPtr(props, parameters, false);
                        break;
                    }

                    case FMOD.Studio.EVENT_CALLBACK_TYPE.DESTROY_PROGRAMMER_SOUND:
                    {
                        var props = (FMOD.Studio.PROGRAMMER_SOUND_PROPERTIES)
                            Marshal.PtrToStructure(parameters, typeof(FMOD.Studio.PROGRAMMER_SOUND_PROPERTIES));
                        var sound = new FMOD.Sound(props.sound);
                        if (sound.hasHandle()) sound.release();
                        self._sound.clearHandle();
                        break;
                    }
                }
            }
            catch (Exception)
            {
                return FMOD.RESULT.ERR_INTERNAL;   // never let a managed exception unwind into native FMOD
            }
            return FMOD.RESULT.OK;
        }

        [AOT.MonoPInvokeCallback(typeof(FMOD.SOUND_PCMREAD_CALLBACK))]
        private static FMOD.RESULT OnPcmRead(IntPtr soundPtr, IntPtr data, uint datalen)
        {
            // FMOD MIXER THREAD. Allocation-free, bounded lock, and it NEVER returns an
            // error: an underrun is silence, not the end of the stream. datalen is in
            // bytes of SOURCE format, so this stays correct under pitch and doppler.
            int samples = (int)(datalen / 2);
            if (samples <= 0) return FMOD.RESULT.OK;

            VoiceRTFmodSink self = null;
            var sound = new FMOD.Sound(soundPtr);
            if (sound.getUserData(out IntPtr userData) == FMOD.RESULT.OK && userData != IntPtr.Zero)
                self = GCHandle.FromIntPtr(userData).Target as VoiceRTFmodSink;

            var ring = self?._ring;
            var scratch = self?._scratch;
            if (ring == null || scratch == null) { WriteSilence(data, samples); return FMOD.RESULT.OK; }

            int written = 0;
            while (written < samples)
            {
                int chunk = Math.Min(scratch.Length, samples - written);
                ring.ReadPcm16(scratch, 0, chunk);                    // zero-fills any shortfall
                Marshal.Copy(scratch, 0, data + written * 2, chunk);  // one memcpy per chunk
                written += chunk;
            }
            return FMOD.RESULT.OK;
        }

        [AOT.MonoPInvokeCallback(typeof(FMOD.SOUND_PCMSETPOS_CALLBACK))]
        private static FMOD.RESULT OnPcmSetPos(IntPtr sound, int subsound, uint position, FMOD.TIMEUNIT postype)
        {
            // FMOD seeks us when the loop window wraps. A live stream has no position to
            // seek to; acknowledging keeps the loop running.
            return FMOD.RESULT.OK;
        }

        private static void WriteSilence(IntPtr data, int samples)
        {
            int written = 0;
            while (written < samples)
            {
                int chunk = Math.Min(Silence.Length, samples - written);
                Marshal.Copy(Silence, 0, data + written * 2, chunk);
                written += chunk;
            }
        }
    }
}

#endif // VOICERT_FMOD
