using System;
using System.Text;
using VoiceRT.Core;
using Xunit;

namespace VoiceRT.Tests
{
    public class ProtocolTests
    {
        [Fact]
        public void Encode_writes_type_and_big_endian_length()
        {
            var f = FrameCodec.EncodeText(FrameType.TextIn, "hello");
            Assert.Equal((byte)FrameType.TextIn, f[0]);
            Assert.Equal(new byte[] { 0, 0, 0, 5 }, new[] { f[1], f[2], f[3], f[4] });
            Assert.Equal("hello", Encoding.UTF8.GetString(f, 5, 5));
        }

        [Fact]
        public void Empty_frame_is_five_bytes()
        {
            Assert.Equal(5, FrameCodec.Encode(FrameType.Interrupt).Length);
        }

        [Fact]
        public void Parser_reassembles_frames_split_across_reads()
        {
            var a = FrameCodec.EncodeText(FrameType.TextOut, "{\"text\":\"hi\",\"turn_id\":2}");
            var b = FrameCodec.Encode(FrameType.Flush);
            var all = new byte[a.Length + b.Length];
            Buffer.BlockCopy(a, 0, all, 0, a.Length);
            Buffer.BlockCopy(b, 0, all, a.Length, b.Length);

            var p = new FrameParser();
            // feed one byte at a time: worst-case fragmentation
            for (int i = 0; i < all.Length; i++)
            {
                p.Feed(all, i, 1);
            }
            Assert.True(p.TryPop(out var f1));
            Assert.Equal(FrameType.TextOut, f1.Type);
            Assert.Equal("hi", MiniJson.GetString(f1.PayloadText, "text"));
            Assert.Equal(2, MiniJson.GetInt(f1.PayloadText, "turn_id"));
            Assert.True(p.TryPop(out var f2));
            Assert.Equal(FrameType.Flush, f2.Type);
            Assert.Empty(f2.Payload);
            Assert.False(p.TryPop(out _));
            Assert.Equal(0, p.Buffered);
        }

        [Fact]
        public void Parser_rejects_oversized_frame()
        {
            var p = new FrameParser();
            p.Feed(new byte[] { (byte)FrameType.AudioOut, 0xFF, 0xFF, 0xFF, 0xFF }, 0, 5);
            Assert.Throws<InvalidOperationException>(() => p.TryPop(out _));
        }

        [Fact]
        public void MiniJson_roundtrips_escapes_and_nested_objects()
        {
            var json = MiniJson.Object(
                ("text", MiniJson.Str("he said \"hi\"\nthen left")),
                ("turn_id", "7"),
                ("metrics", "{\"ttfb\":12,\"tags\":[\"a\",\"b\"]}"));
            Assert.Equal("he said \"hi\"\nthen left", MiniJson.GetString(json, "text"));
            Assert.Equal(7, MiniJson.GetInt(json, "turn_id"));
            Assert.Equal("{\"ttfb\":12,\"tags\":[\"a\",\"b\"]}", MiniJson.GetRaw(json, "metrics"));
            // a nested "text" key must not shadow the top-level one
            var nested = "{\"inner\":{\"text\":\"nope\"},\"text\":\"yes\"}";
            Assert.Equal("yes", MiniJson.GetString(nested, "text"));
            Assert.Null(MiniJson.GetString(nested, "missing"));
        }
    }

    public class RingBufferTests
    {
        private static byte[] Pcm(params short[] samples)
        {
            var b = new byte[samples.Length * 2];
            for (int i = 0; i < samples.Length; i++)
            {
                b[2 * i] = (byte)(samples[i] & 0xFF);
                b[2 * i + 1] = (byte)((samples[i] >> 8) & 0xFF);
            }
            return b;
        }

        [Fact]
        public void Same_rate_read_returns_input_scaled_to_float()
        {
            var ring = new PcmRingBuffer(1024);
            ring.WritePcm16(Pcm(0, 16384, -16384, 32767, 0));
            var dst = new float[4];
            int served = ring.ReadResampled(dst, 1, 16000, 16000);
            Assert.Equal(4, served);
            Assert.Equal(0f, dst[0], 3);
            Assert.Equal(0.5f, dst[1], 3);
            Assert.Equal(-0.5f, dst[2], 3);
            Assert.InRange(dst[3], 0.99f, 1.0f);
        }

        [Fact]
        public void Upsampling_to_48k_triples_frame_count_and_fans_out_channels()
        {
            var ring = new PcmRingBuffer(4096);
            var src = new short[160];                       // 10 ms at 16 kHz
            for (int i = 0; i < src.Length; i++) src[i] = (short)(i * 100);
            ring.WritePcm16(Pcm(src));
            var dst = new float[480 * 2];                   // 10 ms at 48 kHz, stereo
            int served = ring.ReadResampled(dst, 2, 16000, 48000);
            Assert.InRange(served, 470, 480);
            Assert.Equal(dst[10], dst[11]);                 // mono fanned to both channels
            // interpolation keeps it monotonic for a ramp
            for (int f = 1; f < served; f++) Assert.True(dst[f * 2] >= dst[(f - 1) * 2]);
        }

        [Fact]
        public void Last_sample_of_an_utterance_is_played_not_stranded()
        {
            // Regression: an interpolating reader that insists on a lookahead
            // sample leaves the final sample of every utterance in the ring.
            var ring = new PcmRingBuffer(64);
            ring.WritePcm16(Pcm(1000, 2000, 3000, 4000));
            var dst = new float[4];
            int served = ring.ReadResampled(dst, 1, 16000, 16000);
            Assert.Equal(4, served);
            Assert.Equal(4000f / 32768f, dst[3], 5);
            Assert.Equal(0, ring.Available);
            // and the next read is clean silence, not a held DC value
            served = ring.ReadResampled(dst, 1, 16000, 16000);
            Assert.Equal(0, served);
            Assert.All(dst, v => Assert.Equal(0f, v));
        }

        [Fact]
        public void Starved_reader_writes_silence_and_counts_underruns()
        {
            var ring = new PcmRingBuffer(256);
            var dst = new float[32];
            int served = ring.ReadResampled(dst, 1, 16000, 16000);
            Assert.Equal(0, served);
            Assert.All(dst, v => Assert.Equal(0f, v));
            Assert.Equal(32, ring.Underruns);
        }

        [Fact]
        public void Clear_is_the_barge_in_path()
        {
            var ring = new PcmRingBuffer(1024);
            ring.WritePcm16(Pcm(new short[500]));
            Assert.Equal(500, ring.Available);
            ring.Clear();
            Assert.Equal(0, ring.Available);
        }

        [Fact]
        public void Overrun_drops_oldest_not_newest()
        {
            var ring = new PcmRingBuffer(16);
            var s = new short[32];
            for (int i = 0; i < 32; i++) s[i] = (short)i;
            ring.WritePcm16(Pcm(s));
            Assert.Equal(16, ring.Available);
            Assert.True(ring.Overruns > 0);
            var dst = new float[1];
            ring.ReadResampled(dst, 1, 16000, 16000);
            Assert.Equal(16f / 32768f, dst[0], 5);       // oldest surviving sample is #16
        }
    }
}
