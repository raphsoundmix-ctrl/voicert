// How VoiceRTVoiceInput reaches a backend it is not allowed to name.
//
// VoiceRT.Runtime has no reference to FMODUnity and must not gain one: the package
// has to compile in a project with no FMOD installed, which is what the
// VOICERT_FMOD define and the separate VoiceRT.Fmod assembly are for. So the FMOD
// capture backend cannot be constructed by name from here.
//
// It registers itself instead. VoiceRT.Fmod runs a RuntimeInitializeOnLoadMethod
// that drops a factory in here, and VoiceRTVoiceInput calls whatever it finds.
// No reflection, no string type lookup, no component wiring in the scene, and the
// whole thing compiles away to a null delegate when FMOD is absent.

using System;
using System.Collections.Generic;

namespace VoiceRT
{
    public static class VoiceRTMicBackends
    {
        private static readonly List<Entry> Registered = new List<Entry>();

        private readonly struct Entry
        {
            public readonly int Priority;
            public readonly string Name;
            public readonly Func<IVoiceRTMicSource> Create;
            public Entry(int priority, string name, Func<IVoiceRTMicSource> create)
            {
                Priority = priority; Name = name; Create = create;
            }
        }

        /// <summary>Offer a capture backend. Higher priority is tried first; the
        /// built-in Unity backend is 0, so anything that wants to outrank it asks
        /// for more. Registering the same name twice replaces the first — domain
        /// reloads in the editor would otherwise stack duplicates.</summary>
        public static void Register(string name, int priority, Func<IVoiceRTMicSource> create)
        {
            if (string.IsNullOrEmpty(name) || create == null) return;
            Registered.RemoveAll(e => e.Name == name);
            Registered.Add(new Entry(priority, name, create));
            Registered.Sort((a, b) => b.Priority.CompareTo(a.Priority));
        }

        /// <summary>Registered backend names, best first. For the inspector and the log.</summary>
        public static string[] Names()
        {
            var names = new string[Registered.Count];
            for (int i = 0; i < Registered.Count; i++) names[i] = Registered[i].Name;
            return names;
        }

        /// <summary>Build the backends to try, best first. Never returns null and
        /// never returns empty: the Unity backend is always the last resort, which
        /// is exactly the "keep Unity as the secondary path" rule.</summary>
        public static List<IVoiceRTMicSource> CreateAll(bool includeRegistered = true)
        {
            var sources = new List<IVoiceRTMicSource>();
            if (includeRegistered)
            {
                foreach (var entry in Registered)
                {
                    IVoiceRTMicSource source = null;
                    try { source = entry.Create(); }
                    catch (Exception) { source = null; }   // a broken backend must not take the mic down
                    if (source != null) sources.Add(source);
                }
            }
            sources.Add(new VoiceRTUnityMicSource());
            return sources;
        }
    }
}
