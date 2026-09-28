// The primary capture backend: FMOD Core's record API.
//
// The product is Unity + FMOD first, and the microphone is the last part of the
// audio path that was still going through Unity. It is also the part that broke
// most often, for two reasons this backend is built to survive:
//
//   1. Unity's recording backend can come up empty. Measured on the development
//      machine: UnityEngine.Microphone.devices returned an empty array and
//      Microphone.Start(null) returned null for every device, in an editor where
//      the OS was listing four active capture endpoints and FMOD, in the same
//      process, enumerated eight. AudioSettings.Reset did not bring it back.
//
//   2. The device Windows calls "default" is regularly not a microphone. Measured
//      on the same machine: the default capture endpoint was "Microphone (Mixing
//      Driver 1 for US-1x2)", a virtual loopback that recorded 72000 frames at a
//      peak of exactly zero, while the physical "Microphone (US-1x2)" sat beside
//      it in the list — and, that day, was held exclusively by another process and
//      returned ERR_RECORD. The only endpoint carrying audio was a third one.
//
// So this backend never asks for "the default". It ranks the connected drivers,
// drops the loopbacks outright — recording one streams the game's own output into
// speech recognition — and reports faults precisely enough that VoiceRTVoiceInput
// can walk to the next candidate instead of sitting on a dead device.
//
// Recording uses FMODUnity.RuntimeManager.CoreSystem, the system FMOD for Unity
// has already initialised, rather than a second System of our own: a private one
// has to open an output device to bring its record subsystem up, and on a machine
// where an audio interface is already claimed that init returns ERR_OUTPUT_INIT.
//
// Threads: main thread only. FMOD fills the loop buffer on its own thread; Read()
// just measures how far the write cursor moved and copies out behind it.

#if VOICERT_FMOD

using System;
using System.Runtime.InteropServices;
using UnityEngine;

namespace VoiceRT.Fmod
{
    public sealed class VoiceRTFmodMicSource : IVoiceRTMicSource
    {
        /// <summary>Registers this backend ahead of Unity's, in every player and on
        /// every domain reload, without the scene having to reference it.
        ///
        /// [Preserve] because nothing in a managed assembly calls this: IL2CPP's
        /// static analysis would strip the method and the whole backend with it,
        /// and the failure would be a player that quietly uses Unity capture while
        /// the editor uses FMOD.</summary>
        [UnityEngine.Scripting.Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void Register() =>
            VoiceRTMicBackends.Register("FMOD", priority: 100, create: () => new VoiceRTFmodMicSource());

        /// <summary>Seconds of loop buffer FMOD records into. The reader only has to
        /// keep up with it frame by frame; this is the margin for a hitch. At 48 kHz
        /// stereo PCM16 it costs 960 kB.</summary>
        private const float LoopSeconds = 5f;

        private FMOD.Sound _sound;
        private int _driver = -1;
        private string _opened;
        private int _rate;
        private int _channels;
        private int _loopFrames;
        private uint _readFrame;
        private short[] _staging = Array.Empty<short>();

        // -- monitoring into the FMOD Studio mixer ---------------------------
        private FMOD.Studio.Bus _bus;
        private bool _busLocked;
        private FMOD.Channel _monitor;
        private FMOD.DSP _fader;        // the channel fader: INPUT metering is pre-volume
        private FMOD.DSP _groupHead;    // the bus head: proves the signal reached the strip
        private bool _monitorPaused;    // still waiting for the recorder to get ahead
        private float _fmodPeak, _fmodRms, _busPeak;

        /// <summary>Studio bus the microphone is played into, so the signal genuinely
        /// enters the FMOD mixer instead of only existing in a C# array. Empty disables
        /// monitoring entirely; the speech path is unaffected either way.</summary>
        public static string MonitorBusPath = "bus:/VoiceInput";

        /// <summary>How loud the monitored microphone is, 0..1, applied to the BUS.
        ///
        /// Deliberately not applied to the channel. ChannelControl volume is implemented
        /// by the channel FADER DSP, so a channel at zero reads zero at that fader output
        /// AND has audibility zero, which makes it the first candidate for voice stealing —
        /// and a stolen channel runs no DSP, so the meter dies with no error to explain it.
        /// Measured: channel at 0 read -120 dBFS while the same microphone read -45.6 dBFS
        /// with the channel at unity. Hold the channel at unity, attenuate the bus.</summary>
        public static float MonitorVolume = 0f;

        /// <summary>Peak level FMOD itself measured on the microphone channel, 0..1.
        /// This is the "is there signal" answer that does not depend on our own sample
        /// scan — it comes from FMOD metering DSP.</summary>
        public float FmodPeak => _fmodPeak;

        /// <summary>RMS as measured by FMOD, 0..1.</summary>
        public float FmodRms => _fmodRms;

        /// <summary>Peak measured at the bus itself. Non-zero proves the microphone is
        /// reaching the Studio strip, not merely the channel in front of it.</summary>
        public float BusPeak => _busPeak;

        /// <summary>Whether the microphone is currently feeding the Studio bus.</summary>
        public bool IsMonitoring => _monitor.hasHandle();

        // IVoiceRTMicSource: FMOD answers the "is there signal" question itself.
        public float BackendPeak => IsMonitoring ? _fmodPeak : -1f;
        public string BackendMeter => IsMonitoring ? MonitorBusPath : null;

        public string BackendName => "FMOD";
        public bool IsOpen => _sound.hasHandle();
        public string OpenedDevice => _opened;
        public int SampleRate => _rate;
        public int Channels => _channels;
        public bool IsFaulted { get; private set; }

        /// <summary>Whether CoreSystem can be touched.
        ///
        /// RuntimeManager.IsInitialized is NOT sufficient on its own, and reading it
        /// alone is a trap: it tests the private static `instance` field directly
        /// rather than going through the lazy `Instance` property that creates the
        /// manager. Until something else in the scene happens to touch RuntimeManager,
        /// IsInitialized answers false forever — and this backend would report "not
        /// ready" on every frame while the input silently fell through to Unity.
        ///
        /// So force the lazy init by touching StudioSystem, in a try/catch: the
        /// Instance getter rethrows a cached init exception, and in edit mode it
        /// logs an error and returns null, which is why isPlaying gates it.</summary>
        private static bool SystemReady
        {
            get
            {
                if (!Application.isPlaying) return false;
                if (FMODUnity.RuntimeManager.IsInitialized) return true;
                try
                {
                    var studio = FMODUnity.RuntimeManager.StudioSystem;
                    if (!studio.isValid()) return false;
                }
                catch (Exception) { return false; }
                return FMODUnity.RuntimeManager.IsInitialized;
            }
        }

        // ------------------------------------------------------------ enumeration

        /// <summary>Connected capture endpoints, best first, loopbacks removed.
        /// FMOD's own "default" flag is deliberately not honoured — on the machine
        /// this was written for, it points at a silent virtual mixer.</summary>
        public string[] Devices
        {
            get
            {
                var names = Enumerate();
                var plain = new string[names.Length];
                for (int i = 0; i < names.Length; i++) plain[i] = names[i].name;
                return VoiceRTMicNames.Prefer(plain);
            }
        }

        private readonly struct Driver
        {
            public readonly int Index;
            public readonly string name;
            public readonly int Rate;
            public readonly int Channels;
            public Driver(int index, string n, int rate, int channels)
            {
                Index = index; name = n; Rate = rate; Channels = channels;
            }
        }

        private static Driver[] Enumerate()
        {
            if (!SystemReady) return Array.Empty<Driver>();
            var core = FMODUnity.RuntimeManager.CoreSystem;
            if (core.getRecordNumDrivers(out int num, out int _) != FMOD.RESULT.OK || num <= 0)
                return Array.Empty<Driver>();

            var found = new System.Collections.Generic.List<Driver>(num);
            for (int i = 0; i < num; i++)
            {
                var r = core.getRecordDriverInfo(i, out string name, 256, out Guid _,
                    out int rate, out FMOD.SPEAKERMODE _, out int channels, out FMOD.DRIVER_STATE state);
                if (r != FMOD.RESULT.OK) continue;
                if ((state & FMOD.DRIVER_STATE.CONNECTED) == 0) continue;
                // A tap on an output device. Recording it feeds the NPC's own voice
                // back into transcription; it is never what the player meant.
                if (VoiceRTMicNames.IsLoopback(name)) continue;
                found.Add(new Driver(i, name, rate, channels <= 0 ? 1 : channels));
            }
            return found.ToArray();
        }

        // ------------------------------------------------------------ lifecycle

        public bool Open(string device)
        {
            Close();
            IsFaulted = false;

            if (!SystemReady)
            {
                // Banks and the Studio system come up over the first few frames.
                // Not a fault: the caller retries.
                return false;
            }

            var drivers = Enumerate();
            if (drivers.Length == 0)
            {
                IsFaulted = true;
                return false;
            }

            int pick = -1;
            if (!string.IsNullOrEmpty(device))
            {
                for (int i = 0; i < drivers.Length; i++)
                    if (drivers[i].name == device) { pick = i; break; }
                if (pick < 0) return false;      // asked for a device that is not here
            }
            else
            {
                // "You choose" means the best-ranked real device, NOT the system
                // default — the default is the thing that was silent.
                string[] preferred = VoiceRTMicNames.Prefer(Names(drivers));
                if (preferred.Length == 0) { IsFaulted = true; return false; }
                for (int i = 0; i < drivers.Length; i++)
                    if (drivers[i].name == preferred[0]) { pick = i; break; }
            }
            if (pick < 0) { IsFaulted = true; return false; }

            return Start(drivers[pick]);
        }

        private static string[] Names(Driver[] drivers)
        {
            var names = new string[drivers.Length];
            for (int i = 0; i < drivers.Length; i++) names[i] = drivers[i].name;
            return names;
        }

        private bool Start(Driver driver)
        {
            var core = FMODUnity.RuntimeManager.CoreSystem;
            int channels = driver.Channels;
            int rate = driver.Rate > 0 ? driver.Rate : 48000;
            int loopFrames = Mathf.Max(1024, Mathf.CeilToInt(rate * LoopSeconds));

            var exinfo = new FMOD.CREATESOUNDEXINFO
            {
                cbsize           = Marshal.SizeOf(typeof(FMOD.CREATESOUNDEXINFO)),
                numchannels      = channels,
                format           = FMOD.SOUND_FORMAT.PCM16,
                defaultfrequency = rate,
                // BYTES for OPENUSER. This is the whole ring FMOD records into.
                length           = (uint)(loopFrames * channels * 2),
            };

            const FMOD.MODE mode = FMOD.MODE.OPENUSER | FMOD.MODE.LOOP_NORMAL;
            var r = core.createSound(IntPtr.Zero, mode, ref exinfo, out FMOD.Sound sound);
            if (r != FMOD.RESULT.OK)
            {
                Debug.LogWarning($"[VoiceRT] FMOD capture: createSound for '{driver.name}' -> {r}.");
                return false;
            }

            r = core.recordStart(driver.Index, sound, true);
            if (r != FMOD.RESULT.OK)
            {
                sound.release();
                // ERR_RECORD is the interesting one: the endpoint exists and is
                // connected, but something else owns it. Nothing here can fix that,
                // and the right move is the next device, so say so and let the
                // caller walk on.
                Debug.LogWarning($"[VoiceRT] FMOD capture: '{driver.name}' would not start ({r})" +
                                 (r == FMOD.RESULT.ERR_RECORD
                                     ? " — the device is held by another process."
                                     : "."));
                return false;
            }

            _sound      = sound;
            _driver     = driver.Index;
            _opened     = driver.name;
            _rate       = rate;
            _channels   = channels;
            _loopFrames = loopFrames;
            _readFrame  = 0;
            core.getRecordPosition(_driver, out _readFrame);
            BeginMonitor(core);
            return true;
        }

        /// <summary>Play the live recording into a Studio bus, so the microphone becomes a
        /// real input of the FMOD mixer and FMOD own metering can answer "is there signal"
        /// without trusting our sample scan.</summary>
        private void BeginMonitor(FMOD.System core)
        {
            _fmodPeak = _fmodRms = _busPeak = 0f;
            if (string.IsNullOrEmpty(MonitorBusPath)) return;

            var studio = FMODUnity.RuntimeManager.StudioSystem;
            if (studio.getBus(MonitorBusPath, out _bus) != FMOD.RESULT.OK)
            {
                // Not fatal: the speech path does not need the bus. Said once, so a missing
                // or renamed bus is never mistaken for a dead microphone.
                Debug.LogWarning($"[VoiceRT] FMOD monitor: bus '{MonitorBusPath}' not found " +
                                 "(bank not loaded, or renamed in FMOD Studio). Capture still " +
                                 "works; the Studio mixer just will not show it.");
                return;
            }

            // lockChannelGroup is an ASYNCHRONOUS Studio command: the core ChannelGroup
            // behind a bus does not exist until that command has been processed, because
            // Studio frees bus groups while they are idle. Asking before the flush returns
            // an invalid handle — intermittently, which is worse than always.
            if (_bus.lockChannelGroup() != FMOD.RESULT.OK) return;
            _busLocked = true;
            studio.flushCommands();
            if (_bus.getChannelGroup(out FMOD.ChannelGroup group) != FMOD.RESULT.OK) return;

            // Start PAUSED. The play cursor would otherwise begin at 0 while the record
            // cursor is also near 0, so it would read not-yet-written silence and then race
            // the writer for the rest of the session.
            if (core.playSound(_sound, group, true, out _monitor) != FMOD.RESULT.OK) return;
            _monitorPaused = true;
            _monitor.setVolume(1f);            // see MonitorVolume: silence is taken at the bus
            _monitor.setPriority(0);           // never the first channel to be stolen
            _bus.setVolume(Mathf.Clamp01(MonitorVolume));

            if (_monitor.getDSP(FMOD.CHANNELCONTROL_DSP_INDEX.FADER, out _fader) == FMOD.RESULT.OK)
                _fader.setMeteringEnabled(true, true);
            if (group.getDSP(FMOD.CHANNELCONTROL_DSP_INDEX.HEAD, out _groupHead) == FMOD.RESULT.OK)
                _groupHead.setMeteringEnabled(true, true);

            Debug.Log($"[VoiceRT] FMOD monitor: microphone -> {MonitorBusPath} " +
                      $"(bus volume {Mathf.Clamp01(MonitorVolume):0.##}).");
        }

        /// <summary>Release the monitor once the recorder is comfortably ahead, then read
        /// FMOD own meters. Called once per frame from Read().</summary>
        private void PumpMonitor(uint recordFrame)
        {
            if (!_monitor.hasHandle()) return;

            if (_monitorPaused)
            {
                // A quarter second of recorded audio is enough of a cushion that one frame
                // spike cannot let playback overtake the writer.
                int margin = Math.Max(1024, _rate / 4);
                if (recordFrame >= (uint)margin) { _monitor.setPaused(false); _monitorPaused = false; }
                return;
            }

            if (_fader.hasHandle() &&
                _fader.getMeteringInfo(out FMOD.DSP_METERING_INFO input, IntPtr.Zero) == FMOD.RESULT.OK)
            {
                float peak = 0f, rms = 0f;
                int n = Math.Min((int)input.numchannels, 32);
                for (int c = 0; c < n; c++)
                {
                    if (input.peaklevel[c] > peak) peak = input.peaklevel[c];
                    if (input.rmslevel[c] > rms) rms = input.rmslevel[c];
                }
                _fmodPeak = peak;
                _fmodRms = rms;
            }

            if (_groupHead.hasHandle() &&
                _groupHead.getMeteringInfo(out FMOD.DSP_METERING_INFO busIn, IntPtr.Zero) == FMOD.RESULT.OK)
            {
                float peak = 0f;
                int n = Math.Min((int)busIn.numchannels, 32);
                for (int c = 0; c < n; c++) if (busIn.peaklevel[c] > peak) peak = busIn.peaklevel[c];
                _busPeak = peak;
            }
        }

        private void EndMonitor()
        {
            if (_monitor.hasHandle()) { _monitor.stop(); _monitor.clearHandle(); }
            _fader.clearHandle();
            _groupHead.clearHandle();
            // Locking a bus pins its whole strip and effect chain in memory; leaving it
            // locked leaks the VoiceInput bus for the life of the process.
            if (_busLocked && _bus.isValid()) _bus.unlockChannelGroup();
            _busLocked = false;
            _bus.clearHandle();
            _monitorPaused = false;
            _fmodPeak = _fmodRms = _busPeak = 0f;
        }

        public void Close()
        {
            if (SystemReady) EndMonitor();
            if (_driver >= 0 && SystemReady)
            {
                var core = FMODUnity.RuntimeManager.CoreSystem;
                core.recordStop(_driver);
            }
            if (_sound.hasHandle()) { _sound.release(); _sound.clearHandle(); }
            _driver = -1;
            _opened = null;
            _rate = 0;
            _channels = 0;
            _loopFrames = 0;
            _readFrame = 0;
        }

        public void DropPending()
        {
            if (!IsOpen || !SystemReady) return;
            if (FMODUnity.RuntimeManager.CoreSystem.getRecordPosition(_driver, out uint pos) == FMOD.RESULT.OK)
                _readFrame = pos;
        }

        // ------------------------------------------------------------ capture

        public int Read(ref float[] into)
        {
            if (!IsOpen || !SystemReady) return 0;
            var core = FMODUnity.RuntimeManager.CoreSystem;

            // A device that is yanked mid-session stops recording rather than
            // erroring on every call, so this is the only place it shows up.
            if (core.isRecording(_driver, out bool recording) == FMOD.RESULT.OK && !recording)
            {
                IsFaulted = true;
                return 0;
            }

            if (core.getRecordPosition(_driver, out uint pos) != FMOD.RESULT.OK) return 0;
            PumpMonitor(pos);
            if (pos == _readFrame) return 0;                 // no new audio this frame

            int frames = pos >= _readFrame
                ? (int)(pos - _readFrame)
                : (int)(_loopFrames - _readFrame + pos);
            if (frames <= 0) return 0;

            // FMOD lapped us while the main thread was elsewhere. What is in the ring
            // now belongs to an earlier moment; sending it answers the wrong question.
            if (frames >= _loopFrames - _loopFrames / 8)
            {
                _readFrame = pos;
                return -frames;
            }

            int samples = frames * _channels;
            if (_staging.Length < samples) _staging = new short[Mathf.NextPowerOfTwo(samples)];
            if (into == null || into.Length < samples) into = new float[Mathf.NextPowerOfTwo(samples)];

            uint byteOffset = _readFrame * (uint)_channels * 2;
            uint byteCount = (uint)samples * 2;
            var r = _sound.@lock(byteOffset, byteCount,
                out IntPtr p1, out IntPtr p2, out uint len1, out uint len2);
            if (r != FMOD.RESULT.OK) return 0;

            // lock() hands back up to two spans: the ring wraps inside the request.
            int written = 0;
            written += CopyOut(p1, len1, written);
            written += CopyOut(p2, len2, written);
            _sound.unlock(p1, p2, len1, len2);

            for (int i = 0; i < written; i++) into[i] = _staging[i] * (1f / 32768f);
            _readFrame = pos;
            return written;
        }

        private int CopyOut(IntPtr src, uint bytes, int at)
        {
            if (src == IntPtr.Zero || bytes < 2) return 0;
            int count = (int)(bytes / 2);
            if (at + count > _staging.Length) count = _staging.Length - at;
            if (count <= 0) return 0;
            Marshal.Copy(src, _staging, at, count);
            return count;
        }
    }
}

#endif // VOICERT_FMOD
