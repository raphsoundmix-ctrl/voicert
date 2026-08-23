// Standalone test for VoiceRTProtocol.h — no Unreal required.
// Build + run: build_and_test.bat (MSVC) or
//   g++ -std=c++17 -I../VoiceRT/Source/VoiceRT/Public protocol_test.cpp -o protocol_test && ./protocol_test

#include "VoiceRTProtocol.h"

#include <cstdio>
#include <cstdlib>
#include <cmath>

using namespace voicert;

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); ++failures; } } while (0)

static std::vector<uint8_t> pcm(std::initializer_list<int16_t> samples)
{
    std::vector<uint8_t> b;
    for (int16_t s : samples) { b.push_back(uint8_t(s & 0xFF)); b.push_back(uint8_t((s >> 8) & 0xFF)); }
    return b;
}

int main()
{
    // -- codec ----------------------------------------------------------
    {
        auto f = FrameCodec::encodeText(FrameType::TextIn, "hello");
        CHECK(f.size() == 10);
        CHECK(f[0] == 0x02);
        CHECK(f[1] == 0 && f[2] == 0 && f[3] == 0 && f[4] == 5);
        CHECK(std::string(f.begin() + 5, f.end()) == "hello");
        CHECK(FrameCodec::encode(FrameType::Interrupt).size() == 5);
    }

    // -- parser: worst-case fragmentation, two frames back to back --------
    {
        auto a = FrameCodec::encodeText(FrameType::TextOut, "{\"text\":\"hi\",\"turn_id\":2}");
        auto b = FrameCodec::encode(FrameType::Flush);
        std::vector<uint8_t> all(a); all.insert(all.end(), b.begin(), b.end());
        FrameParser p;
        for (uint8_t byte : all) p.feed(&byte, 1);
        Frame f1, f2, f3;
        CHECK(p.tryPop(f1));
        CHECK(f1.type == FrameType::TextOut);
        CHECK(MiniJson::getString(f1.text(), "text") == "hi");
        CHECK(MiniJson::getInt(f1.text(), "turn_id") == 2);
        CHECK(p.tryPop(f2));
        CHECK(f2.type == FrameType::Flush && f2.payload.empty());
        CHECK(!p.tryPop(f3));
        CHECK(p.buffered() == 0);
    }

    // -- parser rejects oversized --------------------------------------------
    {
        FrameParser p;
        uint8_t bad[5] = { 0x82, 0xFF, 0xFF, 0xFF, 0xFF };
        p.feed(bad, 5);
        bool threw = false;
        try { Frame f; p.tryPop(f); } catch (const std::length_error&) { threw = true; }
        CHECK(threw);
    }

    // -- MiniJson --------------------------------------------------------------
    {
        std::string j = "{\"tool_name\":\"play_animation\",\"arguments\":{\"animation\":\"wave\",\"n\":{\"x\":1}},\"call_id\":\"c1\",\"turn_id\":-3}";
        CHECK(MiniJson::getString(j, "tool_name") == "play_animation");
        CHECK(MiniJson::getRaw(j, "arguments") == "{\"animation\":\"wave\",\"n\":{\"x\":1}}");
        CHECK(MiniJson::getInt(j, "turn_id") == -3);
        CHECK(MiniJson::getString(j, "missing").empty());
        std::string nested = "{\"inner\":{\"text\":\"nope\"},\"text\":\"yes\"}";
        CHECK(MiniJson::getString(nested, "text") == "yes");
        CHECK(MiniJson::escape("a\"b\\c\nd") == "a\\\"b\\\\c\\nd");
    }

    // -- ring: same-rate read scales to float ------------------------------------
    {
        PcmRingBuffer ring(1024);
        auto bytes = pcm({ 0, 16384, -16384, 32767 });
        ring.writePcm16(bytes.data(), bytes.size());
        float dst[4];
        size_t served = ring.readResampled(dst, 4, 1, 16000, 16000);
        CHECK(served == 4);
        CHECK(std::fabs(dst[0]) < 1e-3f);
        CHECK(std::fabs(dst[1] - 0.5f) < 1e-3f);
        CHECK(std::fabs(dst[2] + 0.5f) < 1e-3f);
        CHECK(dst[3] > 0.99f);
    }

    // -- ring: last sample of an utterance is played, not stranded -----------------------
    {
        PcmRingBuffer ring(64);
        auto bytes = pcm({ 1000, 2000, 3000, 4000 });
        ring.writePcm16(bytes.data(), bytes.size());
        float dst[4];
        CHECK(ring.readResampled(dst, 4, 1, 16000, 16000) == 4);
        CHECK(std::fabs(dst[3] - 4000.f / 32768.f) < 1e-5f);
        CHECK(ring.available() == 0);
        CHECK(ring.readResampled(dst, 4, 1, 16000, 16000) == 0);   // clean silence afterwards
        CHECK(dst[0] == 0.f && dst[3] == 0.f);
    }

    // -- ring: 16k -> 48k upsample, stereo fan-out --------------------------------
    {
        PcmRingBuffer ring(4096);
        std::vector<uint8_t> src;
        for (int i = 0; i < 160; ++i) { int16_t s = int16_t(i * 100); src.push_back(uint8_t(s & 0xFF)); src.push_back(uint8_t((s >> 8) & 0xFF)); }
        ring.writePcm16(src.data(), src.size());
        std::vector<float> dst(480 * 2);
        size_t served = ring.readResampled(dst.data(), 480, 2, 16000, 48000);
        CHECK(served >= 470 && served <= 480);
        CHECK(dst[10] == dst[11]);
        for (size_t f = 1; f < served; ++f) CHECK(dst[f * 2] >= dst[(f - 1) * 2]);
    }

    // -- ring: drain for USoundWaveProcedural + clear on barge-in ---------------------
    {
        PcmRingBuffer ring(256);
        auto bytes = pcm({ 1, 2, 3, 4, 5 });
        ring.writePcm16(bytes.data(), bytes.size());
        std::vector<int16_t> out;
        CHECK(ring.drain(out, 3) == 3);
        CHECK(out[0] == 1 && out[2] == 3);
        CHECK(ring.available() == 2);
        ring.clear();
        CHECK(ring.available() == 0);
        float silence[8];
        CHECK(ring.readResampled(silence, 8, 1, 16000, 16000) == 0);
        CHECK(ring.underruns() == 8);
    }

    // -- ring: overrun drops oldest -----------------------------------------------------
    {
        PcmRingBuffer ring(16);
        std::vector<uint8_t> src;
        for (int i = 0; i < 32; ++i) { int16_t s = int16_t(i); src.push_back(uint8_t(s & 0xFF)); src.push_back(uint8_t((s >> 8) & 0xFF)); }
        ring.writePcm16(src.data(), src.size());
        CHECK(ring.available() == 16);
        CHECK(ring.overruns() > 0);
        std::vector<int16_t> out;
        ring.drain(out, 1);
        CHECK(out[0] == 16);
    }

    if (failures == 0) { std::printf("protocol_test: all checks passed\n"); return 0; }
    std::printf("protocol_test: %d failure(s)\n", failures);
    return 1;
}
