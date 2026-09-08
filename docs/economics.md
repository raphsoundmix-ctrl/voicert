# Economics: what a talking NPC costs

Two questions come first for any game developer. What does it cost to run, and does it pay for itself? Both have numbers here, and `voicert.game` plus `voicert.economics` implement the answer. Licensing has its own page: [licensing.md](licensing.md).

## Voicing an open world: what it costs

The recording bill, the four dialogue tiers, the frame budget, and what to bake versus generate.

### What recorded dialogue costs

You pay for recorded dialogue once, and again every time the script changes. The SAG-AFTRA Interactive Media Agreement minimum for an off-camera Day Performer is **$1,134.95 for a 4-hour day covering up to three voices** and **$2,270.78 for a 6-hour day covering 6–10 voices** (contract year 2025-11-01 to 2026-10-31). That is the floor, before studio time, direction, engineering, editing and retakes, and before any name talent.

Then look at the scale. Skyrim shipped around **60,000 lines**. Fallout 4, **111,000**. Starfield was announced at **over 250,000 lines** (Bethesda's own pre-release count from October 2022). Baldur's Gate 3 holds the Guinness record with **2,121,425 words of character dialogue** across 500+ voiced characters and 240+ actors. A two-person studio cannot buy that once, let alone twice.

The industry has already moved. **7,818 Steam titles disclosed generative AI use** in a July 2025 analysis, roughly 7% of the library and about one in five games released that year, up from around 1,000 titles in all of 2024. The 2024–25 strike settled with real terms attached: informed written consent for each use, disclosure, and usage reports within 90 days of release. Synthetic voice is not a way around performers. It is a way to voice the bulk of an open world that nobody was ever going to record.

### Four tiers, and only one of them is expensive

Dialogue gets a level-of-detail system, the same way geometry does. What you pay for is what the player can hear, not how many NPCs exist.

| Tier | What runs | Voice pool | LLM work |
|---|---|---|---|
| **LIVE** | full pipeline: STT → LLM → TTS, streamed | one slot | a streamed generation |
| **BARK** | the LLM picks a line from a pre-baked bank | none | one short classification call |
| **CROWD** | one shared murmur bed | none | none |
| **OFF** | nothing, NPC state frozen | none | none |

BARK is where most of an open world lives, so it is worth being precise about it. There is no synthesis and no new audio asset, just playback of a line you already shipped. The LLM is still involved, but only to choose which line fits, which is a far cheaper call than generating speech.

Each threshold has a wider exit distance than its entry distance: an NPC enters LIVE at 6 m but only drops out of it at 9 m. Without that gap, a player walking along the boundary would create and destroy agents on every render frame.

An NPC you are actually talking to stays LIVE whatever the distance, so walking away mid-sentence does not cut the character off.

Live agents come out of a fixed-size pool. When it is full, the lowest-priority agent is dropped, which is how FMOD and Wwise already handle voices. A quest-giver outranks an ambient vendor, and anything dropped falls back to the bark tier instead of going silent. In `examples/game_open_world.py`, a market square with 240 NPCs never holds more than 7 agents, however the crowd moves:

```
DIALOGUE LOD    (240 NPCs in the square)
  LIVE      2 NPC   full STT+LLM+TTS, one pool slot each
  BARK      4 NPC   LLM picks a pre-baked line — no synthesis
  CROWD    53 NPC   one shared murmur bed
  OFF     181 NPC   nothing runs

PLAYER WALKS 40 m ACROSS THE SQUARE
  step 4: live= 32  bark= 43  crowd= 68  off= 97  | agents held: 7  ← capped
```

The two counts on the last line measure different things. `live=` is how many NPCs are close enough to qualify for the LIVE tier, and `agents held:` is how many actually got a pool slot. When the first number exceeds the pool size, the rest fall back to BARK. That gap is the cap working.

### It stays out of the frame budget

At 60 fps you get 16.6 ms per render frame, and speech synthesis gets none of it. Synthesis runs off the render thread, so what actually limits you is a fraction of one CPU core, set by the real-time factor of your TTS. RTF is seconds of CPU per second of audio produced, so anything below 1.0 is faster than real time.

The sherpa-onnx maintainers measured Piper VITS voices on a **Raspberry Pi 4**: **RTF 0.774–0.812 on one thread**, **0.349–0.357 on four**, with 61–75 MB per medium-quality voice. If a Pi 4 can synthesize faster than real time on a single core, a gaming PC is not the bottleneck. On Apple silicon, Supertonic reports **RTF 0.012–0.015 on an M4 Pro CPU**, roughly 70 times faster than real time.

`ComputeBudget` turns that into a pool size instead of a guess:

```python
from voicert.game import ComputeBudget
ComputeBudget(rtf=0.10, cpu_share=0.35, speaking_duty=0.5).max_pool_size()  # -> 7
```

Measure RTF on your minimum spec, not on your workstation.

NVIDIA's shipped work uses the same split. In PUBG's Ally Duo, a System-1 behaviour tree handles reflexes at game tick rate while the System-2 model reasons separately, so reflexes never wait for the model. VoiceRT works the same way: the pipeline is never on the frame's critical path.

### Bake what you can, generate what you cannot

| Stage | Baked at build time | On the player's device | Cloud |
|---|---|---|---|
| Static dialogue | yes, best quality TTS, paid once | — | — |
| Reactive barks | — | yes, local TTS, $0 per player | — |
| Open conversation | — | yes, small local LLM plus local TTS | optional, for the big moments |

Baking is the default for a reason. Commodity cloud TTS costs **$4–$30 per 1M characters** (Google Standard/WaveNet $4, Neural2 $16, Chirp 3 HD $30; Amazon Polly Standard $4, Neural $16, Generative $30). At that price a 10,000-line bank is a rounding error, and re-baking it after a script change costs nothing.

Whatever cannot be baked runs on the player's hardware, where each extra line costs the studio nothing. That is what makes dynamic dialogue possible at all: the cost does not grow with units sold.

Games ship this today. PUBG's Ally Duo runs a quantized **2B Mistral-NeMo-Minitron entirely on-device** (an RTX GPU with at least 8 GB of VRAM, shared with the game) next to a Parakeet ASR and KRAFTON's own TTS. inZOI's Smart Zoi uses a **0.5B** Minitron, and NVIDIA says a 2B Minitron "fits in as little as 1.5 GB of VRAM". On phones, **Gemma 3 1B is 529 MB at int4 QAT** and reaches about 2,585 tok/s prefill on a Galaxy S24 Ultra (Google suggests at least 4 GB of device memory). Meta's 4-bit Llama 3.2 1B/3B cut size by 56% and memory by 41%, with decode 2.5 times faster. Apple's roughly 3B on-device model runs at about 30 tok/s on an iPhone 15 Pro and is free to developers through the Foundation Models framework.

Where cloud really is the right answer, small models are cheap: **$0.05–$1.00 per 1M input tokens** (gpt-5-nano $0.05/$0.40, Gemini 2.5 Flash-Lite $0.10/$0.40, Claude Haiku 4.5 $1.00/$5.00). Set a cap per session and fall back to the bark bank when you hit it. VoiceRT treats that as a normal tier change, not an error.

### Try it

```bash
python examples/game_open_world.py     # LOD, pool, budget, cost model
pytest tests/test_game_layer.py -q     # 23 tests covering all of it
```

---

## What the bridge costs to run

Two budgets, both bounded by design rather than by luck.

**Network and CPU, per live NPC.** PCM16 mono at 16 kHz is 32 KB/s down; text and tool frames are a few hundred bytes each. On a LAN or the same machine this is nothing. The ring buffer on the engine side (`PcmRingBuffer` in C#, the equivalent in C++) holds 0.25–5 seconds of audio and resamples on the audio thread with linear interpolation — no allocation, no lock held across the resample, so it cannot glitch the audio callback even under load. Measured on this machine: the whole protocol layer round-trips through a real Python bridge process in the C# xUnit suite (`BridgeIntegrationTests.cs`) in about 2 seconds for two full turns including a barge-in, with zero allocation-related failures across repeated runs.

**Memory, per live NPC.** The engine side holds one ring buffer (default 2 s × 16 kHz × 2 bytes ≈ 64 KB) plus one procedural sound object. That is the entire footprint — no model weights, no ONNX runtime, no phoneme tables live in the game process, because none of that runs there. The number of *possible* NPCs in the world costs nothing at all: an NPC in the BARK or CROWD tier is just game state (a `DialogueTier` enum and a couple of floats) until it earns a LIVE slot.

**What actually gates you** is the same CPU budget from the cost model above, on the *voicert* side: `ComputeBudget(rtf, cpu_share).max_pool_size()`. That number is your realistic concurrent-NPC-conversation limit, independent of how large the open world is.

## Big voice banks: what actually needs baking, and what does not

A large NPC roster has always meant two separate costs, and this framework only removes one of them.

**Recording is the cost this removes.** A cast of hundreds of NPCs, each with even a modest line count, is a SAG-AFTRA session-rate problem before it is anything else (see the cost model above). Runtime synthesis for the LIVE and BARK tiers sidesteps that recording bill entirely — there is nothing to record for a line the LLM composes at runtime.

**Engine setup is the cost that does not disappear, whichever way you produce the audio.** Every clip Unity or Unreal plays still needs the same decisions: Unity's *Load Type* (`Decompress On Load` for short, frequently-triggered barks vs `Streaming` for long lines) and compression settings per platform; Unreal's compression per `USoundWave` and whether a line lives on a `Sound Cue` or a MetaSound. VoiceRT does not touch this, because runtime-synthesized audio is not a `.wav` asset at all — the PCM never becomes an import-time decision, it becomes a stream. That is the actual optimization for a "huge voice bank": most of it stops being a bank. Static, pre-written lines (the "baked" row in the cost model's hybrid table) are the only ones that still go through normal asset import, and they are the minority by design — the framework routes reactive barks and open conversation through the live pipeline specifically so they never need a Load Type decision at all.

Two engine-side costs are out of scope for this repo and worth planning for separately: **lip-sync**, which needs either a runtime viseme generator (Meta's Movement SDK / Oculus Lipsync, NVIDIA Audio2Face) driven off the same PCM this bridge delivers, or a MetaHuman-style offline pass, which only applies to pre-baked lines; and **voice identity**, since giving hundreds of NPCs distinct voices is a TTS-model question (multi-speaker models, voice-cloning, or per-archetype voice banks), not an engine-integration one — the bridge's `voice` field in `HELLO` is where that choice plugs in once you pick a TTS backend.

## On-device is the cost lever

Every stage of the pipeline has a local option. Moving only synthesis on-device (the "cloud brain, local voice" row in the cost table below) is the 10× lever; the fully on-device row turns the per-use meter into $0 per turn:

| Stage | Cloud (today) | On-device (the lever) |
|---|---|---|
| VAD | always local | Silero VAD |
| STT | Deepgram Nova-3 | faster-whisper on GPU/NPU |
| LLM | Claude Haiku / Sonnet | llama.cpp-class models |
| TTS | ElevenLabs / Cartesia | Piper / Kokoro on desktop — see the caveats below for phones |
| Memory | any vector DB | sqlite-vec, a vector database in one file |

One caveat on that last row, because it is the row that decides whether a mobile game target is viable. Piper's licensing and Kokoro's speed on low-power ARM both rule them out for a phone target as things stand today (details in [Licensing](licensing.md#licensing-will-bite-you-before-performance-does)). Nothing has been benchmarked on a phone yet. The current candidate is sherpa-onnx with an espeak-free voice, and that still has to be measured.

The demo already runs stub providers through the same interfaces a real model would use. 121 tests pass through those seams, so switching to a local model is an adapter, not a rebuild.

---

## The $10 game and the $15 player

A game designer put the objection better than most:

> I sold a game for $10, and a player burned $15 of tokens.

That is the right objection, and a one-time price against a per-use meter is a real structural problem. A subscription can absorb a heavy user by averaging them against a light one. A $10 purchase cannot: the revenue arrives once and the meter keeps running, and there is no playtime at which it stops. So the question is not whether that can happen. It is what the actual numbers are, and what in the framework stops them.

### First, the bill is not where the argument assumes

`voicert.economics` prices a turn from measured usage against rate cards read from the vendors on 2026-08-31. Run it yourself:

```bash
python tools/unit_economics.py
```

The turn is the measured one from [local-stack.md](local-stack.md): 282 fresh + 700 cached prompt tokens, 23 output tokens, 92 characters synthesized, 3 s of player audio.

| stack | per turn | LLM | STT | TTS | $15 buys |
|---|---|---|---|---|---|
| all cloud, cheap | $0.001532 | 1.7% | 8.2% | **90.1%** | 9,792 turns |
| all cloud, premium | $0.005307 | 8.8% | 4.5% | **86.7%** | 2,826 turns |
| cloud brain, local voice | $0.000152 | 17.7% | 82.3% | 0% | 98,814 turns |
| premium brain, local voice | $0.000707 | 66.1% | 33.9% | 0% | 21,216 turns |
| fully on-device | $0.000000 | 0% | 0% | 0% | unbounded |

**Speech synthesis is ~90% of a cloud voice turn. The language model is under 2%.** Everyone says "tokens", and tokens are the cheapest part. Swapping to a cheaper LLM optimizes a rounding error.

That reframes the fix. The lever is not the model — it is the voice. And **local TTS is tractable where a local LLM is not**: a Piper-class voice is ~60 MB of CPU-only inference with no GPU, no CUDA and no vendor lock, so it runs on the ~47% of Steam machines with 8 GB of VRAM or less and the ~27% whose GPU is not NVIDIA (Steam Hardware Survey, July 2026). Moving that one modality on-device is a 10× cut. `PriceBook.hybrid_local_tts()` is that configuration.

### Second, the meter needs a stop

Every profile here already declares a **latency** budget and the pipeline holds it. `voicert.economics` gives a profile a **money** budget on the same footing:

```python
policy  = BudgetPolicy.from_revenue("10.00", margin_target=0.90)  # $1.00/player, enforced
session = SessionLedger("tavern", PlayerLedger("p1", policy), policy, PriceBook.hybrid_local_tts())

auth = session.authorize(estimate, TierPlan.local_tts())
if isinstance(auth, BudgetDenied):
    play(auth.fallback)          # pre-authored line — always affordable, never an error
else:
    session.settle(auth, actual)
```

Three properties, each tested:

- **Denial is a value, not an exception.** Running out of budget returns a `BudgetDenied` carrying an affordable `fallback`; only programmer error raises. A player must never meet an error dialog because a studio hit a spending cap — they should meet an NPC reading its authored lines.
- **Reserve, then settle.** Two NPCs talking to one player can each pass an affordability check and jointly bust the cap. Check-and-hold is one atomic step, so the ceiling holds under concurrency.
- **The ledger alone cannot make the ceiling hard, and says so.** By the time `settle()` runs the provider has already generated the tokens and the money is gone, and the estimate is *systematically* low because output length is unknown at authorize time. So `Authorization` hands the caller the cap — `auth.max_characters(book)` and `auth.max_output_tokens(book)` — to pass down to the provider. Use them and the bound is hard; ignore them and it is "the ceiling, plus one turn's overrun".
- **Barge-in is a cost mechanism.** The pipeline already cancels generation the instant a player interrupts. `session.abandon()` is where that reaches the invoice: the prompt is owed, the unspoken remainder of the reply is not.

Money is integer nano-dollars, never float — the guarantee is an invariant over a running sum, and float addition is not associative, so two players making identical turns in a different order would get different answers.

### What the objection gets right

- **A per-use meter against a one-time price has no upper bound.** That is a real structural mismatch, and nothing above makes it not one. It makes it *bounded*, which is a different claim.
- **The measurements here are from an idle machine.** No renderer was competing for the GPU.
- **A 1.7B model does not hold character** (four failures in twelve turns; see [local-stack.md](local-stack.md)). The smallest model that survives a long conversation is unmeasured, and if it turns out to be 7–8B the local tier narrows to the machines that can spare 5–6 GB.
- **The basic implementation really is a weekend.** Microphone → Whisper → LLM → TTS is not hard, and saying otherwise would be dishonest. What is not a weekend is everything that makes it survive contact with a game: barge-in that truncates memory to what was actually *heard*, VAD pre-roll, voice budgeting across 50 NPCs — and above all routing generated audio through the same busses, attenuation, occlusion and ducking as every other sound, which is the one part of this problem that is shaped like a game rather than like a voice agent.

---

## This is a moving field, not a lone bet

Two bets this project makes — cheap local voice models, and routing between a cheap model and an expensive one to hit a cost target — are both active research and investment fronts in 2026, not something unique here.

**Local voice models, cheaper every quarter.** Self-hosted Kokoro-82M runs ~$0.65 per 1M characters vs. ~$100 for premium cloud TTS, and the Elo quality gap between open and commercial TTS has closed 64% (223 → 81) since 2023 ([OfflineTTS landscape report, 2026](https://offlinetts.com/blog/tts-stt-landscape-h1-2026/)). Kokoro itself now compresses to ~80 MB with no quality loss ([TTS Arena](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX)); newer entrants (Supertonic 3, Neuphonic's NeuTTS) push the curve further, and a June 2026 paper cuts TTS inference memory 75% via KV-cache compression ([arXiv 2606.09019](https://arxiv.org/abs/2606.09019)).

**Cost-aware model routing is now a paid feature at every major vendor.** [RouteLLM](https://arxiv.org/abs/2406.18665) (Berkeley/LMSYS) showed a trained router cuts LLM cost 50%+ at ~95% top-model quality. AWS Bedrock, Microsoft Azure AI Foundry (an explicit "Cost" mode across 27 models), OpenAI's GPT-5, and Google's ["speculative cascades"](https://research.google/blog/speculative-cascades-a-hybrid-approach-for-smarter-faster-llm-inference/) all now ship the same idea. `TierPlan`/`TierRouter` here applies it to the one modality none of them route: voice.

**Games are converging on the same pattern.** NVIDIA shipped on-device Qwen3-8B support for ACE in PC games (Oct 2025); Krafton's inZOI runs on-device NPCs. Covering an indie studio's "AI Village," Frisson Labs quotes the team choosing local inference explicitly "to negate the cost of inference" — the same trade made here ([Frisson Labs, May 2026](https://www.frisson-labs.com/ai-npcs-2026)).

**The money already backing it.** AI inference is a ~$106–108B market in 2025 growing toward $500B+, and three inference-serving startups (Fireworks, Together AI, Baseten) each raised $800M–$1.5B in a single month in mid-2026 ([market.us](https://market.us/report/ai-inference-market/)).

*This is a solo-built prototype, not a funded platform — the above is the field it sits inside, not a claim of parity with it.*
