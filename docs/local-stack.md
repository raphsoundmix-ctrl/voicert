# Talking to an NPC with a real microphone, fully offline

No cloud, no API keys. Every stage has a local counterpart, and three of the four are already wired.

```
Unity Microphone ──PCM16 16 kHz──▶ AUDIO_IN ──▶ [ VAD ] ──▶ [ UtteranceSegmenter ]
                                                   │                    │
                                    barge-in ◀─────┘        one whole utterance
                                                                        ▼
        AudioSource ◀── AUDIO_OUT ◀── [ TTS ] ◀── sentences ◀── [ local LLM ] ◀── [ Whisper ]
```

**The part that is not obvious.** A microphone hands you 20-50 ms chunks forever; Whisper wants one complete utterance. Something has to decide where an utterance begins and ends, and feeding the model individual chunks produces confident nonsense — every chunk looks like a complete short sentence to it. `voicert.utterance.UtteranceSegmenter` is that decision. It keeps a rolling **pre-roll** buffer (300 ms by default) and prepends it when the VAD fires, because a VAD needs energy before it triggers — without pre-roll the model hears "ello" instead of "hello". It also drops anything under 250 ms (coughs, chair scrapes) and force-flushes past 20 s, so a fan near the mic cannot grow the buffer until the process dies. Set `transport.segmenter` and only whole utterances reach the pipeline; leave it unset and the old chunk-per-frame behaviour is unchanged.

The second non-obvious piece is **sentence chunking before TTS**. Feeding the LLM's token stream straight into synthesis gives every word its own falling intonation. `SentenceBuffer` holds tokens until a sentence boundary, then releases the whole clause, so TTS can place stress and breath — with a character-count escape hatch for models that forget punctuation.

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
pip install -e ".[local]"          # faster-whisper + httpx + sherpa-onnx
ollama pull qwen3:1.7b
```

```python
from voicert.config import ConfigFactory
from voicert.processors.local import OllamaLLM, SherpaOnnxTTS, WhisperSTT
from voicert.utterance import UtteranceConfig, UtteranceSegmenter

runtime = ConfigFactory.build("npc", processors=[
    WhisperSTT(ctx, model_size="base.en", device="cuda", compute_type="float16"),
    OllamaLLM(ctx, model="qwen3:1.7b"),
    SherpaOnnxTTS(ctx, model="voice.onnx", tokens="tokens.txt"),
])
runtime.transport.segmenter = UtteranceSegmenter(UtteranceConfig(), sample_rate=16_000)
await runtime.ctx.llm.warmup()     # during level load, not on first line
```

On the Unity side add `VoiceRT Microphone` next to `VoiceRT NPC` — it already streams PCM16 mono 16 kHz into `AUDIO_IN`, which is exactly what the segmenter and Whisper expect.

**One gotcha worth planning for.** With the mic always open, the VAD hears the NPC's own voice through the player's speakers and fires barge-in at itself. Headphones make it disappear; otherwise use push-to-talk (gate `SendMicAudio` behind a key) or real acoustic echo cancellation. Do not "fix" it by muting the mic while the agent speaks — that removes barge-in, which is the whole point of the design.

**Status:** the segmenter and sentence buffer are covered by 14 tests and the local LLM path has been run end-to-end against a live Ollama on this machine (numbers above). `WhisperSTT` and `SherpaOnnxTTS` follow their libraries' documented APIs but have not been executed here — neither package is installed in this environment, and no voice model has been downloaded.
