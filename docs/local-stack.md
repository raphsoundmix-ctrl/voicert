# Talking to an NPC with a real microphone, fully offline

No cloud, no API keys, no paid inference. Every stage is local and wired: a player holds a key,
speaks, and a character answers in its own voice from its own position.

```
Unity Microphone ──PCM16 16 kHz──▶ AUDIO_IN ──▶ [ VAD ] ──▶ [ UtteranceSegmenter ]
                                                   │                    │
                                    barge-in ◀─────┘        one whole utterance
                                                                        ▼
        AudioSource ◀── AUDIO_OUT ◀── [ TTS ] ◀── sentences ◀── [ local LLM ] ◀── [ Whisper ]
```

**The part that is not obvious.** A microphone hands you 20-50 ms chunks forever; Whisper wants one complete utterance. Something has to decide where an utterance begins and ends, and feeding the model individual chunks produces confident nonsense — every chunk looks like a complete short sentence to it. `voicert.utterance.UtteranceSegmenter` is that decision. It keeps a rolling **pre-roll** buffer (300 ms by default) and prepends it when the VAD fires, because a VAD needs energy before it triggers — without pre-roll the model hears "ello" instead of "hello". It also drops anything under 250 ms (coughs, chair scrapes) and force-flushes past 20 s, so a fan near the mic cannot grow the buffer until the process dies. Set `transport.segmenter` and only whole utterances reach the pipeline; leave it unset and the old chunk-per-frame behaviour is unchanged.

The second non-obvious piece is **sentence chunking before TTS**. Feeding the LLM's token stream straight into synthesis gives every word its own falling intonation. `SentenceBuffer` holds tokens until a sentence boundary, then releases the whole clause, so TTS can place stress and breath — with a character-count escape hatch for models that forget punctuation. `OllamaLLM` yields those clauses; measured on a 25-word reply, per-token synthesis produced 20.1 s of audio at 1.25 words/second, per-clause 8.8 s at 3.4. `llm_first_token` is stamped by the provider on the first *token* (`LLMService.note_first_token`), so buffering never shows up as model latency.

A line the player **types** does not go through any of this: `AgentRuntime.say_text()` opens the turn and pushes a final user `TextFrame` straight past the STT stage. Sending typed text as pretend audio only works with the stub STT — Whisper reads those bytes as PCM and the failure is swallowed by the pipeline.

**Measured on this machine** (RTX 4080 16 GB, Ollama, `keep_alive=-1`, `think=false`, streamed `/api/chat`, timed to the first content chunk). A 12-turn tavern-keeper conversation with a 696-token persona prompt, prompt tokens plateauing at ~980:

| Model | warm TTFT | full reply | cold first call | model file | VRAM actually held |
|---|---|---|---|---|---|
| `qwen3:1.7b` | **45 ms** | ~121 ms | 322 ms in-process / ~34 s from disk | 1.7 GB | **~2.3–2.7 GB** |
| `qwen3:14b` | ~41 ms | ~710 ms | 7,959 ms | 9.6 GB | ~9.5 GB |

Three things a game has to act on.

**Time-to-first-token barely separates the two models — decode speed does.** Both prefill a short prompt in roughly 40 ms. The 1.7B finishes a reply in ~121 ms against the 14B's ~710 ms, and since first audio waits on the first *sentence* rather than the first token, that 5.9× gap is what actually pushes the 14B past the NPC profile's budget.

**The VRAM column is what the card holds, not the file size.** `ollama ps` reports 1.7 GB for `qwen3:1.7b`; `nvidia-smi` taken at the same moment reports 2.3–2.7 GB, because the file excludes the KV cache and CUDA context. Budget against the larger number.

**Ollama evicts an idle model by default** — `ollama ps` shows `UNTIL 4 minutes from now` — so the first NPC to speak after a quiet stretch pays a cold load measured here at ~34 s from disk. `OllamaLLM` therefore defaults to `keep_alive=-1` and exposes `await llm.warmup()` for level load.

Two caveats that cut against these numbers, stated because they are the ones a reader would otherwise have to discover for themselves:

- **This was an idle machine.** No game was rendering on the same GPU. Contention for VRAM, SM time and thermal headroom is unmeasured here, and on Windows the failure mode when you get it wrong is a stutter, not a smaller number.
- **1.7B is fast but does not hold character.** Across the same 12 turns it referred to the innkeeper in the third person *from inside his own mouth* on four of them, regurgitated a few-shot example nearly verbatim, and contradicted its own room price. Latency at that size is solved; fidelity is not. Treat the local tier as a real option for barks and ambient chatter, and assume a larger model — local or cloud — for anything a quest depends on.

**Wiring it up:**

```bash
pip install -e ".[local,cuda,bench]"   # faster-whisper + httpx + sherpa-onnx + CUDA 12 DLLs + psutil
ollama pull qwen3:1.7b
```

sherpa-onnx's PyPI wheel is CPU-only. The CUDA build is a separate wheel, indexed at
<https://k2-fsa.github.io/sherpa/onnx/cuda.html> (pick your Python tag; `+cuda12.cudnn9` matches the
`nvidia-*-cu12` packages the `cuda` extra installs):

```bash
pip uninstall -y sherpa-onnx sherpa-onnx-core
pip install --no-deps "https://huggingface.co/csukuangfj2/sherpa-onnx-wheels/resolve/main/cuda/1.13.7/sherpa_onnx-1.13.7+cuda12.cudnn9-cp313-cp313-win_amd64.whl"
```

`SherpaOnnxTTS(provider="auto")` detects that build (it ships `onnxruntime_providers_cuda.dll`) and
puts the pip-installed CUDA DLL folders on PATH before loading — nothing to configure. **Use the fp32
voice on the GPU.** The `int8` Kokoro/Kitten files are dynamically quantized, and onnxruntime's CUDA
provider has no kernels for those ops, so every quantized matmul round-trips through the CPU:

| Kokoro v1.1, sherpa-onnx 1.13.7, RTX 4080 | first chunk (1.6 s / 5 s sentence) | RTF | VRAM |
|---|---|---|---|
| fp32 `model.onnx`, `provider="cuda"` | **175 ms / 91 ms** | **0.07–0.11** | +0.77 GB |
| fp32 `model.onnx`, `provider="cpu"` | 505 ms / 291 ms | 0.31–0.44 | — |
| int8 `model.int8.onnx`, `provider="cuda"` | 3 314 ms / 2 273 ms | 1.85 | +0.2 GB |
| int8 `model.int8.onnx`, `provider="cpu"` | ~1 400 ms / — | ~0.77 | — |

The fp32 model is `kokoro-multi-lang-v1_1.tar.bz2` from the sherpa-onnx `tts-models` release (325 MB
`model.onnx` + 54 MB `voices.bin`, Apache-2.0).

```python
from voicert.config import ConfigFactory
from voicert.processors.local import OllamaLLM, SherpaOnnxTTS, WhisperSTT
from voicert.utterance import UtteranceConfig, UtteranceSegmenter

runtime = ConfigFactory.build("npc", processors=lambda ctx: [
    WhisperSTT(ctx, model_size="small.en", device="cuda", compute_type="float16"),
    OllamaLLM(ctx, model="qwen3:8b"),
    SherpaOnnxTTS(ctx, model_kind="kokoro", model="model.onnx", voices="voices.bin",
                  tokens="tokens.txt", data_dir="espeak-ng-data", dict_dir="dict",
                  lexicon="lexicon-us-en.txt", provider="auto"),
])
runtime.transport.segmenter = UtteranceSegmenter(UtteranceConfig(), sample_rate=16_000)
await runtime.ctx.llm.warmup()     # during level load, not on first line
```

**Half the machine, not all of it.** The stack is sized so a game can render next to it: Whisper
`base.en` fp16, `qwen3:1.7b` and fp32 Kokoro together hold ~3.5 GB of the 4080's 16 GB, CPU pools are
capped at 4 threads (`cpu_threads` / `num_threads`), and GPU work per turn is a burst of a few hundred
milliseconds. `examples/resmon.py -- <command>` samples GPU utilisation, VRAM and CPU while a
command runs and prints the percentiles, so the "under 50 %" claim is measured rather than assumed.

**Schedule the process like a game, or the GPU idles under you.** On a hybrid Intel CPU Windows puts a
background console process on the E-cores. This pipeline is launch-bound — thousands of small CUDA
kernels per sentence — so a slow CPU side starves the GPU, the driver drops it to P8 while it is working,
and the same turn goes from ~350 ms to over a second. `examples/run_local_npc.py` therefore turns power
throttling off, raises its priority (and Ollama's) and pins itself to the P-cores (`foreground_qos()`);
a game process is foreground already, but its inference threads deserve the same treatment.

On the Unity side there is **one** `VoiceRTVoiceInput` for the player, not one per character, and the game points its `Target` at whoever is being spoken to. A component that required a `VoiceRTNpc` on the same object meant every character in earshot opened the device and streamed identical audio, and the server ran a VAD per NPC over it. It reads no keys — "Active Input Handling" differs per project — so the game calls `BeginUtterance()` / `EndUtterance()` from whatever it uses. The device's own rate (44.1 kHz here) is resampled to 16 kHz with the fractional read position carried across chunks, so a 48 kHz device does not drift a sample every few frames.

**Feedback decides the default.** With the mic always open, the VAD hears the NPC's own voice through the player's speakers and fires barge-in at itself, forever. Push-to-talk cannot do that, so it is the default; releasing the key sends `ENDPOINT`, which closes the utterance immediately instead of paying the VAD's hangover (450 ms) on every single turn. Open mic is a mode, not the baseline, and it wants headphones — `allowBargeIn` plus a higher threshold while the NPC speaks is the compromise for speakers. Do not "fix" feedback by muting the mic whenever the agent speaks: that removes barge-in, which is the point.

**Barge-in has to cut what the player is hearing, not what the server is doing.** Kokoro synthesizes about ten times faster than the words are spoken, so a nine-second reply is fully generated in under a second — the pipeline is idle while the engine still has nine seconds queued, and an interruption manager asked to cancel finds nothing in flight. The server cannot know how much has been played, but it knows what it has sent since the last cut, and flushes that.

**Serving it to a game:** `python -m voicert.game.local_server --port 8767` loads Whisper and Kokoro once and shares them across NPC sessions (`voicert.game.local_stack.LocalStack`), maps HELLO's `voice` to a Kokoro speaker, warms every stage before it opens the listener, and prints `VOICERT READY …` when a launcher may connect. If nothing answers on Ollama's port it starts `ollama serve` itself (PATH, then the per-user install location) and waits for it — a game must not ask the player for a terminal. It also sets `HF_HUB_OFFLINE=1` before the model loaders import: faster-whisper otherwise asks huggingface.co for the model's revision on every load, files or no files. Pass `--online` once, at install time, to fetch models. `examples/npc_chat.py` is the same conversation from a terminal. Use the **fp32 v1.0** Kokoro release: `kokoro-multi-lang-v1_1` is the Chinese-first model and has only three English voices.

**Who a character is** lives in a world file, not in code: `examples/worlds/harbour-town.json`
carries the shared lore and each character's role, knowledge, voice and how they turn a question away.
`voicert.game.agents` assembles that with what the character remembers about this player — one JSON file
per `(npc_id, player_id)`, written after every exchange — into one system prompt. The isolation is
structural: there is no query that could return the guard's memory to the fruit vendor.

**Choose the model by measurement.** The role restriction is the requirement a small model quietly
fails. Against the demo's own guard persona, six probes: `qwen3:1.7b` scored 3/6 (invented lore, printed
code, explained who Elon Musk is), `qwen3:4b` 2/6 (the Thinking-2507 build reads its reasoning aloud and
`think: false` does not stop it), `qwen3:8b` 6/6 at 5.6 GB. Prompt work moved 1.7b to 5/6 and no further.
Two prompt shapes did survive and are in `NpcAgent.system_prompt`: the restriction goes **last**, and it
gives the model a line it can say ("that word is gibberish to you") rather than only a prohibition.

**Speech in:** `small.en`, not `base.en`. Measured over ten spoken lines (`examples/stt_bench.py`):
4.6 % word error against 12.0 %, for 27 ms and 400 MiB more. Names are why — `base.en` heard
"Who is Elon Musk?" as "Who is a lawn musk?", and a character who mishears a name has nothing to
remember.

**Status:** executed end to end with speech, in a terminal and inside Unity. `examples/npc_voice_probe.py`
renders the player's line with Kokoro and streams it as microphone audio, which makes the whole path —
VAD, segmenter, Whisper, persona, model, sentence buffer, Kokoro, the wire — testable without a person.
Measured p50 489 ms (n=10) from the end of the player's speech to the first sample back with the
editor idle, 850-1100 ms with the game rendering; the numbers and their breakdown live in `VERSIONS.md` ("Stage 3").
