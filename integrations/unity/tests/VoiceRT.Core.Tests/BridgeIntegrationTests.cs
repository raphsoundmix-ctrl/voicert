// Cross-language proof: the C# client the Unity package ships talks to the
// real Python bridge over TCP. Starts `python -m voicert.game.bridge` on a
// free port, runs a full NPC turn, interrupts it, and checks every frame
// type the engine component relies on.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using VoiceRT.Core;
using Xunit;

namespace VoiceRT.Tests
{
    public class BridgeIntegrationTests : IDisposable
    {
        private readonly Process _bridge;
        private readonly int _port;
        private readonly bool _available;

        public BridgeIntegrationTests()
        {
            var root = FindRepoRoot();
            var python = root == null ? null : FindPython(root);
            if (python == null) { _available = false; return; }

            _port = FreePort();
            _bridge = new Process
            {
                StartInfo = new ProcessStartInfo
                {
                    FileName = python,
                    Arguments = $"-m voicert.game.bridge 127.0.0.1 {_port}",
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                }
            };
            _bridge.StartInfo.Environment["PYTHONPATH"] = Path.Combine(root, "src");
            _bridge.StartInfo.Environment["PYTHONUNBUFFERED"] = "1";
            _bridge.Start();
            _bridge.BeginOutputReadLine();
            _bridge.BeginErrorReadLine();
            _available = WaitForPort(_port, TimeSpan.FromSeconds(15));
        }

        public void Dispose()
        {
            try { if (_bridge != null && !_bridge.HasExited) _bridge.Kill(true); } catch { }
            _bridge?.Dispose();
        }

        [Fact]
        public void Full_turn_then_interrupt_over_real_tcp()
        {
            Assert.SkipWhen(!_available, "python bridge not available (no repo root / python / port)");

            var ready = new ManualResetEventSlim();
            var turnEnd = new ManualResetEventSlim();
            var flushed = new ManualResetEventSlim();
            var errors = new List<string>();
            long audioBytes = 0;
            int audioChunks = 0;
            var subtitle = "";
            int firstAudioOrder = -1, firstTextOrder = -1, order = 0;
            string metricsJson = null;

            using var client = new VoiceRTClient();
            client.Ready += () => ready.Set();
            client.AudioReceived += pcm =>
            {
                if (firstAudioOrder < 0) firstAudioOrder = Interlocked.Increment(ref order);
                Interlocked.Add(ref audioBytes, pcm.Length);
                Interlocked.Increment(ref audioChunks);
            };
            client.TextReceived += (t, turn) =>
            {
                if (firstTextOrder < 0) firstTextOrder = Interlocked.Increment(ref order);
                subtitle += t;
            };
            client.TurnEnded += (turn, m) => { metricsJson = m; turnEnd.Set(); };
            client.Flushed += () => flushed.Set();
            client.ErrorReceived += e => errors.Add(e);

            client.Connect("127.0.0.1", _port, new HelloOptions
            {
                NpcId = "yorick", Character = "Yorick the Merchant", LoreScope = "Velenhart harbor",
            });

            Assert.True(ready.Wait(5000), "READY not received");
            Assert.Equal(16000, client.SampleRate);

            client.SendText("What is for sale today?");
            Assert.True(turnEnd.Wait(10000), "TURN_END not received");

            Assert.True(audioChunks > 0, "no AUDIO_OUT frames");
            Assert.True(audioBytes % 2 == 0, "PCM16 payload must be whole samples");
            Assert.False(string.IsNullOrWhiteSpace(subtitle), "no subtitle text");
            Assert.True(firstAudioOrder < firstTextOrder, "text must follow its own audio");
            Assert.Contains("tts_first_audio", metricsJson);
            Assert.Contains("\"profile\": \"npc\"", metricsJson.Replace("\":\"", "\": \""));

            // Barge-in: start a long answer, cut it after the first audio chunk.
            var gotAudio = new ManualResetEventSlim();
            int before = audioChunks;
            client.AudioReceived += _ => { if (audioChunks > before) gotAudio.Set(); };
            client.SendText("Tell me a very long story about the harbor");
            Assert.True(gotAudio.Wait(5000), "second turn produced no audio");
            client.SendInterrupt();
            Assert.True(flushed.Wait(5000), "FLUSH not received after INTERRUPT");

            // After FLUSH, audio must stop arriving.
            Thread.Sleep(400);
            int atFlush = audioChunks;
            Thread.Sleep(400);
            Assert.Equal(atFlush, audioChunks);

            Assert.Empty(errors);
        }

        [Fact]
        public void Tool_calls_reach_the_engine_side()
        {
            Assert.SkipWhen(!_available, "python bridge not available");

            var tool = new ManualResetEventSlim();
            string toolName = null, toolArgs = null;
            using var client = new VoiceRTClient();
            var ready = new ManualResetEventSlim();
            client.Ready += () => ready.Set();
            client.ToolCalled += (n, a, id) => { toolName = n; toolArgs = a; tool.Set(); };
            client.Connect("127.0.0.1", _port, new HelloOptions { NpcId = "mira" });
            Assert.True(ready.Wait(5000));
            client.SendText("Use a tool and check the world state");
            Assert.True(tool.Wait(10000), "TOOL frame not received");
            Assert.Contains(toolName, new[] { "emit_game_event", "query_world_state", "play_animation" });
            Assert.StartsWith("{", toolArgs);
        }

        // -- helpers -------------------------------------------------------

        private static string FindRepoRoot()
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir != null)
            {
                if (File.Exists(Path.Combine(dir.FullName, "pyproject.toml")) &&
                    Directory.Exists(Path.Combine(dir.FullName, "src", "voicert")))
                    return dir.FullName;
                dir = dir.Parent;
            }
            return null;
        }

        private static string FindPython(string root)
        {
            var env = Environment.GetEnvironmentVariable("VOICERT_PYTHON");
            if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
            var venvWin = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (File.Exists(venvWin)) return venvWin;
            var venvNix = Path.Combine(root, ".venv", "bin", "python");
            if (File.Exists(venvNix)) return venvNix;
            foreach (var name in new[] { "python", "python3" })
            {
                try
                {
                    using var p = Process.Start(new ProcessStartInfo(name, "--version")
                        { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false });
                    p.WaitForExit(3000);
                    if (p.ExitCode == 0) return name;
                }
                catch { }
            }
            return null;
        }

        private static int FreePort()
        {
            var l = new TcpListener(IPAddress.Loopback, 0);
            l.Start();
            int port = ((IPEndPoint)l.LocalEndpoint).Port;
            l.Stop();
            return port;
        }

        private static bool WaitForPort(int port, TimeSpan timeout)
        {
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                try
                {
                    using var c = new TcpClient();
                    c.Connect(IPAddress.Loopback, port);
                    return true;
                }
                catch { Thread.Sleep(150); }
            }
            return false;
        }
    }
}
