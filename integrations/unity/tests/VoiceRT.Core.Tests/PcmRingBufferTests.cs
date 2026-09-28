using System;
using System.Threading;
using System.Threading.Tasks;
using VoiceRT.Core;
using Xunit;

public class PcmRingBufferTests
{
    private static byte[] Ramp(int samples, int start = 0)
    {
        var b = new byte[samples * 2];
        for (int i = 0; i < samples; i++)
        {
            short v = (short)(start + i);
            b[2 * i] = (byte)(v & 0xFF);
            b[2 * i + 1] = (byte)((v >> 8) & 0xFF);
        }
        return b;
    }

    [Fact]
    public void RoundTripsExactSamples()
    {
        // The one test that catches an endianness mistake in the BlockCopy fast path.
        // A wrong byte order here is inaudible as anything but noise, and no FMOD test
        // would localise it.
        var ring = new PcmRingBuffer(4096);
        ring.WritePcm16(Ramp(1000, -500));
        var dst = new short[1000];
        Assert.Equal(1000, ring.ReadPcm16(dst, 0, 1000));
        for (int i = 0; i < 1000; i++) Assert.Equal((short)(-500 + i), dst[i]);
        Assert.Equal(0, ring.Available);
    }

    [Fact]
    public void UnderrunIsSilenceNotFailure()
    {
        var ring = new PcmRingBuffer(1024);
        ring.WritePcm16(Ramp(10, 1));
        var dst = new short[64];
        for (int i = 0; i < 64; i++) dst[i] = 0x7FFF;    // poison
        Assert.Equal(10, ring.ReadPcm16(dst, 0, 64));     // 10 real, 54 silence
        for (int i = 10; i < 64; i++) Assert.Equal(0, dst[i]);
        Assert.Equal(54, ring.Underruns);
    }

    [Fact]
    public void WrapsAroundCapacity()
    {
        var ring = new PcmRingBuffer(100);
        var dst = new short[60];
        for (int round = 0; round < 20; round++)
        {
            ring.WritePcm16(Ramp(60, round * 60));
            Assert.Equal(60, ring.ReadPcm16(dst, 0, 60));
            for (int i = 0; i < 60; i++) Assert.Equal((short)(round * 60 + i), dst[i]);
        }
        Assert.Equal(0, ring.Overruns);
    }

    [Fact]
    public void OverflowDropsOldestAndCountsOnce()
    {
        var ring = new PcmRingBuffer(100);
        ring.WritePcm16(Ramp(80));
        ring.WritePcm16(Ramp(80, 1000));    // 60 must be dropped
        Assert.Equal(100, ring.Available);
        Assert.Equal(60, ring.Overruns);
        var dst = new short[100];
        ring.ReadPcm16(dst, 0, 100);
        Assert.Equal((short)60, dst[0]);    // oldest 60 gone, ramp resumes at 60
    }

    [Fact]
    public void WriteLargerThanRingKeepsTail()
    {
        var ring = new PcmRingBuffer(64);
        ring.WritePcm16(Ramp(500));
        var dst = new short[64];
        ring.ReadPcm16(dst, 0, 64);
        Assert.Equal((short)(500 - 64), dst[0]);
    }

    [Fact]
    public void BargeInSuppressesInFlightAudioUntilFlush()
    {
        // This is the bug the current VoiceRTNpc.Interrupt() has: it clears the ring on
        // the main thread while the reader thread is still draining pre-interrupt AUDIO_OUT
        // out of the socket, so the NPC audibly resumes talking until FLUSH arrives.
        var ring = new PcmRingBuffer(4096);
        ring.WritePcm16(Ramp(500));

        ring.BeginBargeIn();                       // main thread, before the round trip
        Assert.Equal(0, ring.Available);

        ring.WritePcm16(Ramp(500));                // reader thread, stale audio still on the wire
        Assert.Equal(0, ring.Available);
        Assert.Equal(500, ring.SuppressedSamples);

        ring.EndBargeIn();                         // server FLUSH: the real cut point
        ring.WritePcm16(Ramp(120, 7));             // new turn
        Assert.Equal(120, ring.Available);
        var dst = new short[120];
        ring.ReadPcm16(dst, 0, 120);
        Assert.Equal((short)7, dst[0]);
    }

    [Fact]
    public async Task SurvivesConcurrentWriterAndMixer()
    {
        // Not a timing assertion - a data-race smoke test. Two threads hammering the
        // same lock for 2 s must produce no exception and no impossible counters.
        var ring = new PcmRingBuffer(16000 * 2);
        var stop = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        long produced = 0;

        var writer = Task.Run(() =>
        {
            var chunk = Ramp(1600);                       // 100 ms at 16 kHz
            while (!stop.IsCancellationRequested)
            {
                ring.WritePcm16(chunk);
                Interlocked.Add(ref produced, 1600);
                Thread.Sleep(20);                          // faster than realtime on purpose
            }
        });

        var mixer = Task.Run(() =>
        {
            var scratch = new short[1024];
            while (!stop.IsCancellationRequested)
            {
                ring.ReadPcm16(scratch, 0, 1024);
                Thread.Sleep(10);
            }
        });

        await Task.WhenAll(writer, mixer);
        Assert.True(ring.Available <= ring.Capacity);
        Assert.True(produced > 0);
    }

    [Fact]
    public void ClearDuringReadLeavesConsistentState()
    {
        var ring = new PcmRingBuffer(256);
        ring.WritePcm16(Ramp(200));
        ring.Clear();
        var dst = new short[10];
        Assert.Equal(0, ring.ReadPcm16(dst, 0, 10));
        Assert.Equal(10, ring.Underruns);
        ring.WritePcm16(Ramp(10, 42));
        Assert.Equal(10, ring.ReadPcm16(dst, 0, 10));
        Assert.Equal((short)42, dst[0]);
    }
}
