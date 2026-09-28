#if UNITY_EDITOR
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build;
using UnityEngine;

namespace VoiceRT.EditorTools
{
    /// <summary>
    /// FMOD for Unity ships as a .unitypackage into Assets/Plugins/FMOD with a plain
    /// asmdef named "FMODUnity" - it is NOT a UPM package, so asmdef versionDefines
    /// (which key off Packages/manifest.json) can never detect it. This does.
    /// </summary>
    [InitializeOnLoad]
    internal static class VoiceRTFmodDefine
    {
        private const string Symbol = "VOICERT_FMOD";

        static VoiceRTFmodDefine() { Sync(); }

        [MenuItem("VoiceRT/FMOD/Refresh Define")]
        private static void Sync()
        {
            bool present = FmodPresent();

            var named = NamedBuildTarget.FromBuildTargetGroup(
                BuildPipeline.GetBuildTargetGroup(EditorUserBuildSettings.activeBuildTarget));

            var defines = PlayerSettings.GetScriptingDefineSymbols(named)
                .Split(';')
                .Where(s => !string.IsNullOrWhiteSpace(s))
                .ToList();

            bool has = defines.Contains(Symbol);
            if (present == has) return;                 // guard: never loop on the recompile

            if (present) defines.Add(Symbol); else defines.Remove(Symbol);
            PlayerSettings.SetScriptingDefineSymbols(named, string.Join(";", defines));
            Debug.Log($"[VoiceRT] {(present ? "added" : "removed")} {Symbol} - FMOD sink will " +
                      $"{(present ? "compile" : "be excluded")} after this recompile.");
        }

        private static bool FmodPresent()
        {
            // Fast path: the canonical import location.
            if (File.Exists(Path.Combine(Application.dataPath, "Plugins/FMOD/FMODUnity.asmdef")))
                return true;

            // The user may have moved it (FMOD's own FileReorganizer does this).
            return AssetDatabase.FindAssets("FMODUnity t:AssemblyDefinitionAsset")
                .Select(AssetDatabase.GUIDToAssetPath)
                .Any(p => Path.GetFileName(p) == "FMODUnity.asmdef");
        }
    }
}
#endif
