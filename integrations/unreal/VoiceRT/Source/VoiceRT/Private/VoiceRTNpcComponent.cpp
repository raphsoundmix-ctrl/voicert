#include "VoiceRTNpcComponent.h"
#include "VoiceRTClient.h"

#include "Components/AudioComponent.h"
#include "Sound/SoundWaveProcedural.h"
#include "Sound/SoundAttenuation.h"
#include "GameFramework/Actor.h"

DEFINE_LOG_CATEGORY_STATIC(LogVoiceRTNpc, Log, All);

UVoiceRTNpcComponent::UVoiceRTNpcComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.bStartWithTickEnabled = true;
}

void UVoiceRTNpcComponent::BeginPlay()
{
    Super::BeginPlay();
    if (bConnectOnBeginPlay) Connect();
}

void UVoiceRTNpcComponent::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    Disconnect();
    Super::EndPlay(EndPlayReason);
}

void UVoiceRTNpcComponent::EnsureAudio()
{
    if (Wave && AudioComponent) return;

    // A procedural wave is an ordinary sound to the mixer: attenuation,
    // occlusion, submix sends and reverb all apply. The mixer resamples
    // from the wave's sample rate to the device rate, so we can hand it
    // the bridge's 16 kHz mono int16 untouched.
    Wave = NewObject<USoundWaveProcedural>(this, TEXT("VoiceRTWave"));
    Wave->SetSampleRate(BridgeSampleRate);
    Wave->NumChannels = 1;
    Wave->Duration = INDEFINITELY_LOOPING_DURATION;
    Wave->SoundGroup = SOUNDGROUP_Voice;
    Wave->bLooping = false;
    Wave->bProcedural = true;

    AudioComponent = NewObject<UAudioComponent>(GetOwner(), TEXT("VoiceRTAudio"));
    AudioComponent->SetupAttachment(GetOwner()->GetRootComponent());
    AudioComponent->RegisterComponent();
    AudioComponent->bAutoActivate = false;
    AudioComponent->bAutoDestroy = false;
    AudioComponent->bAllowSpatialization = Attenuation != nullptr;
    if (Attenuation) AudioComponent->AttenuationSettings = Attenuation;
    AudioComponent->SetSound(Wave);
}

void UVoiceRTNpcComponent::Connect()
{
    if (IsConnected()) return;
    EnsureAudio();
    Client = MakeUnique<FVoiceRTClient>();
    if (!Client->Connect(Host, Port, NpcId, Character, LoreScope, Voice))
    {
        OnError.Broadcast(FString::Printf(TEXT("connect to %s:%d failed"), *Host, Port));
        Client.Reset();
        return;
    }
    bWasConnected = true;
}

void UVoiceRTNpcComponent::Disconnect()
{
    if (AudioComponent && AudioComponent->IsPlaying()) AudioComponent->Stop();
    if (Wave) Wave->ResetAudio();
    if (Client) { Client->Close(); Client.Reset(); }
    bWasConnected = false;
}

bool UVoiceRTNpcComponent::IsConnected() const
{
    return Client.IsValid() && Client->IsConnected();
}

void UVoiceRTNpcComponent::TickComponent(float DeltaTime, ELevelTick TickType, FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    if (!Client) return;
    DrainInbound();
    if (bWasConnected && !Client->IsConnected())
    {
        bWasConnected = false;
        OnDisconnected.Broadcast();
    }
}

void UVoiceRTNpcComponent::DrainInbound()
{
    FVoiceRTInbound In;
    while (Client && Client->Inbound.Dequeue(In))
    {
        switch (In.Type)
        {
        case voicert::FrameType::Ready:
        {
            const std::string J = std::string(reinterpret_cast<const char*>(In.Payload.GetData()), In.Payload.Num());
            BridgeSampleRate = voicert::MiniJson::getInt(J, "sample_rate", 16000);
            if (Wave) Wave->SetSampleRate(BridgeSampleRate);
            if (AudioComponent && !AudioComponent->IsPlaying()) AudioComponent->Play();
            OnReady.Broadcast();
            break;
        }
        case voicert::FrameType::AudioOut:
        {
            if (!Wave) break;
            // Keep the queue bounded: if the game stalled, drop the oldest
            // audio rather than letting latency grow without limit.
            const int32 MaxBytes = BridgeSampleRate * 2 * MaxBufferedMs / 1000;
            if (Wave->GetAvailableAudioByteCount() > MaxBytes)
            {
                Wave->ResetAudio();
            }
            Wave->QueueAudio(In.Payload.GetData(), In.Payload.Num());
            if (AudioComponent && !AudioComponent->IsPlaying()) AudioComponent->Play();
            break;
        }
        case voicert::FrameType::TextOut:
        {
            const std::string J = std::string(reinterpret_cast<const char*>(In.Payload.GetData()), In.Payload.Num());
            const int32 Turn = voicert::MiniJson::getInt(J, "turn_id");
            if (Turn != CurrentTurn) { CurrentTurn = Turn; CurrentSubtitle.Empty(); }
            CurrentSubtitle += UTF8_TO_TCHAR(voicert::MiniJson::getString(J, "text").c_str());
            OnSubtitle.Broadcast(CurrentSubtitle);
            break;
        }
        case voicert::FrameType::TurnEnd:
        {
            const std::string J = std::string(reinterpret_cast<const char*>(In.Payload.GetData()), In.Payload.Num());
            OnTurnEnd.Broadcast(voicert::MiniJson::getInt(J, "turn_id"),
                                UTF8_TO_TCHAR(voicert::MiniJson::getRaw(J, "metrics").c_str()));
            break;
        }
        case voicert::FrameType::Flush:
            if (Wave) Wave->ResetAudio();
            OnFlush.Broadcast();
            break;
        case voicert::FrameType::Tool:
        {
            const std::string J = std::string(reinterpret_cast<const char*>(In.Payload.GetData()), In.Payload.Num());
            OnTool.Broadcast(UTF8_TO_TCHAR(voicert::MiniJson::getString(J, "tool_name").c_str()),
                             UTF8_TO_TCHAR(voicert::MiniJson::getRaw(J, "arguments").c_str()));
            break;
        }
        case voicert::FrameType::Error:
        {
            const std::string J = std::string(reinterpret_cast<const char*>(In.Payload.GetData()), In.Payload.Num());
            const FString Msg = UTF8_TO_TCHAR(voicert::MiniJson::getString(J, "message").c_str());
            UE_LOG(LogVoiceRTNpc, Warning, TEXT("[%s] %s"), *NpcId, *Msg);
            OnError.Broadcast(Msg);
            break;
        }
        default:
            break;
        }
    }
}

void UVoiceRTNpcComponent::Say(const FString& PlayerText)
{
    if (!IsConnected()) { UE_LOG(LogVoiceRTNpc, Warning, TEXT("[%s] not connected"), *NpcId); return; }
    Client->SendText(PlayerText);
}

void UVoiceRTNpcComponent::RaiseEvent(const FString& EventName, const FString& PayloadJson)
{
    if (IsConnected()) Client->SendEvent(EventName, PayloadJson);
}

void UVoiceRTNpcComponent::Interrupt()
{
    if (Wave) Wave->ResetAudio();          // do not wait for the round trip
    if (IsConnected()) Client->SendInterrupt();
}

void UVoiceRTNpcComponent::SetLod(const FString& Tier, float DistanceMeters, float Priority)
{
    if (IsConnected()) Client->SendLod(Tier, DistanceMeters, Priority);
}
