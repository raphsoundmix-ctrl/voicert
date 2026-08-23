// VoiceRT NPC — drop onto any Actor and it talks.
//
// What this component does:
//   * opens one TCP connection to the VoiceRT bridge for this NPC
//   * plays the streamed voice through a USoundWaveProcedural on an
//     attached UAudioComponent, so Unreal's attenuation, occlusion,
//     submixes and reverb apply untouched
//   * broadcasts Blueprint delegates for subtitles, tool calls (animations,
//     gestures), turn end, flush and errors — all on the game thread
//   * honours FLUSH instantly: barge-in resets the procedural wave before
//     the next audio render

#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "VoiceRTNpcComponent.generated.h"

class UAudioComponent;
class USoundWaveProcedural;
class USoundAttenuation;
class FVoiceRTClient;

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FVoiceRTSubtitle, const FString&, Text);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(FVoiceRTTool, const FString&, ToolName, const FString&, ArgumentsJson);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(FVoiceRTTurnEnd, int32, TurnId, const FString&, MetricsJson);
DECLARE_DYNAMIC_MULTICAST_DELEGATE(FVoiceRTSignal);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FVoiceRTError, const FString&, Message);

UCLASS(ClassGroup = (VoiceRT), meta = (BlueprintSpawnableComponent), DisplayName = "VoiceRT NPC")
class VOICERT_API UVoiceRTNpcComponent : public UActorComponent
{
    GENERATED_BODY()

public:
    UVoiceRTNpcComponent();

    // -- bridge -----------------------------------------------------------
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Bridge")
    FString Host = TEXT("127.0.0.1");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Bridge")
    int32 Port = 8765;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Bridge")
    bool bConnectOnBeginPlay = true;

    // -- character --------------------------------------------------------
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Character")
    FString NpcId = TEXT("yorick");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Character")
    FString Character = TEXT("Yorick, a merchant of the Harbor Quarter");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Character", meta = (MultiLine = true))
    FString LoreScope = TEXT("the city of Velenhart, its guilds, goods, and rumors");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Character")
    FString Voice = TEXT("default");

    // -- audio ------------------------------------------------------------
    /** Attenuation asset applied to the voice; leave null for 2D. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Audio")
    USoundAttenuation* Attenuation = nullptr;

    /** Max audio queued ahead of playback before we start dropping (ms). */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VoiceRT|Audio", meta = (ClampMin = "250", ClampMax = "5000"))
    int32 MaxBufferedMs = 2000;

    // -- events (game thread) ---------------------------------------------
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTSubtitle OnSubtitle;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTTool OnTool;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTTurnEnd OnTurnEnd;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTSignal OnFlush;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTSignal OnReady;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTSignal OnDisconnected;
    UPROPERTY(BlueprintAssignable, Category = "VoiceRT|Events") FVoiceRTError OnError;

    // -- gameplay API -----------------------------------------------------
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void Connect();
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void Disconnect();
    UFUNCTION(BlueprintPure,     Category = "VoiceRT") bool IsConnected() const;

    /** What the player said (typed, or from your own STT). */
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void Say(const FString& PlayerText);

    /** Push an in-game event the NPC should react to. */
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void RaiseEvent(const FString& EventName, const FString& PayloadJson = TEXT("{}"));

    /** Player started talking over the NPC: cut it off now. */
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void Interrupt();

    /** Report the dialogue LOD tier: LIVE / BARK / CROWD / OFF. */
    UFUNCTION(BlueprintCallable, Category = "VoiceRT") void SetLod(const FString& Tier, float DistanceMeters, float Priority);

    /** Current subtitle for the turn in progress. */
    UFUNCTION(BlueprintPure, Category = "VoiceRT") FString GetCurrentSubtitle() const { return CurrentSubtitle; }

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;
    virtual void TickComponent(float DeltaTime, ELevelTick TickType, FActorComponentTickFunction* ThisTickFunction) override;

private:
    void EnsureAudio();
    void DrainInbound();

    TUniquePtr<FVoiceRTClient> Client;

    UPROPERTY(Transient) TObjectPtr<UAudioComponent> AudioComponent = nullptr;
    UPROPERTY(Transient) TObjectPtr<USoundWaveProcedural> Wave = nullptr;

    FString CurrentSubtitle;
    int32 CurrentTurn = -1;
    int32 BridgeSampleRate = 16000;
    bool bWasConnected = false;
};
