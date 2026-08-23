// VoiceRT engine bridge — wire protocol + PCM ring buffer.
// Header-only, standard C++17, no Unreal dependencies, so it compiles in a
// plain console test (see integrations/unreal/tests) and inside the plugin.
//
// Frame: type (1 byte) | length (4 bytes, big-endian) | payload
// Mirrors src/voicert/game/bridge.py. Keep the two in sync.

#pragma once

#include <cstdint>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace voicert
{
    enum class FrameType : uint8_t
    {
        // client -> server
        Hello = 0x01, TextIn = 0x02, AudioIn = 0x03, Event = 0x04, Interrupt = 0x05, Lod = 0x06,
        // server -> client
        Ready = 0x81, AudioOut = 0x82, TextOut = 0x83, TurnEnd = 0x84, Flush = 0x85, Tool = 0x86, Error = 0x8F,
    };

    struct Frame
    {
        FrameType type = FrameType::Error;
        std::vector<uint8_t> payload;

        std::string text() const { return std::string(payload.begin(), payload.end()); }
    };

    struct FrameCodec
    {
        static constexpr size_t HeaderSize = 5;
        static constexpr size_t MaxFrame = 4u * 1024u * 1024u;

        static std::vector<uint8_t> encode(FrameType type, const uint8_t* data, size_t len)
        {
            if (len > MaxFrame) throw std::length_error("payload too large");
            std::vector<uint8_t> out(HeaderSize + len);
            out[0] = static_cast<uint8_t>(type);
            out[1] = static_cast<uint8_t>((len >> 24) & 0xFF);
            out[2] = static_cast<uint8_t>((len >> 16) & 0xFF);
            out[3] = static_cast<uint8_t>((len >> 8) & 0xFF);
            out[4] = static_cast<uint8_t>(len & 0xFF);
            if (len) std::memcpy(out.data() + HeaderSize, data, len);
            return out;
        }

        static std::vector<uint8_t> encode(FrameType type) { return encode(type, nullptr, 0); }

        static std::vector<uint8_t> encodeText(FrameType type, const std::string& s)
        {
            return encode(type, reinterpret_cast<const uint8_t*>(s.data()), s.size());
        }
    };

    /// Incremental parser: feed socket bytes, pop whole frames.
    class FrameParser
    {
    public:
        void feed(const uint8_t* data, size_t len) { buf_.insert(buf_.end(), data, data + len); }

        bool tryPop(Frame& out)
        {
            if (buf_.size() < FrameCodec::HeaderSize) return false;
            const uint32_t len = (uint32_t(buf_[1]) << 24) | (uint32_t(buf_[2]) << 16) |
                                 (uint32_t(buf_[3]) << 8) | uint32_t(buf_[4]);
            if (len > FrameCodec::MaxFrame) throw std::length_error("incoming frame too large");
            if (buf_.size() < FrameCodec::HeaderSize + len) return false;
            out.type = static_cast<FrameType>(buf_[0]);
            out.payload.assign(buf_.begin() + FrameCodec::HeaderSize,
                               buf_.begin() + FrameCodec::HeaderSize + len);
            buf_.erase(buf_.begin(), buf_.begin() + FrameCodec::HeaderSize + len);
            return true;
        }

        size_t buffered() const { return buf_.size(); }

    private:
        std::vector<uint8_t> buf_;
    };

    /// Just enough JSON for the bridge messages we author ourselves.
    struct MiniJson
    {
        static std::string escape(const std::string& s)
        {
            std::string o; o.reserve(s.size() + 8);
            for (unsigned char c : s)
            {
                switch (c)
                {
                case '"': o += "\\\""; break;
                case '\\': o += "\\\\"; break;
                case '\n': o += "\\n"; break;
                case '\r': o += "\\r"; break;
                case '\t': o += "\\t"; break;
                default:
                    if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
                    else o += static_cast<char>(c);
                }
            }
            return o;
        }
        static std::string str(const std::string& s) { return "\"" + escape(s) + "\""; }

        /// Top-level string field; empty string if absent.
        static std::string getString(const std::string& j, const std::string& key)
        {
            size_t i = findKey(j, key);
            if (i == npos) return {};
            i = skipWs(j, i);
            if (i >= j.size() || j[i] != '"') return {};
            ++i;
            std::string out;
            while (i < j.size())
            {
                char c = j[i++];
                if (c == '\\' && i < j.size())
                {
                    char e = j[i++];
                    switch (e)
                    {
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u':
                        if (i + 4 <= j.size()) { out += static_cast<char>(std::stoi(j.substr(i, 4), nullptr, 16)); i += 4; }
                        break;
                    default: out += e;
                    }
                }
                else if (c == '"') return out;
                else out += c;
            }
            return out;
        }

        static int getInt(const std::string& j, const std::string& key, int fallback = 0)
        {
            size_t i = findKey(j, key);
            if (i == npos) return fallback;
            i = skipWs(j, i);
            size_t start = i;
            if (i < j.size() && j[i] == '-') ++i;
            while (i < j.size() && std::isdigit(static_cast<unsigned char>(j[i]))) ++i;
            if (start == i) return fallback;
            return std::stoi(j.substr(start, i - start));
        }

        /// Raw text of a top-level object/array field, for pass-through.
        static std::string getRaw(const std::string& j, const std::string& key)
        {
            size_t i = findKey(j, key);
            if (i == npos) return {};
            i = skipWs(j, i);
            if (i >= j.size()) return {};
            char open = j[i];
            if (open == '{' || open == '[')
            {
                char close = open == '{' ? '}' : ']';
                int depth = 0; bool inStr = false;
                for (size_t k = i; k < j.size(); ++k)
                {
                    char c = j[k];
                    if (inStr) { if (c == '\\') ++k; else if (c == '"') inStr = false; continue; }
                    if (c == '"') inStr = true;
                    else if (c == open) ++depth;
                    else if (c == close && --depth == 0) return j.substr(i, k - i + 1);
                }
                return {};
            }
            size_t end = i;
            while (end < j.size() && j[end] != ',' && j[end] != '}') ++end;
            return j.substr(i, end - i);
        }

    private:
        static constexpr size_t npos = std::string::npos;

        static size_t skipWs(const std::string& s, size_t i)
        {
            while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i]))) ++i;
            return i;
        }

        // top-level keys only (depth == 1)
        static size_t findKey(const std::string& j, const std::string& key)
        {
            const std::string needle = "\"" + key + "\"";
            int depth = 0; bool inStr = false;
            for (size_t i = 0; i < j.size(); ++i)
            {
                char c = j[i];
                if (inStr) { if (c == '\\') ++i; else if (c == '"') inStr = false; continue; }
                if (c == '"')
                {
                    if (depth == 1 && j.compare(i, needle.size(), needle) == 0)
                    {
                        size_t after = skipWs(j, i + needle.size());
                        if (after < j.size() && j[after] == ':') return after + 1;
                    }
                    inStr = true;
                }
                else if (c == '{' || c == '[') ++depth;
                else if (c == '}' || c == ']') --depth;
            }
            return npos;
        }
    };

    /// PCM16 ring between the network thread and whoever drains audio.
    /// Unreal's USoundWaveProcedural takes int16 at the wave's own sample
    /// rate and resamples in the mixer, so this ring stays at 16 kHz int16
    /// and offers a plain drain(); the float/resampling path is there for
    /// engines (or tests) that want it.
    class PcmRingBuffer
    {
    public:
        explicit PcmRingBuffer(size_t capacitySamples) : buf_(capacitySamples < 16 ? 16 : capacitySamples) {}

        void writePcm16(const uint8_t* bytes, size_t byteCount)
        {
            const size_t n = byteCount / 2;
            std::lock_guard<std::mutex> g(m_);
            for (size_t i = 0; i < n; ++i)
            {
                int16_t s = static_cast<int16_t>(bytes[2 * i] | (bytes[2 * i + 1] << 8));
                if (count_ == buf_.size()) { read_ = (read_ + 1) % buf_.size(); --count_; ++overruns_; }
                buf_[(read_ + count_) % buf_.size()] = s;
                ++count_;
            }
        }

        /// Move up to maxSamples int16 out (for USoundWaveProcedural::QueueAudio).
        size_t drain(std::vector<int16_t>& out, size_t maxSamples)
        {
            std::lock_guard<std::mutex> g(m_);
            size_t n = count_ < maxSamples ? count_ : maxSamples;
            out.resize(n);
            for (size_t i = 0; i < n; ++i) { out[i] = buf_[read_]; read_ = (read_ + 1) % buf_.size(); }
            count_ -= n;
            return n;
        }

        /// Resampled float read (linear), mono fanned to `channels`. Silence on starvation.
        size_t readResampled(float* dst, size_t frames, int channels, int srcRate, int dstRate)
        {
            const double step = double(srcRate) / double(dstRate);
            size_t served = 0;
            std::lock_guard<std::mutex> g(m_);
            for (size_t f = 0; f < frames; ++f)
            {
                if (count_ == 0)
                {
                    for (int c = 0; c < channels; ++c) dst[f * channels + c] = 0.f;
                    ++underruns_;
                    continue;
                }
                const float a = buf_[read_] / 32768.f;
                // With only one sample left there is nothing to interpolate
                // towards: hold it, then consume it. Otherwise the final
                // sample of every utterance would sit in the ring forever.
                const float b = count_ > 1 ? buf_[(read_ + 1) % buf_.size()] / 32768.f : a;
                const float v = a + (b - a) * static_cast<float>(phase_);
                for (int c = 0; c < channels; ++c) dst[f * channels + c] = v;
                ++served;
                phase_ += step;
                while (phase_ >= 1.0 && count_ > 0)
                {
                    phase_ -= 1.0;
                    read_ = (read_ + 1) % buf_.size();
                    --count_;
                }
                if (count_ == 0) phase_ = 0.0;
            }
            return served;
        }

        void clear() { std::lock_guard<std::mutex> g(m_); read_ = 0; count_ = 0; phase_ = 0; }
        size_t available() const { std::lock_guard<std::mutex> g(m_); return count_; }
        size_t capacity() const { return buf_.size(); }
        uint64_t underruns() const { return underruns_; }
        uint64_t overruns() const { return overruns_; }

    private:
        mutable std::mutex m_;
        std::vector<int16_t> buf_;
        size_t read_ = 0;
        size_t count_ = 0;
        double phase_ = 0.0;
        uint64_t underruns_ = 0;
        uint64_t overruns_ = 0;
    };
} // namespace voicert
