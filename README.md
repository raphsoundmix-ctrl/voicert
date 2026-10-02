# VoiceRT — Talk to any NPC
<img width="2752" height="1536" alt="VoiceRT: a player talks to an NPC and hears it answer inside the game's mix" src="https://github.com/user-attachments/assets/f36324da-5850-4a02-802e-229865d464af" />

**Real-time, interruptible voice for game NPCs. Unity + FMOD first; speech recognition, the language model and the voice run on the player's GPU. All rights reserved.**

The player holds a key and talks. The NPC answers in character, in its own voice, through an FMOD event in the game's own mix. Speech recognition, the language model and the voice all run locally: no API key, no per-turn bill.

> **The source code is not in this repository.** VoiceRT's main repository is private for now and will be opened later. This repository holds this README and the product site
> ([voice-rt-agent.vercel.app](https://voice-rt-agent.vercel.app/)); the two documents under `docs/` are the on-device stack notes and the cost model. Paths, modules and commands named below refer to the main repository.
> To discuss an integration or ask for access, use the contact options on the site.

This README is written for the engineers who would integrate or review it: the process split, the wire protocol, the barge-in algorithm, the NPC contract and how a character is configured, what is measured, and what is not.

![Tests](https://img.shields.io/badge/pytest-229%20passed-34d399)
![C# tests](https://img.shields.io/badge/xUnit-21%20passed-34d399)
![mypy](https://img.shields.io/badge/mypy-strict%20%E2%9C%93-34d399)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-2021.3%2B%20package-000000?logo=unity&logoColor=white)
![FMOD](https://img.shields.io/badge/FMOD%20for%20Unity-2.03-ff6d00)
![AltaLab](https://img.shields.io/badge/AltaLab%20accelerator-Fall%202026%20cohort-0070f3)

---

## Contents

- [Status: what is verified, what is not](#status-what-is-verified-what-is-not)
- [Architecture](#architecture)
- [One turn, end to end](#one-turn-end-to-end)
- [Barge-in](#barge-in)
- [The NPC contract](#the-npc-contract)
- [The NPC agent: abilities and configuration](#the-npc-agent-abilities-and-configuration)
- [Wire protocol](#wire-protocol)
- [Unity package](#unity-package)
- [Reference setup](#reference-setup)
- [Measurements](#measurements)
- [Game layer: dialogue LOD and the voice pool](#game-layer-dialogue-lod-and-the-voice-pool)
- [Cost model](#cost-model)
- [Layout of the main repository](#layout-of-the-main-repository)
- [Tests](#tests)
- [Roadmap](#roadmap)
- [License](#license)

---

## Status: what is verified, what is not

| Component | State | Evidence |
|---|---|---|
| Python runtime: pipeline, barge-in, NPC contract, memory, engine bridge, economics ledger | shipped | 229 pytest, mypy strict on 34 files, no API keys or GPU needed for CI |
| Local GPU stack: Whisper `small.en` → `qwen3:8b` (Ollama) → Kokoro v1.0 fp32 (sherpa-onnx) | runs end to end | boot-time GPU gate; p50 **489 ms** end of speech → first audio (n=10, scripted speech input, editor idle), **850–1100 ms** with the game rendering on the same GPU |
| Unity package: FMOD programmer-instrument sink (M1) | shipped | compiled in Unity 6 (6000.6) with FMOD for Unity 2.03.14, heard through a real microphone; a Windows player build runs on the development machine (its config still holds that machine's paths, so it is not packaged for other machines yet) |
| Unity package: plain `AudioSource` sink | shipped | same build; 21 xUnit tests on the engine-agnostic C# core, including a full turn plus a barge-in against a live Python bridge |
| Unreal plugin (`USoundWaveProcedural` playback) | prototype | protocol verified in standalone C++; never compiled inside an Editor |
| Wwise | documented contract only | `voicert.game.sinks` describes the Audio Input wiring (`UAkAudioInputComponent`); there is no plugin code for it yet |
| Cloud adapters: Deepgram, Claude Haiku, OpenRouter, ElevenLabs, Cartesia | interface only | raise `NotImplementedError` until M3 |

Limits, stated up front so nobody has to find them:

- **English only.** Speech recognition is Whisper `small.en`. Every planned provider is multilingual, but nothing else is wired.
- **Windows + NVIDIA CUDA is the only measured platform.** The server refuses to start if any stage fell back to the CPU (`--allow-cpu` overrides it; that path was not tried). Phones and consoles are not benchmarked; Kokoro is not real time on low-power ARM (RTF 2.77–6.63 on a Raspberry Pi 4).
- **Latency is over budget under load.** The NPC profile's budget is 300 ms to first audio. Idle, the stack does ~0.5 s; with the game rendering on the same GPU, 0.85–1.1 s. One GPU has three tenants — Kokoro's first chunk goes from 123.6 ms to 347.2 ms while the 8B model decodes beside it — and closing that gap is the next engineering target.
- **No gestures or game events from the live model yet.** The tool set is closed and forwarded to the engine, but the live language-model stage does not issue tool calls (see [The NPC agent](#the-npc-agent-abilities-and-configuration)).
- **No standalone demo yet.** The Unity scene that starts the server by itself and shows barge-in on screen is milestone M2.

---

## Architecture

```mermaid
flowchart LR
    subgraph U["Unity process"]
        MIC["VoiceRT Voice Input<br/>FMOD record · push-to-talk"]
        LOD["Dialogue LOD<br/>LIVE · BARK · CROWD · OFF"]
        NPC["VoiceRT NPC<br/>(MonoBehaviour)"]
        FMOD["FMOD event<br/>programmer instrument"]
        MIX["Game mix<br/>3D · occlusion · ducking · buses"]
    end
    subgraph V["voicert process (Python)"]
        VAD["VAD + segmenter<br/>barge-in, pre-roll"] --> STT["STT<br/>Whisper small.en"] --> LLM["NPC agent · lore-locked<br/>qwen3:8b · tools: engine only"] --> TTS["TTS<br/>Kokoro → PCM16"]
        MEM["Memory<br/>one file per (NPC, player)"] --- LLM
        GATE["GPU gate<br/>won't start on a CPU fallback"]
    end
    MIC -- "AUDIO_IN · PCM16 16 kHz, while held" --> NPC
    LOD -- "connect only if LIVE" --> NPC
    NPC == "TCP · one socket per live NPC" ==> VAD
    TTS -- "AUDIO_OUT" --> FMOD --> MIX
    VAD -. "barge-in: cancel + FLUSH" .-> FMOD
    LLM -- "TEXT_OUT · TOOL → subtitles, gestures" --> NPC
```

Not every arrow is live today: the live language-model stage makes no tool calls, so the real character produces no `TOOL` frame, and the server does not use the voice pool yet (see the game-layer section).

**Two processes, on purpose.** Everything heavy — VAD, STT, the LLM, TTS, the voice pool — stays in the Python process on the GPU. The engine gets a thin client: one socket per live NPC, PCM queued into an ordinary engine sound, text and tool calls forwarded to your MonoBehaviour. The engine never runs a model, so no model ever has to fit inside the 16.6 ms frame at 60 fps, and a model failure is a dropped socket rather than a crashed game. The Unreal plugin is the same client in C++, playing through `USoundWaveProcedural`; a Wwise path (`UAkAudioInputComponent` in the FMOD slot) exists only as a documented contract so far.

**Frames, not callbacks.** Everything moving through the runtime is an immutable frame (`AudioFrame`, `TextFrame`, `FunctionCallFrame`, `InterruptionFrame`, `EndFrame`). Processors (`STTService`, `LLMService`, `TTSService`) are joined by asyncio queues and only ever see frames, so a provider is one ~50-line class and the core never changes. The core is stdlib-only asyncio; local models and cloud SDKs are optional extras.

---

## One turn, end to end

1. **Capture.** One `VoiceRT Voice Input` per player records through FMOD Core; Unity's `Microphone` is the fallback (on the reference machine `Microphone.devices` came back empty while FMOD, in the same process, listed eight endpoints). The device rate is resampled to 16 kHz with the fractional read position carried across chunks, and streamed as `AUDIO_IN` only while the push-to-talk key is held.
2. **Utterance.** On the server, `EnergyVAD` opens capture and drives barge-in; `UtteranceSegmenter` prepends 300 ms of pre-roll (a VAD needs energy before it fires — without pre-roll Whisper hears "ello"), drops anything under 250 ms and force-flushes past 20 s.
3. **Endpoint.** Releasing the key sends `ENDPOINT`, which closes the utterance immediately instead of paying the VAD's 450 ms hangover on every turn. A client that has sent `ENDPOINT` owns the turn boundary: a pause mid-sentence is not the end of one.
4. **Speech in.** Whisper `small.en` gets the whole utterance (chunk-by-chunk transcription produces confident nonsense). While the player is still talking, a partial transcript over the last 3 s is produced at most every 0.55 s and sent back as `STT` for a live caption; the answer is never built from a partial.
5. **Who is speaking.** `voicert.game.agents` assembles the system prompt from the world file (shared lore, each character's role, knowledge, voice and how they turn a question away) plus what this character remembers about this player. The restriction goes last, and it gives the model a line to say ("that word is gibberish to you") rather than only a prohibition.
6. **The reply.** `qwen3:8b` streams through Ollama. `SentenceBuffer` releases whole clauses, so TTS can place stress and breath instead of giving every word its own falling intonation. `llm_first_token` is stamped by the provider on the first token, so buffering never shows up as model latency.
7. **Voice.** Kokoro (fp32, sherpa-onnx CUDA) synthesizes each clause; the PCM goes out as `AUDIO_OUT` at the TTS rate (24 kHz for Kokoro, announced in `READY`) and the words as `TEXT_OUT` for subtitles. A tool call, if the model made one, would go out as `TOOL`; the live Ollama stage does not make tool calls yet.
8. **Into the mix.** In Unity the socket thread writes into a `PcmRingBuffer`; FMOD's mixer thread pulls it through a user-created sound (`FMOD_OPENUSER` + `pcmreadcallback`) handed to the event's programmer instrument on `CREATE_PROGRAMMER_SOUND`. Everything downstream — spatializer, buses, snapshots, ducking, reverb sends — is authored in FMOD Studio by the sound designer, exactly as for recorded dialogue.
9. **Close.** `TURN_END` carries the turn's metrics; the conversation is written to that (NPC, player) memory file.

---

## Barge-in

An interruption is not a flag that something checks later. Each frame is handled in its own child task, and a cut is `task.cancel()`, delivered at the next await point:

```
VAD: speech_start
  └─ profile gate (0 ms for the NPC: game feel beats politeness)
      └─ pipeline.interrupt()
           1. cancel everything in flight (LLM generation, TTS synthesis)
           2. drain the queues, so nothing produced before the cut plays after it
           3. on_interrupt() on every processor, to flush buffers
           4. InterruptionFrame → transport → FLUSH to the engine
      └─ state.interrupt_assistant(turn)   # history keeps only what was voiced
```

Three details carry most of the weight:

- **History keeps what was voiced.** The LLM may have written 400 characters while TTS voiced 90. Only those 90 enter history (`mark_spoken()` only moves forward, so a late progress report cannot stretch it). The NPC profile goes further and drops the interrupted reply from the model's context: cleaner lore, shorter prompt.
- **Cut what is playing, not what is running.** Kokoro synthesizes about ten times faster than the words are spoken, so a nine-second reply is fully generated in under a second — by the time the player interrupts, the pipeline is often idle and the engine still has seconds queued. The server flushes everything it has sent since the last cut; the client drops its ring buffer on `FLUSH`. **Known gap:** the client does not report how much it has played, so after a late cut like this the character's memory still holds the whole reply that was sent, not only the part the player heard.
- **The tail FMOD already decoded.** FMOD reads ahead `decodeBufferSamples` (1024 by default, 64 ms at 16 kHz) that a flush cannot reach. `hardCutOnFlush` restarts the event to discard it, at the cost of an event restart.

The race cases are tested, not assumed (`tests/test_interruption.py`): a cut mid-generation, a cut while TTS is streaming with no audio frame allowed through afterwards, two cuts fired at once (exactly one wins, no deadlock, the next turn still works), and the VAD gate telling a short "uh-huh" from a real interruption.

---

## The NPC contract

`ConfigFactory.build("npc")` returns a wired runtime. The profile is a contract, not a prompt:

| | `npc` |
|---|---|
| Transport | engine bridge: one TCP socket per live NPC, PCM16 mono both ways |
| Tools | `emit_game_event`, `query_world_state`, `play_animation` — nothing else |
| Barge-in gate | **0 ms** |
| Interrupted reply | **dropped** from the model's context |
| Turn boundary | owned by a push-to-talk client once it sends `ENDPOINT`; open-mic clients endpoint on silence |
| Latency budget | first LLM token **150 ms**, first audio **300 ms**, both measured from the end of the player's speech (deadlines, not added together; `over_budget` fires on the second) |
| Memory | one JSON file per (NPC, player), read before the reply, written after; there is no query that could return the guard's memory to the fruit vendor |

**The tool set is closed.** A prompt-injected "search the web" or "read that file" has nothing to resolve to: `ToolRegistry.get` raises `PermissionError`, and the tests call four out-of-engine tool names to prove it. The guardrail is the structure, not prompt discipline.

**A character is a derived profile.** `ConfigFactory.build` also takes a `ProfileConfig`, so each character keeps the contract and changes only what makes it that character:

```python
from dataclasses import replace
from voicert.config import PROFILES, ConfigFactory

guard = replace(PROFILES["npc"], name="guard",
                prompt_vars={"character": "Brann, a gate guard", "lore_scope": "the north gate"})
runtime = ConfigFactory.build(guard)   # same tools, gate, budgets, context policy
```

---

## The NPC agent: abilities and configuration

One process holds one speech model, one language model and one voice pool, shared by every character. What makes the guard a guard is three separate bodies of text, assembled into one system prompt per session:

| Text | Meaning | Source |
|---|---|---|
| **World lore** | what every character in the game knows | `facts` in the world file |
| **Role lore** | what this character knows, and nothing beyond it | the character's `knows` and `forbidden` |
| **Memory** | what this character learned from this player | a JSON file per (character, player), written after every exchange |

The prompt order is fixed: identity, what you know, what you remember about this player, what you do not know, the spoken-style rules, and last, how to answer anything outside the role. The restriction goes last on purpose:
with the rules in the middle, `qwen3:1.7b` explained quantum entanglement to the night guard. A test pins the order.

**What it can do today.** Listen (English, push-to-talk or voice activity); answer in character inside its lore, in one or two sentences; speak with one of 54 Kokoro voices; be interrupted; remember the player across a walk away and a server restart; keep one character's memory from another.
`qwen3:8b` held the character in 6 of 6 probes; `qwen3:1.7b` scored 3 of 6 with a plain prompt and 5 of 6 after the prompt work.

**A character is data, not code.** One JSON world file describes the shared lore and the whole cast; the server loads it once at start. The shipped demo cast (five characters of a harbour town) is `examples/worlds/harbour-town.json`.

| Field | Meaning |
|---|---|
| world `name`, `facts[]`, `npcs{}` | what the world is called, shared lore (one clause per entry), a map from `npc_id` to a character |
| `name`, `role` | the character's name and one phrase on who they are |
| `voice` | a role name (`default`, `keeper`, `merchant`, `smith`, `guard`, `healer`, `scholar`, `narrator`), a Kokoro voice name or a speaker id |
| `knows[]` | what this character knows and may speak about, one clause per entry |
| `forbidden[]` | subjects named as off-limits for this character, on top of the fixed limits |
| `deflect` | how this character turns an out-of-role question away, in their own body language |

A character the world file does not list can be described by the game in the `HELLO` frame (`character`, `lore_scope`, `voice`), which keeps the runtime usable without an authored world.

**Memory.** The last 24 messages are replayed verbatim into the next session; when the transcript overflows, the oldest are summarised by the same model into at most 40 durable facts about the player, and the transcript is trimmed even if the summariser fails. "My name is Alex" becomes a fact immediately, without a model call. Writes are atomic, wire ids are reduced to safe file names, and a line the player cut off is stored marked `interrupted` (the stock profile does not replay it to the model).

**Tools, honestly.** The three engine tools are a contract, a wire frame and a Unity event, with stub results on the Python side. The live language-model stage does not send tool definitions to the model or parse tool calls, so today the character speaks but does not gesture or raise game events on its own; wiring that is open work.

**Testing a character without a person.** A probe script renders the player's line with the same Kokoro model in a different voice and streams it to the server in 20 ms packets, exactly as a microphone would, so everything downstream is the real path; three scripted dialogues check name memory, isolation between characters and refusal of out-of-role questions.

---

## Wire protocol

TCP, no dependencies on either side. One connection is one NPC; the engine's dialogue LOD decides which NPCs hold one, and `max_sessions` caps it server-side as a backstop. Every frame:

```
type (1 byte) | length (4 bytes, big-endian, unsigned) | payload     max payload 4 MiB
```

| Dir | Type | Name | Payload |
|---|---|---|---|
| → | `0x01` | `HELLO` | JSON `{"proto":1, "npc_id", "character", "lore_scope", "voice", "player_id"}` |
| → | `0x02` | `TEXT_IN` | UTF-8 text the player typed |
| → | `0x03` | `AUDIO_IN` | PCM16 mono 16 kHz microphone audio (runs VAD → barge-in) |
| → | `0x04` | `EVENT` | JSON `{"event", "payload"}`: an in-game event for the NPC |
| → | `0x05` | `INTERRUPT` | empty: the engine detected the player talking over the NPC |
| → | `0x06` | `LOD` | JSON `{"tier", "distance_m", "priority"}` |
| → | `0x07` | `ENDPOINT` | empty: push-to-talk released, end the utterance now |
| ← | `0x81` | `READY` | JSON `{"npc_id", "sample_rate", "channels":1, "format":"pcm16"}` |
| ← | `0x82` | `AUDIO_OUT` | PCM16 mono at `READY.sample_rate`: queue it into the engine's audio source |
| ← | `0x83` | `TEXT_OUT` | JSON `{"text", "turn_id"}`: words as they are voiced, for subtitles and visemes |
| ← | `0x84` | `TURN_END` | JSON `{"turn_id", "metrics"}` |
| ← | `0x85` | `FLUSH` | empty: barge-in, drop every queued sample now |
| ← | `0x86` | `TOOL` | JSON `{"tool_name", "arguments", "call_id"}`, e.g. `play_animation` |
| ← | `0x87` | `STATE` | JSON `{"state"}`: `idle`, `listening`, `processing`, `speaking`, `interrupted`, `error` |
| ← | `0x88` | `STT` | JSON `{"text", "final"}`: what the player was heard to say |
| ← | `0x8F` | `ERROR` | JSON `{"message"}` |

The protocol is implemented three times from one spec — Python (`voicert.game.bridge`), engine-agnostic C# (`Runtime/Core/VoiceRTProtocol.cs` in the Unity package) and header-only C++17 (`VoiceRTProtocol.h` in the Unreal plugin) — and the C# side is tested against the real Python server, not against a copy of it.

Per live NPC the audio costs ~32 KB/s of PCM16 at 16 kHz in each direction (48 KB/s out at Kokoro's 24 kHz); the FMOD sink holds up to `bufferSeconds` of it (30 s by default, because a whole reply arrives long before it is spoken).

---

## Unity package

`com.voicert.npc` — a UPM package, Unity 2021.3+, verified in Unity 6 (6000.6) with FMOD for Unity 2.03.14. It ships with the main repository.

| Component | What it does |
|---|---|
| **VoiceRT NPC** | One per character. Holds the bridge connection (`host`, `port`, `npcId`, `character`, `voice`) and raises `onSubtitle`, `onTool`, `onTurnEnd`, `onFlush`, `onState`, `onTranscript`, `onReady`, `onError`. `Say(text)` drives a typed turn. |
| **VoiceRT FMOD Sink** | Creates and owns one `EventInstance` from an `EventReference` whose only instrument is a programmer instrument, and feeds it from the ring buffer. Compiled only when FMOD for Unity is in the project: an editor script sets the `VOICERT_FMOD` scripting define, and the `VoiceRT.Fmod` assembly is constrained on it. |
| **VoiceRT AudioSource Sink** | The no-middleware path: a streaming `AudioClip` on an ordinary `AudioSource`. |
| **VoiceRT Voice Input** | One per player, not per NPC; the game points it at whoever is being spoken to and calls `BeginUtterance()` / `EndUtterance()` from its own input system. Push-to-talk by default: with an open mic the VAD hears the NPC through the speakers and interrupts itself. Backends behind `IVoiceRTMicSource`: FMOD Core record (primary), `UnityEngine.Microphone` (fallback). A loop buffer (5 s on FMOD, 10 s on the Unity fallback) with overrun detection keeps a stalled frame from handing over stale audio. |
| **VoiceRT Dialogue LOD** | Distance tiers with hysteresis (LIVE 6/9 m, BARK 25/32 m, CROWD 60/75 m by default) and a conversation override; opens and closes the socket as the player crosses the LIVE band. |

`Tools~/MicCheck` is a standalone device checker for the case the capture code was built around: the default Windows microphone is often a virtual endpoint that records silence, or a physical one another process holds (`ERR_RECORD`). The runtime walks past both on its own; MicCheck shows you why.

---

## Reference setup

What the numbers on this page were measured on, and what a build needs. Windows 11 only.

| | |
|---|---|
| Machine | RTX 4080 16 GB, i9-12900K (24 logical CPUs), Windows 11 Pro; NVIDIA driver 617.14 (CUDA Toolkit not needed, the `cuda` extra ships the runtime DLLs) |
| GPU memory | 8,176 MiB for the stack with nothing else running, 8,731 MiB with the game rendering (measured 2026-09-09: `qwen3:8b` 5.58 GB, Whisper +579 MiB, Kokoro +0.77 GB); Ollama 0.35.0 reported 8.40 GB for the resident LLM alone on 2026-10-02, so budget more. Below roughly 12 GB a card cannot be expected to hold the stack and a game together (inference, not measured) |
| Engine side | Unity Editor 6000.6.0f1; FMOD Studio and FMOD for Unity 2.03.14 (a runtime loads banks built by the same or an older Studio, so the two stay together) |
| Server side | Python 3.13 (the core allows 3.11+); faster-whisper 1.2.1; sherpa-onnx 1.13.7 with the CUDA 12 / cuDNN 9 wheel; Ollama 0.35.0 |
| Models | Whisper `small.en`, `qwen3:8b`, Kokoro v1.0 fp32 (about 6 GB on disk) |
| Tests | .NET SDK 10 for the C# suite (`net10.0`), .NET 8 runtime for MicCheck |

The server loads and warms every model before its listener opens, then prints a `VOICERT READY` line with the TTS sample rate, the voice count and the placement of each stage; a launcher waits for that line. At boot a GPU gate checks that Kokoro and Whisper are on CUDA and
that Ollama holds the LLM fully in VRAM, and exits with code 3 otherwise. If Ollama is not answering, the server starts it. After the models are on disk it runs offline.

---

## Measurements

All on the reference workstation (RTX 4080 16 GB, i9-12900K, Windows 11). Method and caveats: [docs/local-stack.md](docs/local-stack.md).

| What | Result |
|---|---|
| End of speech → first NPC audio, full local stack | p50 **489 ms** (n=10, scripted speech input through the probe script, editor idle) · **850–1100 ms** with the game rendering on the same GPU |
| Kokoro fp32 on CUDA (v1.1 benchmark, idle GPU; the stack ships v1.0) | RTF **0.07–0.11**, first chunk 175 ms / 91 ms (1.6 s / 5 s sentence); int8 on CUDA is RTF 1.85 because the quantized ops round-trip through the CPU, so use fp32 on the GPU |
| Whisper `small.en` vs `base.en` | **4.6 %** vs 12.0 % word error over ten spoken lines, for +27 ms and +400 MiB; `base.en` heard "Who is Elon Musk?" as "Who is a lawn musk?" |
| Holding the character (six probes against the guard persona) | `qwen3:1.7b` 3/6 · `qwen3:4b` 2/6 · `qwen3:8b` **6/6** at 5.6 GB VRAM |
| Local LLM latency (12-turn tavern keeper) | `qwen3:1.7b` warm TTFT 45 ms, full reply ~121 ms · `qwen3:14b` TTFT ~41 ms, full reply ~710 ms: decode speed separates models, not TTFT |
| Barge-in through the live gate | 1,546 ms of buffered reply cut to 0 ms in 7 ms |

The model was chosen by the role-holding probe, not by latency: a smaller model answers sooner and stops being the character (`qwen3:1.7b` explained quantum entanglement to a night guard).

---

## Game layer: dialogue LOD and the voice pool

What you pay for is what the player can hear, not how many NPCs exist.

| Tier | What runs | Voice pool | LLM work |
|---|---|---|---|
| **LIVE** | full pipeline: STT → LLM → TTS, streamed | one slot | a streamed generation |
| **BARK** | the LLM picks a line from a pre-baked bank | none | one short classification call |
| **CROWD** | one shared murmur bed | none | none |
| **OFF** | nothing, NPC state frozen | none | none |

Live agents come out of a fixed-size pool with priority eviction, the way FMOD and Wwise already limit voices; an evicted NPC degrades to BARK, never to silence. The pool size is computed, not guessed: `ComputeBudget(rtf=0.10, cpu_share=0.35, speaking_duty=0.5).max_pool_size()` returns 7. A simulation walks a player across a 240-NPC market square:

```
PLAYER WALKS 40 m ACROSS THE SQUARE
  step 1: live=  2  bark= 13  crowd= 63  off=162  | agents held: 2
  step 3: live= 15  bark= 36  crowd= 68  off=121  | agents held: 7
  step 4: live= 32  bark= 43  crowd= 68  off= 97  | agents held: 7  ← capped
```

`live=` is how many NPCs qualify for the LIVE tier; `agents held:` is how many got a pool slot. This is the simulation: the pool and the BARK/CROWD tiers are implemented and tested in `voicert.game`, but the bridge server does not use the pool yet — it caps concurrent NPC sessions (8 by default), and the Unity `VoiceRT Dialogue LOD` component opens and closes sockets by distance.

---

## Cost model

`voicert.economics` prices a turn against vendor rate cards read on 2026-08-31. The turn: 282 fresh + 700 cached prompt tokens, 23 output tokens and 92 characters synthesized, measured over a 12-turn conversation on `qwen3:1.7b`, plus an assumed 3 s of player audio.
On the cheapest all-cloud stack, speech synthesis is ~90% of a turn's cost, speech recognition ~8% and the LLM under 2%; the fully on-device stack has no per-turn cost. For cloud stages there is a spending-ceiling ledger, built and tested but not yet wired into the bridge (it goes in with the cloud providers, M3):
money is integer nano-dollars, the ledger reserves before a turn and settles after it (safe under concurrency), `BudgetDenied` carries an affordable authored fallback line, and barge-in stops the meter. The full table and the argument: [docs/economics.md](docs/economics.md).

---

## Layout of the main repository

```
src/voicert/             frames, pipeline, interruption, state, config (NPC profile), tools (firewall),
                         metrics, transport (EnergyVAD, loopback), utterance (segmenter), qos (Windows scheduling)
src/voicert/processors/  base contracts, stubs (deterministic, for CI), local (Whisper, Ollama + SentenceBuffer,
                         Kokoro via sherpa-onnx), adapters (cloud skeletons, M3)
src/voicert/game/        bridge (TCP protocol), local_server + local_stack (GPU stack, GPU gate), agents (world file,
                         memory per NPC and player), session (state machine), lod, pool, budget, sinks
src/voicert/economics/   money (integer nano-dollars), prices, usage, ledger (reserve / settle), planning
integrations/unity/      com.voicert.npc UPM package (FMOD sink, mic backends, LOD, MicCheck) + C# xUnit tests
integrations/unreal/     VoiceRT .uplugin (header-only C++17 protocol, FSocket client) + standalone C++ tests
unity-project/           the Unity demo project: our own files only (third-party content is restored by a script)
fmod-project/            the FMOD Studio project and the scripts that rebuild its buses and mix
examples/                demos, a terminal conversation client, a speech-in probe, benchmarks, the world file
scripts/                 Windows setup, Unity import, environment check, model fetch, FMOD build, dev session, test runner
docker/                  the bridge as a container
tools/unit_economics.py  prints the cost table from measured usage
docs/                    engineering documents, setup, operations, decisions
```

---

## Tests

`pytest -q`: **229 passed**, no network, no GPU, no keys. Providers are deterministic stubs behind the same interfaces as the real ones; the GPU gate and the Ollama autostart are tested against monkeypatched device and process reports.

| File | Tests | Covers |
|---|---|---|
| `test_economics.py` | 46 | integer money, rate cards, the reserve/settle ledger under concurrency, ceilings that hold |
| `test_agents.py` | 39 | world files, prompt assembly, memory per (NPC, player), Windows-safe file names, interrupted lines not replayed as finished |
| `test_game_layer.py` | 23 | LOD tiers and hysteresis, pool eviction, compute budget |
| `test_local_gpu.py` | 20 | CUDA DLL discovery, sherpa-onnx CUDA detection and provider resolution, the int8-on-GPU guard, first-token timing |
| `test_local_server_boot.py` | 19 | Ollama autostart and its failure modes, offline mode set before the model hub loads, the GPU gate (CPU voice or recogniser, LLM split or not in VRAM) |
| `test_bridge.py` | 17 | framing, HELLO/READY with the TTS rate, error paths, a text turn over a real socket, INTERRUPT → FLUSH, tools forwarded, session cap, abrupt disconnects |
| `test_session_state.py` | 16 | the per-NPC state machine, clauses never repeated, sentence cutting without punctuation |
| `test_utterance.py` | 16 | pre-roll, minimum length, stuck-VAD flush, a pause under a held key does not split the utterance, open mic still endpoints on silence |
| `test_config_factory.py` | 14 | the NPC contract and the tool firewall |
| `test_interruption.py` | 9 | barge-in races, the stock NPC's instant cut, both context policies |
| `test_pipeline.py` | 6 | frames end to end, metrics, immutability |
| `test_turn_closing.py` | 4 | a failed turn still closes, memory survives a failure, a cut turn stays open for reconciliation |

C#: **21 passed**, compiled against the package's own sources (no copy drift), including a test that launches the Python bridge and drives a full turn plus a barge-in over a real socket. C++: the Unreal plugin's protocol tests build with MSVC or g++ and need no Unreal install.

---

## Roadmap

- [x] Core runtime, barge-in with history repair, the NPC contract, game layer, TCP engine bridge, economics ledger.
- [x] **M1** FMOD programmer-instrument sink, compiled in Unity 6 with FMOD for Unity 2.03.14, verified with a real microphone on the full local stack; a Windows player build starts its own server on the development machine.
- [ ] **M2** Demo scene and a 30-second video: mic → NPC → FMOD event, barge-in and memory on screen.
- [ ] **M3** Cloud providers (Deepgram, Claude Haiku, ElevenLabs Flash) running end to end behind the same adapters, next to the local path that already does, with the spending ceiling wired in.
- [ ] **M4** UPM 0.1 release; Unreal + Wwise at parity.

---

## Team

Raph (Rafael Kuldashev): more than ten years in sound and voice; the design comes from the mix, not from an SDK. Vyacheslav Romanenko: 14+ years of technical sound design and FMOD on PC, console and mobile. VoiceRT is in the Fall 2026 cohort of AltaLab, the accelerator run by AltaIR Capital.

## License

All rights reserved, (c) 2026 Raph. This repository holds the project description and the product site; the source lives in a private repository that will be opened later. It is not open source, and no license to use, copy, modify or distribute any VoiceRT code is granted without written permission.
Third-party runtimes keep their own licenses, and FMOD needs its own licence from FMOD. Pipecat, LiveKit Agents and Rapida AI were studied as design references; every design decision and the implementation are original.
