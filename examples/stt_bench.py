"""base.en or small.en for the microphone path — measured, not assumed.

The brief says not to swap the speech model without numbers, and there is a
concrete reason to look: on the live path ``Who is Elon Musk?`` came back as
"Who is a lawn musk?". Names are exactly where a small model gives out, and an
NPC that mishears the player's name forgets it.

Lines are rendered with Kokoro — the same voice the probe uses as the player —
and transcribed by each model in turn. Reported per model: word error rate over
all lines, whether each expected name survived, the wall time per utterance, and
the VRAM the model holds.

    python examples/stt_bench.py
    python examples/stt_bench.py --models tiny.en base.en small.en
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import re
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

CACHE = Path(__file__).resolve().parent / ".voice-cache"
DEFAULT_TTS = Path(r"D:\VOICE RT\_shared\models\tts\kokoro-multi-lang-v1_0")
PLAYER_SPEAKER = 2   # af_bella, the probe's player voice

#: (spoken line, names that must survive). Half of these are the ordinary
#: business of the demo; half are the case that actually breaks.
LINES: list[tuple[str, tuple[str, ...]]] = [
    ("Hey, can I enter?", ()),
    ("What is my name?", ()),
    ("Do you have any apples?", ()),
    ("Why can't I go in?", ()),
    ("My name is Alex, by the way.", ("Alex",)),
    ("You can call me Nella.", ("Nella",)),
    ("I am looking for Merrick, the night guard.", ("Merrick",)),
    ("Has a dockhand called Bly come past tonight?", ("Bly",)),
    ("Is this place really called Kettleport?", ("Kettleport",)),
    ("Tell me about the Harbour Office.", ("Harbour",)),
]

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein over words, divided by the reference length."""
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        row = [i]
        for j, h in enumerate(hyp, 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = row
    return prev[-1] / len(ref)


def vram_mib() -> float:
    out = subprocess.run(
        ["nvidia-smi", "--id=0", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip().splitlines()[0])


def render(text: str, tts_dir: Path) -> bytes:
    """The line as 16 kHz PCM16, cached — the same cache the voice probe fills."""
    CACHE.mkdir(exist_ok=True)
    key = hashlib.sha1(f"{text}|{PLAYER_SPEAKER}|{tts_dir.name}".encode()).hexdigest()[:16]
    path = CACHE / f"{key}.wav"
    if path.is_file():
        with wave.open(str(path), "rb") as wav:
            return wav.readframes(wav.getnframes())

    import numpy as np
    import sherpa_onnx

    from voicert.processors.local import enable_nvidia_dlls

    enable_nvidia_dlls()
    lexicons = [p for p in ("lexicon-us-en.txt", "lexicon-zh.txt") if (tts_dir / p).is_file()]
    tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(tts_dir / "model.onnx"), voices=str(tts_dir / "voices.bin"),
                tokens=str(tts_dir / "tokens.txt"), data_dir=str(tts_dir / "espeak-ng-data"),
                dict_dir=str(tts_dir / "dict") if (tts_dir / "dict").is_dir() else "",
                lexicon=",".join(str(tts_dir / p) for p in lexicons),
            ),
            num_threads=2, provider="cpu",
        )))
    audio = tts.generate(text, sid=PLAYER_SPEAKER, speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)
    if audio.sample_rate != 16_000:
        n = int(len(samples) * 16_000 / audio.sample_rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)),
                            samples).astype(np.float32)
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(pcm)
    return pcm


def bench(size: str, clips: list[tuple[str, tuple[str, ...], bytes]], args: argparse.Namespace) -> None:
    import numpy as np
    from faster_whisper import WhisperModel

    from voicert.processors.local import enable_nvidia_dlls

    enable_nvidia_dlls()
    before = vram_mib()
    model = WhisperModel(size, device=args.device, compute_type=args.compute_type, cpu_threads=4)

    def run(pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()

    run(clips[0][2])                       # first call pays the CUDA kernel selection
    loaded = vram_mib() - before

    rates, times, missed = [], [], []
    print(f"\n{size}")
    for text, names, pcm in clips:
        started = time.monotonic()
        heard = run(pcm)
        elapsed = (time.monotonic() - started) * 1000
        rate = error_rate(text, heard)
        rates.append(rate)
        times.append(elapsed)
        lost = [n for n in names if n.lower() not in heard.lower()]
        missed.extend(lost)
        flag = "  " if not lost and rate == 0 else ("!!" if lost else " ~")
        print(f"  {flag} {elapsed:5.0f} ms  wer {rate:4.0%}  {heard}")
        if lost:
            print(f"       lost: {', '.join(lost)}   (said: {text})")
    print(f"  == {size}: wer {statistics.mean(rates):.1%}, "
          f"median {statistics.median(times):.0f} ms, worst {max(times):.0f} ms, "
          f"names lost {len(missed)}/{sum(len(n) for _, n, _ in clips)}, "
          f"+{loaded:.0f} MiB VRAM")

    del model
    gc.collect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", default=["base.en", "small.en"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--tts-dir", default=str(DEFAULT_TTS))
    args = ap.parse_args()

    tts_dir = Path(args.tts_dir)
    clips = [(text, names, render(text, tts_dir)) for text, names in LINES]
    total_s = sum(len(pcm) for _, _, pcm in clips) / 2 / 16_000
    print(f"{len(clips)} lines, {total_s:.1f} s of speech, "
          f"{sum(len(n) for _, n, _ in clips)} names to keep")
    for size in args.models:
        bench(size, clips, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
