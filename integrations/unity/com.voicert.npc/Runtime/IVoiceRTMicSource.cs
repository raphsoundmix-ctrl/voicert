// Where the player's voice is captured from.
//
// This exists because the capture backend is the one part of the microphone path
// that is genuinely environment-dependent, and the rest of it — resampling to
// 16 kHz, stereo mixdown, level metering, the dead-input verdict, push-to-talk
// gating, chunking into packets — is not. Splitting them means the DSP is written
// once and every backend inherits the same behaviour, including the device
// auto-advance that is the whole reason this seam was cut.
//
// A source is deliberately dumb: it opens a device, hands over whatever samples
// have arrived since the last call, at whatever rate and channel count the device
// runs at, and says when it has faulted. It does not resample, does not meter,
// does not decide anything. VoiceRTVoiceInput owns all of that.
//
// Threading: every member is main thread. Capture backends that run their own
// threads buffer internally and drain on Read().

namespace VoiceRT
{
    public interface IVoiceRTMicSource
    {
        /// <summary>Shown to the player and written to the log — "FMOD", "Unity".</summary>
        string BackendName { get; }

        /// <summary>A device is open and Read() may be called.</summary>
        bool IsOpen { get; }

        /// <summary>The device actually opened. Null means the backend's own default.</summary>
        string OpenedDevice { get; }

        /// <summary>The open device's native rate. 0 when closed.</summary>
        int SampleRate { get; }

        /// <summary>The open device's channel count — 1 or 2 in practice. 0 when closed.</summary>
        int Channels { get; }

        /// <summary>Selectable device names, best first. Backends that can tell a
        /// real capsule from a loopback or a virtual mixer put the real ones first
        /// and drop the loopbacks entirely, because streaming the game's own output
        /// back into speech recognition is worse than capturing nothing.</summary>
        string[] Devices { get; }

        /// <summary>Open a device. Null or empty means "you choose". Returns false
        /// if nothing would open, which is a normal outcome worth reporting rather
        /// than an exception.</summary>
        bool Open(string device);

        void Close();

        /// <summary>Copy every sample captured since the last call into <paramref name="into"/>,
        /// growing it if it is too small, and return how many were written. Samples are
        /// interleaved at <see cref="Channels"/> and <see cref="SampleRate"/>.
        ///
        /// Returns 0 when nothing new arrived, which is the common case at a high frame
        /// rate and is not an error. Returns a negative count when the backend lapped its
        /// own buffer: the samples still in there belong to some earlier moment, the caller
        /// must discard what it is holding, and -count is how many were dropped.</summary>
        int Read(ref float[] into);

        /// <summary>Throw away whatever is queued; the next Read() starts from now.
        /// Called when the player begins an utterance, so the first packet cannot
        /// carry the tail of the room from before they pressed the key.</summary>
        void DropPending();

        /// <summary>Level this backend measured with its OWN metering, 0..1, or a
        /// negative number when the backend has no meter of its own.
        ///
        /// Worth having separately from the level the caller computes by scanning
        /// samples, because the two answer different questions. Ours says "the bytes
        /// we received are not all zero". This one says "the audio engine itself sees
        /// signal", which is what you want when the question is whether the engine is
        /// wired to the microphone at all.</summary>
        float BackendPeak { get; }

        /// <summary>Where BackendPeak is measured, for display — e.g. the Studio bus
        /// the microphone is routed into. Null when there is no backend meter.</summary>
        string BackendMeter { get; }

        /// <summary>The device cannot work: held exclusively by another process,
        /// unplugged, or the driver refused. Distinct from "open but silent", which
        /// only the caller can judge, and which the caller handles by moving on.</summary>
        bool IsFaulted { get; }
    }
}
