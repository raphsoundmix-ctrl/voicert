// Where a VoiceRTNpc's generated voice goes.
//
// The thread contract is part of this interface, not an implementation detail:
// the network reader thread calls Write/Flush directly (no marshalling, that is
// the whole point), and everything else is main thread.

namespace VoiceRT
{
    public interface IVoiceRTAudioSink
    {
        /// <summary>MAIN THREAD. Called once, when READY names the stream's sample rate.
        /// Sizes buffers. Must be called before Begin().</summary>
        void Configure(int sourceSampleRate);

        /// <summary>MAIN THREAD. Start the voice. Idempotent.</summary>
        void Begin();

        /// <summary>NETWORK READER THREAD. Little-endian PCM16 mono at the configured
        /// rate. Must not allocate, must not block for long, must not touch UnityEngine.</summary>
        void Write(byte[] pcm16le, int offset, int count);

        /// <summary>NETWORK READER THREAD. Convenience overload.</summary>
        void Write(byte[] pcm16le);

        /// <summary>NETWORK READER THREAD. The server's FLUSH arrived: this is the
        /// authoritative cut point, in stream order. Drop everything queued.</summary>
        void Flush();

        /// <summary>MAIN THREAD. Local barge-in, fired before the server round trip.
        /// Drops the queue AND suppresses audio already in flight until Flush() lands.</summary>
        void BargeIn();

        /// <summary>MAIN THREAD. Stop and release everything. Idempotent.</summary>
        void End();

        /// <summary>Milliseconds of voice currently buffered. Cheap enough for Update.</summary>
        int BufferedMilliseconds { get; }
    }
}
