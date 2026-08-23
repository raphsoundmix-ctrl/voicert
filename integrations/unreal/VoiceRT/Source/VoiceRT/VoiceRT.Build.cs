using UnrealBuildTool;

public class VoiceRT : ModuleRules
{
    public VoiceRT(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        CppStandard = CppStandardVersion.Cpp17;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine",
            "Sockets",
            "Networking",
        });

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "Sockets",
        });

        // VoiceRTProtocol.h is plain C++17 (std::vector/std::string/std::mutex),
        // deliberately independent of Unreal so it also builds in the
        // standalone test under integrations/unreal/tests/.
        PublicIncludePaths.Add(ModuleDirectory + "/Public");
    }
}
