#include "Modules/ModuleManager.h"

class FVoiceRTModule : public IModuleInterface
{
public:
    virtual void StartupModule() override {}
    virtual void ShutdownModule() override {}
};

IMPLEMENT_MODULE(FVoiceRTModule, VoiceRT)
