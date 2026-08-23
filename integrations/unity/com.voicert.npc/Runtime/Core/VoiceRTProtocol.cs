// VoiceRT engine bridge — wire protocol (engine-agnostic, no UnityEngine references).
//
// Frame layout:  type (1 byte) | length (4 bytes, big-endian) | payload
// Mirrors src/voicert/game/bridge.py exactly. Keep the two in sync.

using System;
using System.Collections.Generic;
using System.Text;

namespace VoiceRT.Core
{
    /// <summary>Frame types shared with the Python bridge.</summary>
    public enum FrameType : byte
    {
        // client -> server
        Hello = 0x01,
        TextIn = 0x02,
        AudioIn = 0x03,
        Event = 0x04,
        Interrupt = 0x05,
        Lod = 0x06,

        // server -> client
        Ready = 0x81,
        AudioOut = 0x82,
        TextOut = 0x83,
        TurnEnd = 0x84,
        Flush = 0x85,
        Tool = 0x86,
        Error = 0x8F,
    }

    public readonly struct Frame
    {
        public readonly FrameType Type;
        public readonly byte[] Payload;

        public Frame(FrameType type, byte[] payload)
        {
            Type = type;
            Payload = payload ?? Array.Empty<byte>();
        }

        public string PayloadText => Encoding.UTF8.GetString(Payload);
    }

    public static class FrameCodec
    {
        public const int HeaderSize = 5;
        public const int MaxFrame = 4 * 1024 * 1024;

        public static byte[] Encode(FrameType type, byte[] payload)
        {
            payload ??= Array.Empty<byte>();
            if (payload.Length > MaxFrame)
                throw new ArgumentException($"payload too large: {payload.Length}");
            var buf = new byte[HeaderSize + payload.Length];
            buf[0] = (byte)type;
            // big-endian uint32, matches struct.Struct("!BI") on the Python side
            buf[1] = (byte)(payload.Length >> 24);
            buf[2] = (byte)(payload.Length >> 16);
            buf[3] = (byte)(payload.Length >> 8);
            buf[4] = (byte)payload.Length;
            Buffer.BlockCopy(payload, 0, buf, HeaderSize, payload.Length);
            return buf;
        }

        public static byte[] Encode(FrameType type) => Encode(type, Array.Empty<byte>());

        public static byte[] EncodeText(FrameType type, string text) =>
            Encode(type, Encoding.UTF8.GetBytes(text ?? string.Empty));
    }

    /// <summary>
    /// Incremental parser: feed arbitrary byte chunks from the socket, pop whole frames.
    /// Handles frames split across reads and several frames in one read.
    /// </summary>
    public sealed class FrameParser
    {
        private readonly List<byte> _buf = new List<byte>(64 * 1024);

        public void Feed(byte[] data, int offset, int count)
        {
            for (int i = 0; i < count; i++) _buf.Add(data[offset + i]);
        }

        public bool TryPop(out Frame frame)
        {
            frame = default;
            if (_buf.Count < FrameCodec.HeaderSize) return false;
            var type = (FrameType)_buf[0];
            long length = ((long)_buf[1] << 24) | ((long)_buf[2] << 16) | ((long)_buf[3] << 8) | _buf[4];
            if (length > FrameCodec.MaxFrame)
                throw new InvalidOperationException($"incoming frame too large: {length}");
            if (_buf.Count < FrameCodec.HeaderSize + length) return false;
            var payload = new byte[length];
            _buf.CopyTo(FrameCodec.HeaderSize, payload, 0, (int)length);
            _buf.RemoveRange(0, FrameCodec.HeaderSize + (int)length);
            frame = new Frame(type, payload);
            return true;
        }

        public int Buffered => _buf.Count;
    }

    /// <summary>
    /// Just enough JSON for the bridge: we produce small objects and read
    /// string/int fields from messages we authored ourselves. No dependency
    /// on Newtonsoft so the core compiles anywhere.
    /// </summary>
    public static class MiniJson
    {
        public static string Escape(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder(s.Length + 8);
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.ToString();
        }

        public static string Object(params (string key, string rawValue)[] fields)
        {
            var sb = new StringBuilder("{");
            for (int i = 0; i < fields.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(Escape(fields[i].key)).Append("\":").Append(fields[i].rawValue);
            }
            return sb.Append('}').ToString();
        }

        public static string Str(string value) => "\"" + Escape(value) + "\"";

        /// <summary>Extract a top-level string field. Returns null if absent.</summary>
        public static string GetString(string json, string key)
        {
            int idx = FindKey(json, key);
            if (idx < 0) return null;
            int i = SkipWs(json, idx);
            if (i >= json.Length || json[i] != '"') return null;
            i++;
            var sb = new StringBuilder();
            while (i < json.Length)
            {
                char c = json[i++];
                if (c == '\\' && i < json.Length)
                {
                    char e = json[i++];
                    switch (e)
                    {
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'u':
                            if (i + 4 <= json.Length)
                            {
                                sb.Append((char)Convert.ToInt32(json.Substring(i, 4), 16));
                                i += 4;
                            }
                            break;
                        default: sb.Append(e); break;
                    }
                }
                else if (c == '"') return sb.ToString();
                else sb.Append(c);
            }
            return sb.ToString();
        }

        /// <summary>Extract a top-level integer field. Returns fallback if absent.</summary>
        public static int GetInt(string json, string key, int fallback = 0)
        {
            int idx = FindKey(json, key);
            if (idx < 0) return fallback;
            int i = SkipWs(json, idx);
            int start = i;
            if (i < json.Length && json[i] == '-') i++;
            while (i < json.Length && char.IsDigit(json[i])) i++;
            return int.TryParse(json.Substring(start, i - start), out var v) ? v : fallback;
        }

        /// <summary>Raw text of a top-level object/array/scalar field (for pass-through).</summary>
        public static string GetRaw(string json, string key)
        {
            int idx = FindKey(json, key);
            if (idx < 0) return null;
            int i = SkipWs(json, idx);
            if (i >= json.Length) return null;
            char open = json[i];
            if (open == '{' || open == '[')
            {
                char close = open == '{' ? '}' : ']';
                int depth = 0; bool inStr = false;
                for (int j = i; j < json.Length; j++)
                {
                    char c = json[j];
                    if (inStr) { if (c == '\\') j++; else if (c == '"') inStr = false; continue; }
                    if (c == '"') inStr = true;
                    else if (c == open) depth++;
                    else if (c == close && --depth == 0) return json.Substring(i, j - i + 1);
                }
                return null;
            }
            if (open == '"') return Str(GetString(json, key));
            int end = i;
            while (end < json.Length && json[end] != ',' && json[end] != '}') end++;
            return json.Substring(i, end - i).Trim();
        }

        private static int FindKey(string json, string key)
        {
            // top-level keys only: track depth so nested objects cannot match
            string needle = "\"" + key + "\"";
            int depth = 0; bool inStr = false;
            for (int i = 0; i < json.Length; i++)
            {
                char c = json[i];
                if (inStr) { if (c == '\\') i++; else if (c == '"') inStr = false; continue; }
                if (c == '"')
                {
                    if (depth == 1 && string.CompareOrdinal(json, i, needle, 0, needle.Length) == 0)
                    {
                        int after = i + needle.Length;
                        after = SkipWs(json, after);
                        if (after < json.Length && json[after] == ':') return after + 1;
                    }
                    inStr = true;
                }
                else if (c == '{' || c == '[') depth++;
                else if (c == '}' || c == ']') depth--;
            }
            return -1;
        }

        private static int SkipWs(string s, int i)
        {
            while (i < s.Length && char.IsWhiteSpace(s[i])) i++;
            return i;
        }
    }
}
