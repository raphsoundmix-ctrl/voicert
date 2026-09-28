"""Talk to an NPC without a microphone, and measure what the player would wait.

The hard part of testing a voice pipeline is that the input is a person. This
renders the player's line with the *same* Kokoro model the NPC speaks with — a
different voice, so it sounds like someone else — and streams it into the bridge
in 20 ms packets, exactly as a microphone would. Everything downstream is the
real path: the server's VAD finds the utterance, Whisper transcribes it, the
model answers in character, Kokoro speaks, and the reply comes back as PCM.

    python examples/npc_voice_probe.py --npc guard "Hey, can I enter?"
    python examples/npc_voice_probe.py --npc guard --ptt "My name is Alex."
    python examples/npc_voice_probe.py --script tests/dialogue/memory.txt

What it reports, per turn:

  spoken      how long the player's line took to say (you cannot beat this)
  endpoint    wall time from the last packet sent to the first NPC sample back —
              what the player actually experiences as the pause
  stt/llm/tts the server's own marks, all measured from the VAD endpoint

``--ptt`` sends ENDPOINT the moment the line finishes instead of trailing
silence, which is what a push-to-talk key does; the difference between the two
is the VAD's hangover, paid on every open-mic turn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import struct
import sys
import time
import wave
from pathlib import Path

HEADER = struct.Struct("!BI")
T_HELLO, T_TEXT_IN, T_AUDIO_IN, T_EVENT, T_INTERRUPT, T_LOD, T_ENDPOINT = range(0x01, 0x08)
T_READY, T_AUDIO_OUT, T_TEXT_OUT, T_TURN_END, T_FLUSH, T_TOOL, T_STATE, T_STT = (
    0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88,
)
T_ERROR = 0x8F

RATE = 16_000
CHUNK_MS = 20
CACHE = Path(__file__).resolve().parent / ".voice-cache"

#: A different voice from any NPC's, so the transcript is not the model hearing
#: itself. af_bella is the clearest of the female voices at this size.
PLAYER_VOICE = "af_bella"


# ---------------------------------------------------------------- rendering


def render_line(text: str, tts_dir: Path, speaker: int) -> bytes:
    """The player's line as 16 kHz PCM16, cached on disk by text and voice."""
    CACHE.mkdir(exist_ok=True)
    key = hashlib.sha1(f"{text}|{speaker}|{tts_dir.name}".encode()).hexdigest()[:16]
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
                model=str(tts_dir / "model.onnx"),
                voices=str(tts_dir / "voices.bin"),
                tokens=str(tts_dir / "tokens.txt"),
                data_dir=str(tts_dir / "espeak-ng-data"),
                dict_dir=str(tts_dir / "dict") if (tts_dir / "dict").is_dir() else "",
                lexicon=",".join(str(tts_dir / p) for p in lexicons),
            ),
            num_threads=2, provider="cpu",   # a one-off render must not fight the server for the GPU
        )))
    audio = tts.generate(text, sid=speaker, speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)
    # Kokoro speaks at 24 kHz; a microphone hands the bridge 16 kHz.
    if audio.sample_rate != RATE:
        n = int(len(samples) * RATE / audio.sample_rate)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples
        ).astype(np.float32)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return pcm


# ------------------------------------------------------------------- client


class Probe:
    """One connection, driven turn by turn."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.sample_rate = 16_000
        self.states: list[str] = []
        self.heard: list[str] = []
        self.reply = ""
        self.audio_bytes = 0
        self.first_audio: float | None = None
        self.metrics: dict = {}
        self.error: str | None = None
        self.flushes = 0
        self.flush_at: float | None = None
        self.last_audio: float | None = None
        self._turn_end = asyncio.Event()
        self._speaking = asyncio.Event()

    async def send(self, ftype: int, payload: bytes = b"") -> None:
        self.writer.write(HEADER.pack(ftype, len(payload)) + payload)
        await self.writer.drain()

    async def hello(self, npc_id: str, voice: str, player: str) -> None:
        await self.send(T_HELLO, json.dumps({
            "proto": 1, "npc_id": npc_id, "voice": voice, "player_id": player,
        }).encode())

    async def pump(self) -> None:
        """Read frames until the socket closes."""
        try:
            while True:
                head = await self.reader.readexactly(HEADER.size)
                ftype, length = HEADER.unpack(head)
                payload = await self.reader.readexactly(length) if length else b""
                self._on_frame(ftype, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def _on_frame(self, ftype: int, payload: bytes) -> None:
        if ftype == T_AUDIO_OUT:
            now = time.monotonic()
            if self.first_audio is None:
                self.first_audio = now
                self._speaking.set()
            self.last_audio = now
            self.audio_bytes += len(payload)
        elif ftype == T_READY:
            self.sample_rate = json.loads(payload)["sample_rate"]
        elif ftype == T_TEXT_OUT:
            self.reply += json.loads(payload)["text"]
        elif ftype == T_STATE:
            self.states.append(json.loads(payload)["state"])
        elif ftype == T_STT:
            obj = json.loads(payload)
            self.heard.append(("" if obj["final"] else "~") + obj["text"])
        elif ftype == T_FLUSH:
            self.flushes += 1
            self.flush_at = time.monotonic()
        elif ftype == T_TURN_END:
            self.metrics = json.loads(payload).get("metrics") or {}
            self._turn_end.set()
        elif ftype == T_ERROR:
            self.error = json.loads(payload)["message"]
            self._turn_end.set()

    def begin_turn(self) -> None:
        self.reply = ""
        self.audio_bytes = 0
        self.first_audio = None
        self.metrics = {}
        self.states.clear()
        self.heard.clear()
        self.error = None
        self.flushes = 0
        self.flush_at = None
        self.last_audio = None
        self._turn_end.clear()
        self._speaking.clear()

    async def speak(self, pcm: bytes, *, ptt: bool, tail_ms: int = 900) -> float:
        """Stream a line at real-time pace. Returns the instant the player stopped."""
        step = RATE * CHUNK_MS // 1000 * 2
        started = time.monotonic()
        for offset in range(0, len(pcm), step):
            await self.send(T_AUDIO_IN, pcm[offset : offset + step])
            # Pace it: a VAD reading a whole utterance in one burst is not the
            # thing under test, and neither is a server that never has to wait.
            target = started + (offset + step) / 2 / RATE
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        stopped = time.monotonic()
        if ptt:
            await self.send(T_ENDPOINT)
        else:
            silence = b"\0" * step
            for _ in range(tail_ms // CHUNK_MS):
                await self.send(T_AUDIO_IN, silence)
                await asyncio.sleep(CHUNK_MS / 1000)
        return stopped

    async def wait_turn(self, timeout: float = 60.0) -> None:
        await asyncio.wait_for(self._turn_end.wait(), timeout)

    async def wait_speaking(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._speaking.wait(), timeout)


# --------------------------------------------------------------------- main


async def barge_in(probe: "Probe", opener: bytes, cut: bytes, after_ms: int) -> None:
    """Ask something long, then talk over the answer.

    What is being checked: the audio stops (no samples after the FLUSH), the
    FLUSH comes back promptly, and the character survives — the session keeps its
    identity and answers the new question instead of finishing the old one.
    """
    probe.begin_turn()
    await probe.speak(opener, ptt=True)
    await probe.wait_speaking()
    await asyncio.sleep(after_ms / 1000)

    interrupted_line = probe.reply.strip()
    cut_at = time.monotonic()
    probe.states.clear()
    probe.heard.clear()
    probe.reply = ""
    probe.flushes = 0
    probe.flush_at = None
    probe.metrics = {}
    probe.first_audio = None
    probe._turn_end.clear()      # the interrupted turn already set it
    await probe.speak(cut, ptt=True)
    flushed = probe.flush_at
    audio_after_flush = 0
    if flushed is not None:
        probe.audio_after_flush_mark = True
    print(f"      cut into: {interrupted_line[:90]}...")

    print(f"\n  interrupted after {after_ms} ms of the answer")
    print(f"      FLUSH after {(flushed - cut_at) * 1000:.0f} ms" if flushed
          else "      NO FLUSH — the engine would keep playing the old line")
    try:
        await probe.wait_turn()
    except asyncio.TimeoutError:
        print("      ! the interrupted turn never ended")
        return
    heard = [h for h in probe.heard if not h.startswith("~")]
    print(f"      heard instead: {heard[-1] if heard else '(nothing)'!r}")
    print(f"      new answer: {probe.reply.strip()}")
    print(f"      states: {' -> '.join(probe.states)}")


async def run(args: argparse.Namespace) -> int:
    tts_dir = Path(args.tts_dir)
    lines = args.lines or []
    if args.script:
        lines = [ln.strip() for ln in Path(args.script).read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")]
    if not lines:
        print("nothing to say; pass lines or --script", file=sys.stderr)
        return 2

    from voicert.game.local_stack import KOKORO_V1_0_VOICES

    speaker = KOKORO_V1_0_VOICES.get(PLAYER_VOICE, 2)
    rendered = [(text, render_line(text, tts_dir, speaker)) for text in lines]

    reader, writer = await asyncio.open_connection(args.host, args.port)
    probe = Probe(reader, writer)
    pump = asyncio.create_task(probe.pump())
    await probe.hello(args.npc, args.voice, args.player)
    await asyncio.sleep(0.4)   # let READY land

    print(f"\n{'=' * 78}\n{args.npc}  ({'push-to-talk' if args.ptt else 'open mic'})\n{'=' * 78}")
    if args.barge_in:
        if len(rendered) < 2:
            print("--barge-in needs two lines: what to ask, and what to cut in with")
            return 2
        await barge_in(probe, rendered[0][1], rendered[1][1], args.barge_in)
        writer.close()
        pump.cancel()
        return 0
    waits: list[float] = []
    for text, pcm in rendered:
        spoken_ms = len(pcm) / 2 / RATE * 1000
        probe.begin_turn()
        stopped = await probe.speak(pcm, ptt=args.ptt)
        try:
            await probe.wait_turn()
        except asyncio.TimeoutError:
            print(f"  ! no answer to {text!r} within 60 s")
            continue
        if probe.error:
            print(f"  ! {probe.error}")
            continue
        wait_ms = (probe.first_audio - stopped) * 1000 if probe.first_audio else float("nan")
        waits.append(wait_ms)
        audio_ms = probe.audio_bytes / 2 / probe.sample_rate * 1000
        marks = " ".join(
            f"{k.replace('_ms', '')}={probe.metrics[k]:.0f}"
            for k in ("stt_final", "llm_first_token", "llm_first_sentence", "tts_first_audio")
            if probe.metrics.get(k) is not None
        )
        final_heard = [h for h in probe.heard if not h.startswith("~")]
        partials = [h for h in probe.heard if h.startswith("~")]
        print(f"\n  You ({spoken_ms:.0f} ms): {text}")
        if partials:
            print(f"      heard while speaking: {partials[-1][1:]!r} ({len(partials)} preview(s))")
        print(f"      transcribed: {final_heard[-1] if final_heard else '(nothing)'!r}")
        print(f"  {args.npc}: {probe.reply.strip()}")
        print(f"      wait {wait_ms:.0f} ms · voice {audio_ms:.0f} ms · {marks}")
        print(f"      states: {' -> '.join(probe.states)}")

    writer.close()
    pump.cancel()
    if waits:
        ordered = sorted(waits)
        print(f"\n  wait after speaking: p50 {ordered[len(ordered) // 2]:.0f} ms, "
              f"min {ordered[0]:.0f}, max {ordered[-1]:.0f}, n={len(ordered)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lines", nargs="*", help="what the player says, one turn per argument")
    ap.add_argument("--script", help="file with one line per turn")
    ap.add_argument("--npc", default="guard")
    ap.add_argument("--voice", default="default", help="only used when the server has no world file")
    ap.add_argument("--player", default="player", help="whose memory this conversation belongs to")
    ap.add_argument("--ptt", action="store_true", help="end each line with ENDPOINT, not silence")
    ap.add_argument("--barge-in", type=int, metavar="MS", default=0,
                    help="ask the first line, then cut in with the second after this many "
                         "milliseconds of the answer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--tts-dir", default=r"D:\VOICE RT\_shared\models\tts\kokoro-multi-lang-v1_0")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
