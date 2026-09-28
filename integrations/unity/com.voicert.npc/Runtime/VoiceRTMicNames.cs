// Telling a microphone from something that merely looks like one.
//
// Windows presents loopbacks, virtual mixer outputs and stereo-mix taps in the
// same list as the capsule on your desk, and the one the system calls "default"
// is regularly one of those. They open without complaint, they advance their read
// position, and they deliver digital silence forever — or, worse, they deliver the
// game's own output, which then goes to speech recognition and comes back as the
// NPC transcribing itself.
//
// Measured on the development machine: the default capture endpoint was
// "Microphone (Mixing Driver 1 for US-1x2)", a virtual loopback belonging to an
// audio interface's mixer driver. It recorded 72000 frames at a peak of exactly
// zero while the physical "Microphone (US-1x2)" sat next to it in the same list.
//
// So the name is evidence, and it is the only evidence available before opening a
// device. It is a heuristic and it is treated as one: it reorders candidates and
// drops outright only the loopbacks, which are never right. Everything else is
// still tried, and the real verdict comes from whether audio actually arrives.

using System;

namespace VoiceRT
{
    public static class VoiceRTMicNames
    {
        // FMOD names capture-side taps on an OUTPUT device by suffixing the output's
        // name, e.g. "Speakers (US-1x2) [loopback]". These are never a microphone.
        private static readonly string[] LoopbackMarkers =
        {
            "[loopback]", "stereo mix", "what u hear", "what you hear",
            "wave out mix", "loopback",
        };

        // Virtual endpoints that are real capture devices as far as the OS is
        // concerned, but are fed by software routing that is usually not running.
        private static readonly string[] VirtualMarkers =
        {
            "mixing driver", "virtual", "vb-audio", "cable output", "voicemeeter",
            "asio", "aggregate", "multi-output", "steam streaming", "nvidia broadcast",
            "obs-", "obs virtual",
        };

        /// <summary>A tap on an output device. Capturing this records the game, not
        /// the player, so it is excluded rather than deprioritised.</summary>
        public static bool IsLoopback(string name)
        {
            if (string.IsNullOrEmpty(name)) return false;
            string n = name.ToLowerInvariant();
            foreach (string marker in LoopbackMarkers)
                if (n.Contains(marker)) return true;
            return false;
        }

        /// <summary>A software endpoint that may or may not carry anything. Worth
        /// trying, but only after every device that looks like real hardware.</summary>
        public static bool LooksVirtual(string name)
        {
            if (string.IsNullOrEmpty(name)) return false;
            string n = name.ToLowerInvariant();
            foreach (string marker in VirtualMarkers)
                if (n.Contains(marker)) return true;
            return false;
        }

        /// <summary>Lower sorts earlier: real hardware, then virtual endpoints.
        /// Loopbacks get a rank too, for backends that cannot drop them, but a
        /// backend that can identify them should not offer them at all.</summary>
        public static int Rank(string name) =>
            IsLoopback(name) ? 2 : LooksVirtual(name) ? 1 : 0;

        /// <summary>Order a device list best-first, keeping the backend's own order
        /// within each rank — a stable sort, so a backend that already knows which
        /// of its physical devices is best does not have that undone here.</summary>
        public static string[] Prefer(string[] devices, bool dropLoopbacks = true)
        {
            if (devices == null) return Array.Empty<string>();
            var kept = new System.Collections.Generic.List<string>(devices.Length);
            foreach (string d in devices)
            {
                if (dropLoopbacks && IsLoopback(d)) continue;
                kept.Add(d);
            }
            // Insertion sort by rank: stable, and these lists are a handful of items.
            for (int i = 1; i < kept.Count; i++)
            {
                string item = kept[i];
                int rank = Rank(item);
                int j = i - 1;
                while (j >= 0 && Rank(kept[j]) > rank) { kept[j + 1] = kept[j]; j--; }
                kept[j + 1] = item;
            }
            return kept.ToArray();
        }
    }
}
