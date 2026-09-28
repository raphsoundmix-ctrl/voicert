"""Type to an NPC over the engine bridge and listen to what it says back.

The same path Unity's chat panel takes — TEXT_IN over the bridge, AUDIO_OUT and
TEXT_OUT back — but from a terminal, so the voice stack can be exercised and
timed without opening the editor::

    python -m voicert.game.local_server --port 8767          # in one shell
    python examples/npc_chat.py --voice keeper               # in another
    python examples/npc_chat.py --say "Do you have a room?"  # one shot, for CI

Each reply is written to a .wav next to the transcript, and the line printed
after it is the number that matters: the wait from pressing Enter to the first
sample of the answer.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import struct
import sys
import time
import wave
from pathlib import Path

HEADER = struct.Struct("!BI")  # type (1 B) + length (4 B big-endian)
T_HELLO, T_TEXT_IN, T_INTERRUPT = 0x01, 0x02, 0x05
T_READY, T_AUDIO_OUT, T_TEXT_OUT, T_TURN_END, T_FLUSH, T_TOOL, T_STATE, T_STT, T_ERROR = (
    0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x8F,
)
NAMES = {T_READY: "READY", T_AUDIO_OUT: "AUDIO_OUT", T_TEXT_OUT: "TEXT_OUT",
         T_TURN_END: "TURN_END", T_FLUSH: "FLUSH", T_TOOL: "TOOL",
         T_STATE: "STATE", T_STT: "STT", T_ERROR: "ERROR"}


class Turn:
    """One question and the answer it produced."""

    def __init__(self, text: str) -> None:
        self.question = text
        self.asked_at = time.perf_counter()
        self.first_audio_ms: float | None = None
        self.subtitle = ""
        self.pcm = bytearray()
        self.metrics: dict[str, object] = {}
        self.done = asyncio.Event()


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        head = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    ftype, length = HEADER.unpack(head)
    return ftype, (await reader.readexactly(length) if length else b"")


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def pump(reader: asyncio.StreamReader, state: dict[str, object]) -> None:
    """Server -> screen. The only place that touches the socket for reading."""
    while True:
        frame = await read_frame(reader)
        if frame is None:
            print("\n[bridge closed the connection]")
            return
        ftype, payload = frame
        turn: Turn | None = state.get("turn")  # type: ignore[assignment]
        if ftype == T_READY:
            ready = json.loads(payload.decode("utf-8") or "{}")
            state["rate"] = int(ready.get("sample_rate", 16_000))
            print(f"[ready] {ready.get('npc_id')} at {state['rate']} Hz, {ready.get('format')}")
        elif ftype == T_AUDIO_OUT and turn is not None:
            if turn.first_audio_ms is None:
                turn.first_audio_ms = (time.perf_counter() - turn.asked_at) * 1000
            turn.pcm += payload
        elif ftype == T_TEXT_OUT and turn is not None:
            # TEXT_OUT carries the chunk that was just voiced, not the whole line.
            turn.subtitle += json.loads(payload.decode("utf-8") or "{}").get("text", "")
        elif ftype == T_TURN_END and turn is not None:
            turn.metrics = json.loads(payload.decode("utf-8") or "{}").get("metrics", {})
            turn.done.set()
        elif ftype == T_STATE:
            # What the session is doing. Typed turns go straight to processing,
            # so this only becomes interesting once a microphone is attached.
            print(f"  [{json.loads(payload)['state']}]")
        elif ftype == T_STT:
            obj = json.loads(payload)
            print(f"  heard{'' if obj['final'] else ' (so far)'}: {obj['text']}")
        elif ftype == T_FLUSH:
            print("[flush] barge-in: drop queued audio")
        elif ftype == T_ERROR:
            print(f"[error] {payload.decode('utf-8', 'replace')}")
            if turn is not None:
                turn.done.set()
        else:
            print(f"[{NAMES.get(ftype, hex(ftype))}] {len(payload)} B")


async def ask(writer: asyncio.StreamWriter, state: dict[str, object], text: str,
              out_dir: Path, index: int, timeout: float) -> Turn:
    turn = Turn(text)
    state["turn"] = turn
    writer.write(HEADER.pack(T_TEXT_IN, len(text.encode())) + text.encode())
    await writer.drain()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(turn.done.wait(), timeout=timeout)
    rate = int(state.get("rate", 16_000))  # type: ignore[arg-type]
    if turn.pcm:
        path = out_dir / f"reply-{index:02d}.wav"
        write_wav(path, bytes(turn.pcm), rate)
        print(f"npc  : {turn.subtitle}")
        print(f"       {len(turn.pcm) / 2 / rate:.2f} s of audio -> {path}")
    else:
        print("npc  : (silence — the bridge sent no audio)")
    first = f"{turn.first_audio_ms:.0f} ms" if turn.first_audio_ms is not None else "never"
    print(f"       first audio after {first}"
          + (f"   metrics {json.dumps(turn.metrics)}" if turn.metrics else ""))
    return turn


async def main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        reader, writer = await asyncio.open_connection(args.host, args.port)
    except OSError as exc:
        print(f"cannot reach the bridge at {args.host}:{args.port} — {exc}\n"
              f"start it with: python -m voicert.game.local_server --port {args.port}")
        return 2
    hello = json.dumps({"proto": 1, "npc_id": args.npc_id, "character": args.character,
                        "lore_scope": args.lore, "voice": args.voice}).encode("utf-8")
    writer.write(HEADER.pack(T_HELLO, len(hello)) + hello)
    await writer.drain()

    state: dict[str, object] = {"rate": 16_000, "turn": None}
    reader_task = asyncio.ensure_future(pump(reader, state))
    await asyncio.sleep(0.2)  # let READY land before the first prompt

    index = 0
    try:
        if args.say:
            for line in args.say:
                index += 1
                print(f"\nyou  : {line}")
                await ask(writer, state, line, out_dir, index, args.timeout)
        else:
            print("Type a line and press Enter. Empty line or Ctrl-C quits.\n")
            loop = asyncio.get_running_loop()
            while True:
                line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
                if not line:
                    break
                index += 1
                await ask(writer, state, line, out_dir, index, args.timeout)
    except KeyboardInterrupt:
        pass
    finally:
        reader_task.cancel()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--npc-id", default="keeper")
    ap.add_argument("--character", default="Yorick, the keeper of the Anchor tavern")
    ap.add_argument("--lore", default="the harbour town of Velenhart, its guilds, goods and rumours")
    ap.add_argument("--voice", default="keeper", help="role, Kokoro voice name, or speaker id")
    ap.add_argument("--say", action="append", help="one-shot line (repeatable); omit for a prompt")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out-dir", default=r"D:\VOICE RT\_scratch\npc-chat")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
