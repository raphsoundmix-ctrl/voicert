// TCP client for the VoiceRT bridge inside Unreal: FSocket + one FRunnable
// reader thread. Decoded frames are pushed into a thread-safe queue that
// the owning component drains on the game thread in TickComponent, so all
// delegates fire on the game thread and audio is queued from there.

#pragma once

#include "CoreMinimal.h"
#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "Containers/Queue.h"
#include "VoiceRTProtocol.h"

class FSocket;

struct FVoiceRTInbound
{
    voicert::FrameType Type = voicert::FrameType::Error;
    TArray<uint8> Payload;

    FString Text() const
    {
        FUTF8ToTCHAR Conv(reinterpret_cast<const ANSICHAR*>(Payload.GetData()), Payload.Num());
        return FString(Conv.Length(), Conv.Get());
    }
};

class FVoiceRTClient : public FRunnable
{
public:
    FVoiceRTClient();
    virtual ~FVoiceRTClient() override;

    /** Connects and sends HELLO. Returns false (and logs) on failure. Game thread. */
    bool Connect(const FString& Host, int32 Port, const FString& NpcId,
                 const FString& Character, const FString& LoreScope, const FString& Voice);

    /** Closes the socket and joins the reader thread. Game thread. */
    void Close();

    bool IsConnected() const { return bConnected; }

    // -- outbound (any thread; serialized by a critical section) --------------
    void SendText(const FString& Text);
    void SendEvent(const FString& EventName, const FString& PayloadJson);
    void SendInterrupt();
    void SendLod(const FString& Tier, float DistanceMeters, float Priority);
    void SendAudio(const uint8* Pcm16Mono16k, int32 Bytes);

    /** Drained by the component on the game thread. MPSC-safe. */
    TQueue<FVoiceRTInbound, EQueueMode::Mpsc> Inbound;

    // FRunnable
    virtual bool Init() override { return true; }
    virtual uint32 Run() override;
    virtual void Stop() override { bRunning = false; }

private:
    void SendRaw(const std::vector<uint8_t>& Frame);

    FSocket* Socket = nullptr;
    FRunnableThread* Thread = nullptr;
    FCriticalSection SendLock;
    TAtomic<bool> bRunning{ false };
    TAtomic<bool> bConnected{ false };
};
