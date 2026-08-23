// TCP client for the VoiceRT engine bridge. Pure .NET: usable from Unity,
// from a plain console app, or from the xunit tests in this repo.
//
// Threading: Connect() starts one background reader thread. Every event
// below fires ON THAT THREAD. The Unity component marshals text/tool events
// to the main thread; audio goes straight into a PcmRingBuffer, which is
// thread-safe by design.

using System;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace VoiceRT.Core
{
    public sealed class HelloOptions
    {
        public string NpcId = "npc";
        public string Character = "";
        public string LoreScope = "";
        public string Voice = "default";
    }

    public sealed class VoiceRTClient : IDisposable
    {
        public event Action Ready;
        public event Action<byte[]> AudioReceived;                 // PCM16 mono 16 kHz
        public event Action<string, int> TextReceived;             // text, turnId
        public event Action<int, string> TurnEnded;                // turnId, metrics JSON
        public event Action Flushed;                               // drop all queued audio NOW
        public event Action<string, string, string> ToolCalled;    // name, args JSON, callId
        public event Action<string> ErrorReceived;
        public event Action<Exception> Disconnected;

        private TcpClient _tcp;
        private NetworkStream _stream;
        private Thread _reader;
        private volatile bool _running;
        private readonly object _sendLock = new object();

        public bool IsConnected => _running && _tcp != null && _tcp.Connected;
        public int SampleRate { get; private set; } = 16000;

        public void Connect(string host, int port, HelloOptions hello, int timeoutMs = 3000)
        {
            if (hello == null) throw new ArgumentNullException(nameof(hello));
            Close();
            _tcp = new TcpClient { NoDelay = true };
            var ar = _tcp.BeginConnect(host, port, null, null);
            if (!ar.AsyncWaitHandle.WaitOne(timeoutMs))
            {
                _tcp.Close();
                throw new TimeoutException($"VoiceRT bridge at {host}:{port} did not answer in {timeoutMs} ms");
            }
            _tcp.EndConnect(ar);
            _stream = _tcp.GetStream();
            _running = true;

            string helloJson = MiniJson.Object(
                ("proto", "1"),
                ("npc_id", MiniJson.Str(hello.NpcId)),
                ("character", MiniJson.Str(hello.Character)),
                ("lore_scope", MiniJson.Str(hello.LoreScope)),
                ("voice", MiniJson.Str(hello.Voice)));
            Send(FrameCodec.EncodeText(FrameType.Hello, helloJson));

            _reader = new Thread(ReadLoop) { IsBackground = true, Name = "VoiceRT-reader" };
            _reader.Start();
        }

        // -- outbound ------------------------------------------------------

        public void SendText(string text) => Send(FrameCodec.EncodeText(FrameType.TextIn, text));

        public void SendAudio(byte[] pcm16Mono16k) => Send(FrameCodec.Encode(FrameType.AudioIn, pcm16Mono16k));

        public void SendEvent(string name, string payloadJson = "{}") =>
            Send(FrameCodec.EncodeText(FrameType.Event,
                MiniJson.Object(("event", MiniJson.Str(name)), ("payload", string.IsNullOrEmpty(payloadJson) ? "{}" : payloadJson))));

        public void SendInterrupt() => Send(FrameCodec.Encode(FrameType.Interrupt));

        public void SendLod(string tier, float distanceMeters, float priority) =>
            Send(FrameCodec.EncodeText(FrameType.Lod,
                MiniJson.Object(
                    ("tier", MiniJson.Str(tier)),
                    ("distance_m", distanceMeters.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture)),
                    ("priority", priority.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)))));

        private void Send(byte[] frame)
        {
            var s = _stream;
            if (s == null) return;
            lock (_sendLock)
            {
                try { s.Write(frame, 0, frame.Length); }
                catch (Exception ex) { Fail(ex); }
            }
        }

        // -- inbound -------------------------------------------------------

        private void ReadLoop()
        {
            var parser = new FrameParser();
            var chunk = new byte[32 * 1024];
            try
            {
                while (_running)
                {
                    int n = _stream.Read(chunk, 0, chunk.Length);
                    if (n <= 0) break;
                    parser.Feed(chunk, 0, n);
                    while (parser.TryPop(out var frame)) Dispatch(frame);
                }
                Fail(null);
            }
            catch (Exception ex)
            {
                if (_running) Fail(ex);
            }
        }

        private void Dispatch(Frame f)
        {
            switch (f.Type)
            {
                case FrameType.Ready:
                    SampleRate = MiniJson.GetInt(f.PayloadText, "sample_rate", 16000);
                    Ready?.Invoke();
                    break;
                case FrameType.AudioOut:
                    AudioReceived?.Invoke(f.Payload);
                    break;
                case FrameType.TextOut:
                {
                    var j = f.PayloadText;
                    TextReceived?.Invoke(MiniJson.GetString(j, "text") ?? "", MiniJson.GetInt(j, "turn_id"));
                    break;
                }
                case FrameType.TurnEnd:
                {
                    var j = f.PayloadText;
                    TurnEnded?.Invoke(MiniJson.GetInt(j, "turn_id"), MiniJson.GetRaw(j, "metrics") ?? "{}");
                    break;
                }
                case FrameType.Flush:
                    Flushed?.Invoke();
                    break;
                case FrameType.Tool:
                {
                    var j = f.PayloadText;
                    ToolCalled?.Invoke(
                        MiniJson.GetString(j, "tool_name") ?? "",
                        MiniJson.GetRaw(j, "arguments") ?? "{}",
                        MiniJson.GetString(j, "call_id") ?? "");
                    break;
                }
                case FrameType.Error:
                    ErrorReceived?.Invoke(MiniJson.GetString(f.PayloadText, "message") ?? f.PayloadText);
                    break;
                default:
                    ErrorReceived?.Invoke($"unknown frame type 0x{(byte)f.Type:X2}");
                    break;
            }
        }

        private void Fail(Exception ex)
        {
            if (!_running) return;
            _running = false;
            Disconnected?.Invoke(ex);
        }

        public void Close()
        {
            _running = false;
            try { _stream?.Close(); } catch { /* closing */ }
            try { _tcp?.Close(); } catch { /* closing */ }
            _stream = null;
            _tcp = null;
            if (_reader != null && _reader.IsAlive && Thread.CurrentThread != _reader)
                _reader.Join(500);
            _reader = null;
        }

        public void Dispose() => Close();
    }
}
