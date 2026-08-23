// VoiceRT NPC — drop onto any GameObject with an AudioSource and it talks.
//
// What this component does:
//   * opens one TCP connection to the VoiceRT bridge for this NPC
//   * plays the streamed voice through the AudioSource, so Unity's own
//     spatializer, mixer groups, reverb zones and occlusion apply untouched
//   * raises UnityEvents for subtitles, tool calls (animations, gestures),
//     turn end, flush and errors — all on the main thread
//   * honours FLUSH instantly: barge-in empties the ring buffer before the
//     next audio callback
//
// Audio path: AudioClip.Create(..., stream: true, PCMReaderCallback) at the
// engine output rate. The reader callback pulls from a PcmRingBuffer that
// the network thread fills, resampling 16 kHz -> output rate on the fly.
// Using a streaming clip (rather than OnAudioFilterRead on a silent source)
// keeps the voice a normal AudioSource voice: 3D, priority, doppler, mixer.

using System;
using System.Collections.Concurrent;
using UnityEngine;
using UnityEngine.Events;
using VoiceRT.Core;

namespace VoiceRT
{
    [Serializable] public class StringEvent : UnityEvent<string> { }
    [Serializable] public class ToolEvent : UnityEvent<string, string> { }   // toolName, argsJson
    [Serializable] public class TurnEndEvent : UnityEvent<int, string> { }   // turnId, metricsJson

    [AddComponentMenu("VoiceRT/VoiceRT NPC")]
    [RequireComponent(typeof(AudioSource))]
    public sealed class VoiceRTNpc : MonoBehaviour
    {
        [Header("Bridge")]
        public string host = "127.0.0.1";
        public int port = 8765;
        public bool connectOnEnable = true;

        [Header("Character")]
        [Tooltip("Stable id for this NPC; one connection per id.")]
        public string npcId = "yorick";
        public string character = "Yorick, a merchant of the Harbor Quarter";
        [TextArea(2, 5)] public string loreScope = "the city of Velenhart, its guilds, goods, and rumors";
        public string voice = "default";

        [Header("Audio")]
        [Tooltip("Seconds of voice buffered between network and audio thread.")]
        [Range(0.25f, 5f)] public float bufferSeconds = 2f;

        [Header("Events (main thread)")]
        public StringEvent onSubtitle;      // partial words as they are voiced
        public ToolEvent onTool;            // play_animation, emit_game_event, ...
        public TurnEndEvent onTurnEnd;
        public UnityEvent onFlush;          // barge-in happened
        public StringEvent onError;
        public UnityEvent onReady;
        public UnityEvent onDisconnected;

        public bool IsConnected => _client != null && _client.IsConnected;
        public string CurrentSubtitle { get; private set; } = "";

        private VoiceRTClient _client;
        private PcmRingBuffer _ring;
        private AudioSource _source;
        private AudioClip _clip;
        private int _outputRate;
        private int _bridgeRate = 16000;
        private readonly ConcurrentQueue<Action> _mainThread = new ConcurrentQueue<Action>();
        private int _currentTurn = -1;

        // -- lifecycle -----------------------------------------------------

        private void Awake()
        {
            _source = GetComponent<AudioSource>();
            _outputRate = AudioSettings.outputSampleRate;
            _ring = new PcmRingBuffer(Mathf.CeilToInt(_bridgeRate * bufferSeconds));
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
            _client = new VoiceRTClient();
            _client.Ready += () => Post(() => { _bridgeRate = _client.SampleRate; StartPlayback(); onReady?.Invoke(); });
            _client.AudioReceived += pcm => _ring.WritePcm16(pcm);   // network thread, thread-safe
            _client.Flushed += () => { _ring.Clear(); Post(() => onFlush?.Invoke()); };
            _client.TextReceived += (text, turn) => Post(() =>
            {
                if (turn != _currentTurn) { _currentTurn = turn; CurrentSubtitle = ""; }
                CurrentSubtitle += text;
                onSubtitle?.Invoke(CurrentSubtitle);
            });
            _client.TurnEnded += (turn, metrics) => Post(() => onTurnEnd?.Invoke(turn, metrics));
            _client.ToolCalled += (name, args, id) => Post(() => onTool?.Invoke(name, args));
            _client.ErrorReceived += msg => Post(() => { Debug.LogWarning($"[VoiceRT:{npcId}] {msg}"); onError?.Invoke(msg); });
            _client.Disconnected += ex => Post(() =>
            {
                if (ex != null) Debug.LogWarning($"[VoiceRT:{npcId}] disconnected: {ex.Message}");
                onDisconnected?.Invoke();
            });

            try
            {
                _client.Connect(host, port, new HelloOptions
                {
                    NpcId = npcId, Character = character, LoreScope = loreScope, Voice = voice,
                });
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[VoiceRT:{npcId}] connect failed: {ex.Message}");
                onError?.Invoke(ex.Message);
                _client.Dispose();
                _client = null;
            }
        }

        public void Disconnect()
        {
            if (_source != null && _source.isPlaying) _source.Stop();
            _client?.Dispose();
            _client = null;
            _ring?.Clear();
        }

        private void Update()
        {
            while (_mainThread.TryDequeue(out var a)) a();
        }

        private void Post(Action a) => _mainThread.Enqueue(a);

        // -- audio ---------------------------------------------------------

        private void StartPlayback()
        {
            if (_source.isPlaying) return;
            // A streaming clip loops over a small window and keeps asking the
            // callback for more samples, which is exactly a live voice needs.
            _clip = AudioClip.Create($"VoiceRT-{npcId}", _outputRate, 1, _outputRate, true, OnPcmRead);
            _source.clip = _clip;
            _source.loop = true;
            _source.Play();
        }

        private void OnPcmRead(float[] data)
        {
            // Audio thread. Mono clip, so channels == 1 here; Unity spatializes after.
            _ring.ReadResampled(data, 1, _bridgeRate, _outputRate);
        }

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
            _ring.Clear();           // do not wait for the round trip
            if (IsConnected) _client.SendInterrupt();
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

        public int BufferedMilliseconds => _ring == null ? 0 : (int)(1000L * _ring.Available / _bridgeRate);
    }
}
