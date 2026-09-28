"""One fully local NPC turn, end to end, with no microphone and no cloud.

The player's question is synthesized by one local voice, fed into the runtime as
if it were microphone audio (20 ms chunks through the VAD and the utterance
segmenter), transcribed by Whisper on the GPU, answered by a local model through
Ollama, and spoken back by a second local voice. The number that comes out is
the MVP's one metric: time from the end of the player's speech to the first NPC
audio, against the NPC profile's 300 ms budget.

    python examples/run_local_npc.py
    python examples/run_local_npc.py --tts kitten --question "Where is the smith?"
    python examples/resmon.py -- python examples/run_local_npc.py   # + GPU/CPU use

Everything runs on the local GPU when one is there (Whisper via CTranslate2,
qwen3 via Ollama, fp32 Kokoro via sherpa-onnx's CUDA build) and the whole
stack is meant to stay under half the machine: ``--threads`` caps the CPU
pools, and ``resmon.py`` reports whether the GPU stayed under 50 %.

Writes player.wav and npc.wav next to the metrics so a human can listen.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import wave
from pathlib import Path

from voicert.config import ConfigFactory
from voicert.qos import boost_ollama, foreground_qos
from voicert.frames import AudioFrame, TextFrame
from voicert.processors.local import OllamaLLM, SherpaOnnxTTS, WhisperSTT
from voicert.utterance import UtteranceConfig, UtteranceSegmenter

MODELS = Path(r"D:\VOICE RT\_shared\models\tts")
KITTEN = MODELS / "kitten-mini-en-v0_8"
KOKORO = MODELS / "kokoro-int8-multi-lang-v1_1"
KOKORO32 = MODELS / "kokoro-multi-lang-v1_1"  # fp32: the one that benefits from the GPU

logging.basicConfig(level=logging.INFO, format="%(name)s  %(message)s")
log = logging.getLogger("run_local_npc")


def tts_kwargs(kind: str) -> dict[str, object]:
    if kind == "kitten":
        return dict(model_kind="kitten", model=str(KITTEN / "model.onnx"), voices=str(KITTEN / "voices.bin"),
                    tokens=str(KITTEN / "tokens.txt"), data_dir=str(KITTEN / "espeak-ng-data"))
    if kind in ("kokoro", "kokoro32"):
        d = KOKORO32 if kind == "kokoro32" else KOKORO
        model = "model.onnx" if kind == "kokoro32" else "model.int8.onnx"
        return dict(model_kind="kokoro", model=str(d / model), voices=str(d / "voices.bin"),
                    tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data"), dict_dir=str(d / "dict"),
                    lexicon=",".join([str(d / "lexicon-us-en.txt"), str(d / "lexicon-zh.txt")]))
    raise SystemExit(f"unknown tts {kind!r}")


def resample_to_16k(pcm16: bytes, rate: int) -> bytes:
    """Linear resample of mono PCM16 to 16 kHz — good enough to stand in for a microphone."""
    import numpy as np

    if rate == 16_000:
        return pcm16
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    n_out = int(round(len(x) * 16_000 / rate))
    idx = np.linspace(0, len(x) - 1, n_out)
    y = np.interp(idx, np.arange(len(x)), x)
    return bytes(np.clip(y, -32768, 32767).astype(np.int16).tobytes())


def write_wav(path: Path, pcm16: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


async def main(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not args.no_qos:
        foreground_qos(args.pcores)

    # -- the player's voice: a different local model/speaker than the NPC --------
    # A throwaway runtime supplies the ctx the provider wants; it is never started.
    scratch = ConfigFactory.build("npc")
    # The player's line is prepared off the clock, so it stays on the CPU and leaves the GPU to the NPC.
    player_tts = SherpaOnnxTTS(scratch.ctx, speaker_id=args.player_speaker, provider="cpu",
                               num_threads=args.threads, **tts_kwargs(args.player_tts))  # type: ignore[arg-type]
    t0 = time.perf_counter()
    question_pcm = b"".join([f.pcm async for f in player_tts.synthesize(args.question)])
    question_rate = player_tts.output_sample_rate
    log.info("player question synthesized: %.2f s of audio in %.0f ms", len(question_pcm) / 2 / question_rate, (time.perf_counter() - t0) * 1000)
    question_16k = resample_to_16k(question_pcm, question_rate)
    write_wav(out / "player.wav", question_16k, 16_000)

    # -- the NPC runtime: real local providers wired into the strict npc profile ----
    stages: dict[str, WhisperSTT | OllamaLLM | SherpaOnnxTTS] = {}

    def providers(ctx):  # type: ignore[no-untyped-def]
        stages["stt"] = WhisperSTT(ctx, model_size=args.whisper, device="cuda", compute_type="float16",
                                   cpu_threads=args.threads)
        stages["llm"] = OllamaLLM(ctx, model=args.llm, num_predict=args.num_predict)
        stages["tts"] = SherpaOnnxTTS(ctx, speaker_id=args.npc_speaker, provider=args.tts_provider,
                                      num_threads=args.threads, **tts_kwargs(args.tts))  # type: ignore[arg-type]
        return list(stages.values())

    runtime = ConfigFactory.build("npc", processors=providers)
    runtime.transport.segmenter = UtteranceSegmenter(UtteranceConfig(), sample_rate=16_000)
    await runtime.start()
    # Level load: every stage pays its first-run cost here, not on the player's first line.
    for name, stage in stages.items():
        t0 = time.perf_counter()
        await stage.warmup()
        log.info("%s warm in %.0f ms", name, (time.perf_counter() - t0) * 1000)
    if not args.no_qos:
        log.info("ollama after warm-up: %s", boost_ollama())  # llama-server.exe exists only now

    # -- feed the question like a microphone: 20 ms chunks on a wall-clock schedule, then silence
    # so the VAD closes. Paced against perf_counter rather than sleep(0.02) per chunk: Windows
    # timers tick at ~15.6 ms, so naive sleeps run the "microphone" 1.5x slower than real time
    # and inflate the wall number.
    chunk = 16_000 * 2 // 50  # 640 bytes = 20 ms
    speech = [question_16k[i:i + chunk] for i in range(0, len(question_16k), chunk)]
    silence = [bytes(chunk)] * int(args.trailing_silence_ms / 20)

    # Stamp the arrival *while* the feeder is still running. Waiting for the
    # event after the loop would floor every measurement at the silence we feed
    # (600 ms by default) and hide exactly the sub-600 ms turns this bench exists
    # to show.
    first_audio_at = 0.0

    async def stamp_first_audio() -> None:
        nonlocal first_audio_at
        await runtime.transport.first_audio.wait()
        first_audio_at = time.perf_counter()

    stamper = asyncio.ensure_future(stamp_first_audio())
    start = time.perf_counter()
    speech_end_wall = start
    for n, data in enumerate(speech + silence):
        delay = start + n * 0.02 - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        await runtime.transport.feed_input(data, 16_000)
        if n == len(speech) - 1:
            # end of the last chunk's audio, not the moment it was handed over
            speech_end_wall = time.perf_counter() + len(data) / 2 / 16_000

    # -- wait for the reply -----------------------------------------------------
    try:
        await asyncio.wait_for(stamper, timeout=args.timeout)
    except asyncio.TimeoutError:
        log.error("no NPC audio within %.0fs", args.timeout)
        stamper.cancel()
        await runtime.stop()
        return 2
    first_audio_wall = (first_audio_at - speech_end_wall) * 1000

    # let the reply finish: stop when the final assistant TextFrame has arrived, or after a cap
    deadline = time.perf_counter() + args.timeout
    while time.perf_counter() < deadline:
        if any(isinstance(f, TextFrame) and f.role == "assistant" and f.final for f in runtime.transport.outbox):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)

    reply_pcm = b"".join(f.pcm for f in runtime.transport.outbox if isinstance(f, AudioFrame) and f.source == "agent")
    reply_rate = next((f.sample_rate for f in runtime.transport.outbox if isinstance(f, AudioFrame)), 16_000)
    write_wav(out / "npc.wav", reply_pcm, reply_rate)

    metrics = runtime.metrics.report(1)
    turns = [(t.role, t.text) for t in runtime.state.turns]
    await runtime.stop()

    print("\n=== fully local NPC turn ===")
    print(f"question : {args.question}")
    for role, text in turns:
        print(f"{role:>9}: {text}")
    print(f"reply    : {len(reply_pcm) / 2 / reply_rate:.2f} s of audio at {reply_rate} Hz -> {out / 'npc.wav'}")
    print(f"wall     : end of player speech -> first NPC audio = {first_audio_wall:.0f} ms "
          f"(includes the profile's VAD hangover; {args.trailing_silence_ms} ms of silence was fed)")
    print(f"providers: whisper={args.whisper} on cuda/float16, llm={args.llm}, "
          f"tts={args.tts} on {stages['tts'].provider}")
    print("metrics  :", json.dumps(metrics))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="What rooms do you have tonight?")
    ap.add_argument("--tts", default="kokoro32", choices=["kitten", "kokoro", "kokoro32"])
    ap.add_argument("--tts-provider", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--player-tts", default="kokoro", choices=["kitten", "kokoro", "kokoro32"])
    ap.add_argument("--threads", type=int, default=4, help="CPU threads for Whisper and sherpa-onnx (half-machine rule)")
    ap.add_argument("--no-qos", action="store_true", help="keep background-process scheduling (reproduces the slow state)")
    ap.add_argument("--pcores", type=int, default=16, help="pin to the first N logical CPUs (P-cores on hybrid Intel); 0 = off")
    ap.add_argument("--npc-speaker", type=int, default=0)
    ap.add_argument("--player-speaker", type=int, default=5)
    ap.add_argument("--llm", default="qwen3:1.7b")
    ap.add_argument("--num-predict", type=int, default=60)
    ap.add_argument("--whisper", default="base.en")
    ap.add_argument("--trailing-silence-ms", type=int, default=600)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out-dir", default=r"D:\VOICE RT\_scratch\local-turn")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
