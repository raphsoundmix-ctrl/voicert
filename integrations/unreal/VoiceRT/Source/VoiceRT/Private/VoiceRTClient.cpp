#include "VoiceRTClient.h"

#include "Sockets.h"
#include "SocketSubsystem.h"
#include "IPAddress.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "HAL/PlatformProcess.h"

DEFINE_LOG_CATEGORY_STATIC(LogVoiceRT, Log, All);

namespace
{
    std::string ToUtf8(const FString& S)
    {
        FTCHARToUTF8 Conv(*S);
        return std::string(Conv.Get(), Conv.Length());
    }
}

FVoiceRTClient::FVoiceRTClient() = default;

FVoiceRTClient::~FVoiceRTClient()
{
    Close();
}

bool FVoiceRTClient::Connect(const FString& Host, int32 Port, const FString& NpcId,
                             const FString& Character, const FString& LoreScope, const FString& Voice)
{
    Close();

    ISocketSubsystem* Subsystem = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
    if (!Subsystem)
    {
        UE_LOG(LogVoiceRT, Warning, TEXT("no socket subsystem"));
        return false;
    }

    TSharedRef<FInternetAddr> Addr = Subsystem->CreateInternetAddr();
    bool bValidIp = false;
    Addr->SetIp(*Host, bValidIp);
    if (!bValidIp)
    {
        // allow a hostname: resolve via the subsystem
        FAddressInfoResult Info = Subsystem->GetAddressInfo(*Host, nullptr, EAddressInfoFlags::Default, NAME_None);
        if (Info.Results.Num() == 0)
        {
            UE_LOG(LogVoiceRT, Warning, TEXT("cannot resolve %s"), *Host);
            return false;
        }
        Addr = Info.Results[0].Address;
    }
    Addr->SetPort(Port);

    Socket = Subsystem->CreateSocket(NAME_Stream, TEXT("VoiceRT"), Addr->GetProtocolType());
    if (!Socket)
    {
        UE_LOG(LogVoiceRT, Warning, TEXT("CreateSocket failed"));
        return false;
    }
    Socket->SetNoDelay(true);
    if (!Socket->Connect(*Addr))
    {
        UE_LOG(LogVoiceRT, Warning, TEXT("connect to %s:%d failed"), *Host, Port);
        Subsystem->DestroySocket(Socket);
        Socket = nullptr;
        return false;
    }

    bRunning = true;
    bConnected = true;

    // HELLO
    const std::string Hello =
        "{\"proto\":1,\"npc_id\":" + voicert::MiniJson::str(ToUtf8(NpcId)) +
        ",\"character\":" + voicert::MiniJson::str(ToUtf8(Character)) +
        ",\"lore_scope\":" + voicert::MiniJson::str(ToUtf8(LoreScope)) +
        ",\"voice\":" + voicert::MiniJson::str(ToUtf8(Voice)) + "}";
    SendRaw(voicert::FrameCodec::encodeText(voicert::FrameType::Hello, Hello));

    Thread = FRunnableThread::Create(this, TEXT("VoiceRT-reader"), 0, TPri_Normal);
    return Thread != nullptr;
}

void FVoiceRTClient::Close()
{
    bRunning = false;
    bConnected = false;
    if (Socket)
    {
        Socket->Close();
    }
    if (Thread)
    {
        Thread->WaitForCompletion();
        delete Thread;
        Thread = nullptr;
    }
    if (Socket)
    {
        ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Socket);
        Socket = nullptr;
    }
}

uint32 FVoiceRTClient::Run()
{
    voicert::FrameParser Parser;
    TArray<uint8> Chunk;
    Chunk.SetNumUninitialized(32 * 1024);

    while (bRunning && Socket)
    {
        uint32 Pending = 0;
        if (!Socket->HasPendingData(Pending))
        {
            // block briefly for data rather than spinning
            if (!Socket->Wait(ESocketWaitConditions::WaitForRead, FTimespan::FromMilliseconds(50)))
            {
                if (Socket->GetConnectionState() == SCS_ConnectionError) break;
                continue;
            }
        }
        int32 Read = 0;
        if (!Socket->Recv(Chunk.GetData(), Chunk.Num(), Read) || Read <= 0)
        {
            break;
        }
        Parser.feed(Chunk.GetData(), static_cast<size_t>(Read));
        voicert::Frame F;
        try
        {
            while (Parser.tryPop(F))
            {
                FVoiceRTInbound In;
                In.Type = F.type;
                In.Payload.Append(F.payload.data(), static_cast<int32>(F.payload.size()));
                Inbound.Enqueue(MoveTemp(In));
            }
        }
        catch (const std::exception& Ex)
        {
            UE_LOG(LogVoiceRT, Warning, TEXT("protocol error: %hs"), Ex.what());
            break;
        }
    }
    bConnected = false;
    return 0;
}

void FVoiceRTClient::SendRaw(const std::vector<uint8_t>& Frame)
{
    if (!Socket || !bConnected) return;
    FScopeLock Lock(&SendLock);
    int32 Sent = 0;
    int32 Offset = 0;
    const int32 Total = static_cast<int32>(Frame.size());
    while (Offset < Total)
    {
        if (!Socket->Send(Frame.data() + Offset, Total - Offset, Sent) || Sent <= 0)
        {
            bConnected = false;
            return;
        }
        Offset += Sent;
    }
}

void FVoiceRTClient::SendText(const FString& Text)
{
    SendRaw(voicert::FrameCodec::encodeText(voicert::FrameType::TextIn, ToUtf8(Text)));
}

void FVoiceRTClient::SendEvent(const FString& EventName, const FString& PayloadJson)
{
    const std::string Payload = PayloadJson.IsEmpty() ? "{}" : ToUtf8(PayloadJson);
    const std::string Json = "{\"event\":" + voicert::MiniJson::str(ToUtf8(EventName)) + ",\"payload\":" + Payload + "}";
    SendRaw(voicert::FrameCodec::encodeText(voicert::FrameType::Event, Json));
}

void FVoiceRTClient::SendInterrupt()
{
    SendRaw(voicert::FrameCodec::encode(voicert::FrameType::Interrupt));
}

void FVoiceRTClient::SendLod(const FString& Tier, float DistanceMeters, float Priority)
{
    const FString Json = FString::Printf(TEXT("{\"tier\":\"%s\",\"distance_m\":%.2f,\"priority\":%.3f}"),
                                         *Tier, DistanceMeters, Priority);
    SendRaw(voicert::FrameCodec::encodeText(voicert::FrameType::Lod, ToUtf8(Json)));
}

void FVoiceRTClient::SendAudio(const uint8* Pcm16Mono16k, int32 Bytes)
{
    SendRaw(voicert::FrameCodec::encode(voicert::FrameType::AudioIn, Pcm16Mono16k, static_cast<size_t>(Bytes)));
}
