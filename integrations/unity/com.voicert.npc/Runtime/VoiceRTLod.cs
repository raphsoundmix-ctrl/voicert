// Dialogue LOD for Unity — a port of voicert/game/lod.py.
//
// Only NPCs in the LIVE tier hold a bridge connection; everyone else is
// disconnected and plays barks or the crowd bed from your own audio setup.
// The thresholds have hysteresis: the exit distance is wider than the
// entry distance, so a player walking along a boundary does not connect
// and disconnect the NPC every frame.

using UnityEngine;

namespace VoiceRT
{
    public enum DialogueTier { Off = 0, Crowd = 1, Bark = 2, Live = 3 }

    [AddComponentMenu("VoiceRT/VoiceRT Dialogue LOD")]
    [RequireComponent(typeof(VoiceRTNpc))]
    public sealed class VoiceRTLod : MonoBehaviour
    {
        [Tooltip("Listener to measure distance from. Defaults to the main camera.")]
        public Transform listener;

        [Header("Distances (metres) — exit must exceed enter")]
        public float liveEnter = 6f, liveExit = 9f;
        public float barkEnter = 25f, barkExit = 32f;
        public float crowdEnter = 60f, crowdExit = 75f;

        [Header("Priority (quest NPCs outrank ambient ones)")]
        [Range(0f, 1f)] public float priority = 0.1f;

        [Tooltip("Once the player addresses this NPC, it stays LIVE until the conversation ends.")]
        public bool inConversation;

        [Tooltip("Evaluate every N frames; distance does not change much in 16 ms.")]
        [Range(1, 30)] public int evaluateEveryFrames = 10;

        public DialogueTier Tier { get; private set; } = DialogueTier.Off;
        public float LastDistance { get; private set; }

        private VoiceRTNpc _npc;
        private int _frame;

        private void Awake()
        {
            _npc = GetComponent<VoiceRTNpc>();
            _npc.connectOnEnable = false;   // LOD decides when to connect
            if (listener == null && Camera.main != null) listener = Camera.main.transform;
        }

        private void OnValidate()
        {
            liveExit = Mathf.Max(liveExit, liveEnter + 0.1f);
            barkEnter = Mathf.Max(barkEnter, liveExit + 0.1f);
            barkExit = Mathf.Max(barkExit, barkEnter + 0.1f);
            crowdEnter = Mathf.Max(crowdEnter, barkExit + 0.1f);
            crowdExit = Mathf.Max(crowdExit, crowdEnter + 0.1f);
        }

        private void Update()
        {
            if (listener == null) return;
            if (++_frame % evaluateEveryFrames != 0) return;
            LastDistance = Vector3.Distance(listener.position, transform.position);
            Apply(Evaluate(LastDistance));
        }

        public DialogueTier Evaluate(float d)
        {
            if (inConversation) return DialogueTier.Live;
            var cur = Tier;
            if (d <= Threshold(cur, DialogueTier.Live, liveEnter, liveExit)) return DialogueTier.Live;
            if (d <= Threshold(cur, DialogueTier.Bark, barkEnter, barkExit)) return DialogueTier.Bark;
            if (d <= Threshold(cur, DialogueTier.Crowd, crowdEnter, crowdExit)) return DialogueTier.Crowd;
            return DialogueTier.Off;
        }

        // Already at or above this tier -> the wider exit keeps us here;
        // climbing into it -> the stricter enter distance must be met.
        private static float Threshold(DialogueTier current, DialogueTier candidate, float enter, float exit)
            => current >= candidate ? exit : enter;

        private void Apply(DialogueTier next)
        {
            if (next == Tier) return;
            var prev = Tier;
            Tier = next;
            if (next == DialogueTier.Live && !_npc.IsConnected) _npc.Connect();
            if (prev == DialogueTier.Live && next != DialogueTier.Live)
            {
                _npc.SetLod(next.ToString().ToUpperInvariant(), LastDistance, priority);
                _npc.Disconnect();
            }
            else if (_npc.IsConnected)
            {
                _npc.SetLod(next.ToString().ToUpperInvariant(), LastDistance, priority);
            }
        }
    }
}
