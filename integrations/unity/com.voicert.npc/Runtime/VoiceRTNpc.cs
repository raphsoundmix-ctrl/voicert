// VoiceRT NPC — drop it on a character and it talks.
//
// What this component does:
//   * opens one TCP connection to the VoiceRT bridge for this NPC
//   * hands the streamed voice to an IVoiceRTAudioSink — a plain AudioSource by
//     default, or the FMOD programmer-sound sink when one is on the same object
//   * raises UnityEvents for subtitles, tool calls (animations, gestures),
//     turn end, flush and errors — all on the main thread
//   * honours FLUSH instantly: barge-in empties the buffer before the next
//     audio callback, without waiting for a round trip
//
// Where the audio goes is deliberately not this class's business. The sink
// interface carries the thread contract: Write and Flush run on the network
// reader thread (no marshalling — that is the point), everything else is main
// thread.

using System;
using System.Collections.Concurrent;
using UnityEngine;
using UnityEngine.Events;
using VoiceRT.Core;   // VoiceRTClient, HelloOptions

namespace VoiceRT
{
    [Serializable] public class StringEvent : UnityEvent<string> { }
    [Serializable] public class ToolEvent : UnityEvent<string, string> { }   // toolName, argsJson
    [Serializable] public class TurnEndEvent : UnityEvent<int, string> { }   // turnId, metricsJson
    [Serializable] public class TranscriptEvent : UnityEvent<string, bool> { }  // heard text, final?

    [AddComponentMenu("VoiceRT/VoiceRT NPC")]
    public sealed class VoiceRTNpc : MonoBehaviour
    {
        [Header("Bridge")]
        public string host = "127.0.0.1";
        [Tooltip("The local server listens on 8767; 8765 is the library default and is often taken.")]
        public int port = 8767;
        public bool connectOnEnable = true;

        [Header("Character")]
        [Tooltip("Stable id for this NPC; one connection per id.")]
        public string npcId = "yorick";
        public string character = "Yorick, a merchant of the Harbor Quarter";
        [TextArea(2, 5)] public string loreScope = "the city of Velenhart, its guilds, goods, and rumors";
        [Tooltip("A role the server knows (keeper, smith, healer...), a Kokoro voice name, or a speaker id.")]
        public string voice = "default";

        // Constructed here, not left to serialization: a component added with
        // AddComponent at runtime gets null UnityEvents, and the first
        // AddListener from game code would throw.
        [Header("Events (main thread)")]
        public StringEvent onSubtitle = new StringEvent();   // the line so far, as it is voiced
        public ToolEvent onTool = new ToolEvent();           // play_animation, emit_game_event, ...
        public TurnEndEvent onTurnEnd = new TurnEndEvent();
        public UnityEvent onFlush = new UnityEvent();        // barge-in happened
        public StringEvent onError = new StringEvent();
        public UnityEvent onReady = new UnityEvent();
        public UnityEvent onDisconnected = new UnityEvent();
        public StringEvent onState = new StringEvent();          // idle|listening|processing|...
        public TranscriptEvent onTranscript = new TranscriptEvent();  // what the player was heard to say

        public bool IsConnected => _client != null && _client.IsConnected;
        public string CurrentSubtitle { get; private set; } = "";
        /// <summary>What this conversation is doing, as the server sees it.</summary>
        public string State { get; private set; } = "idle";
        /// <summary>True while the NPC is audible: the server is still generating the
        /// reply, or the sink still holds audio it has not played. The second case is
        /// the long one — a GPU voice renders a reply many times faster than it is
        /// spoken. The microphone uses this: an open mic next to a speaker hears the
        /// NPC and barges in on it, and it must not do that in the middle of a line.</summary>
        public bool IsSpeaking => State == "speaking" || BufferedMilliseconds > 0;
        /// <summary>The voice's sample rate as announced by READY (24 kHz for Kokoro).</summary>
        public int BridgeSampleRate => _bridgeRate;

        private VoiceRTClient _client;
        private IVoiceRTAudioSink _sink;
        private int _bridgeRate = 16000;
        private readonly ConcurrentQueue<Action> _mainThread = new ConcurrentQueue<Action>();
        private int _currentTurn = -1;

        // -- lifecycle -----------------------------------------------------

        private void Awake()
        {
            // GetComponent resolves interfaces. An NPC with no sink of its own gets the
            // AudioSource path, so the component behaves exactly as it did before the seam.
            _sink = GetComponent<IVoiceRTAudioSink>();
            if (_sink == null) _sink = gameObject.AddComponent<VoiceRTAudioSourceSink>();
        }

        private void OnEnable()
        {
            if (connectOnEnable) Connect();
        }

        private void OnDisable()
        {
            Disconnect();
        }

        public void Connect()
        {
            if (IsConnected) return;
            // A client can be failed-but-not-null: VoiceRTClient.Fail() stops the
            // reader without closing the socket, so overwriting it here would
            // orphan a live connection and the server would hold that slot.
            Disconnect();
            var client = new VoiceRTClient();
            _client = client;
            // Every handler captures the client it belongs to: a queued action from
            // the previous session must not run against the current one (or against
            // a disposed one) after the player walks away and comes back.
            client.Ready += () => Post(() =>
            {
                if (_client != client) return;
                _bridgeRate = client.SampleRate;
                _sink.Configure(_bridgeRate);   // sizes the buffer AND fixes FMOD's defaultfrequency
                _sink.Begin();
                onReady?.Invoke();
            });
            client.AudioReceived += pcm => { if (_client == client) _sink.Write(pcm); };  // reader thread, by design
            client.Flushed += () =>
            {
                if (_client != client) return;
                _sink.Flush();                                           // reader thread: ordered cut point
                Post(() => onFlush?.Invoke());
            };
            client.TextReceived += (text, turn) => Post(() =>
            {
                if (_client != client) return;
                if (turn != _currentTurn) { _currentTurn = turn; CurrentSubtitle = ""; }
                CurrentSubtitle += text;
                onSubtitle?.Invoke(CurrentSubtitle);
            });
            client.TurnEnded += (turn, metrics) => Post(() => { if (_client == client) onTurnEnd?.Invoke(turn, metrics); });
            client.StateChanged += state => Post(() =>
            {
                if (_client != client) return;
                State = state;
                onState?.Invoke(state);
            });
            client.Transcript += (text, final) => Post(() =>
            {
                if (_client == client) onTranscript?.Invoke(text, final);
            });
            client.ToolCalled += (name, args, id) => Post(() => { if (_client == client) onTool?.Invoke(name, args); });
            client.ErrorReceived += msg => Post(() =>
            {
                if (_client != client) return;
                Debug.LogWarning($"[VoiceRT:{npcId}] {msg}");
                onError?.Invoke(msg);
            });
            client.Disconnected += ex => Post(() =>
            {
                if (_client != client) return;
                if (ex != null) Debug.LogWarning($"[VoiceRT:{npcId}] disconnected: {ex.Message}");
                onDisconnected?.Invoke();
            });

            try
            {
                client.Connect(host, port, new HelloOptions
                {
                    NpcId = npcId, Character = character, LoreScope = loreScope, Voice = voice,
                });
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[VoiceRT:{npcId}] connect failed: {ex.Message}");
                onError?.Invoke(ex.Message);
                client.Dispose();
                _client = null;
            }
        }

        public void Disconnect()
        {
            _sink?.End();
            _client?.Dispose();
            _client = null;
            // Drop this session's leftovers: a queued callback would run against the
            // next one, and a stale subtitle would be prepended to the next reply.
            while (_mainThread.TryDequeue(out _)) { }
            _currentTurn = -1;
            CurrentSubtitle = "";
            State = "idle";
        }

        private void Update()
        {
            while (_mainThread.TryDequeue(out var a)) a();
        }

        private void Post(Action a) => _mainThread.Enqueue(a);

        // -- gameplay API (call from anywhere on the main thread) ------------

        /// <summary>What the player said (typed, or from your own STT).</summary>
        public void Say(string playerText)
        {
            if (!IsConnected) { Debug.LogWarning($"[VoiceRT:{npcId}] not connected"); return; }
            _client.SendText(playerText);
        }

        /// <summary>Push an in-game event the NPC should react to.</summary>
        public void RaiseEvent(string eventName, string payloadJson = "{}")
        {
            if (IsConnected) _client.SendEvent(eventName, payloadJson);
        }

        /// <summary>Player started talking over the NPC: cut it off now.</summary>
        public void Interrupt()
        {
            // Only ever suppress when a FLUSH can come back to lift it. Barging in
            // on a disconnected NPC would leave the ring dropping every later
            // reply, silently, for the rest of the session.
            if (!IsConnected) return;
            _sink?.BargeIn();        // drops the queue AND suppresses audio already in flight
            _client.SendInterrupt();
        }

        /// <summary>Report the dialogue LOD tier (see VoiceRTLod).</summary>
        public void SetLod(string tier, float distanceMeters, float priority)
        {
            if (IsConnected) _client.SendLod(tier, distanceMeters, priority);
        }

        /// <summary>Stream microphone PCM16 mono 16 kHz for server-side VAD / barge-in.</summary>
        public void SendMicAudio(byte[] pcm16Mono16k)
        {
            if (IsConnected) _client.SendAudio(pcm16Mono16k);
        }

        /// <summary>The player stopped talking on purpose (push-to-talk released).
        /// Ends the utterance now rather than after the server's silence timer,
        /// which is worth the whole hangover on every turn.</summary>
        public void EndUtterance()
        {
            if (IsConnected) _client.SendEndpoint();
        }

        public int BufferedMilliseconds => _sink?.BufferedMilliseconds ?? 0;
    }
}
