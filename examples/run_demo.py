"""Offline demo — no API keys, no network. Shows the full runtime loop:

    user audio -> VAD -> STT -> state -> LLM -> TTS -> transport sink
                       └── barge-in: the user speaks mid-answer, the agent
                           is cut, and history keeps only the SPOKEN prefix.

Run:  python examples/run_demo.py [sales|assistant|npc]
"""

from __future__ import annotations

import asyncio
import logging
import sys

from voicert.config import ConfigFactory
from voicert.frames import AudioFrame

logging.basicConfig(level=logging.INFO, format="%(name)s  %(message)s")


async def main(profile: str) -> None:
    runtime = ConfigFactory.build(profile, llm_token_delay=0.03)
    await runtime.start()
    print(f"\n=== profile: {profile} | tools: {sorted(runtime.tools.names())} ===\n")

    # -- turn 1: normal exchange ----------------------------------------
    await runtime.say("Hi! Tell me what you can do.")
    await runtime.transport.first_audio.wait()
    await asyncio.sleep(1.2)  # let the answer finish

    print("--- turn 1 (no interruption) ---")
    print("history:", [(t.role, t.text[:60]) for t in runtime.state.turns])
    print("metrics:", runtime.metrics.report(1), "\n")

    # -- turn 2: barge-in mid-answer --------------------------------------
    runtime.transport.first_audio.clear()
    await runtime.say("Tell me a long story.")
    await runtime.transport.first_audio.wait()
    await asyncio.sleep(0.15)          # the agent got a few words out...
    print("--- USER BARGES IN ---")
    await runtime.interruption.interrupt()

    interrupted = [t for t in runtime.state.turns if t.interrupted]
    for turn in interrupted:
        print(f"truncated turn {turn.turn_id}: “{turn.text}” (spoken={turn.spoken_chars})")

    # -- turn 3: the pipeline survives the cut ------------------------------
    runtime.transport.first_audio.clear()
    await runtime.say("Okay, briefly: what is the plan for tomorrow?")
    await runtime.transport.first_audio.wait()
    await asyncio.sleep(0.8)
    print("\n--- turn 3 (after the barge-in) ---")
    audio_bytes = sum(
        len(f.pcm) for f in runtime.transport.outbox if isinstance(f, AudioFrame)
    )
    print(f"agent alive, total output audio: {audio_bytes} bytes")
    print("LLM context right now:")
    for message in runtime.state.llm_messages(runtime.ctx.system_prompt)[1:]:
        print(f"  {message['role']:>9}: {message['content'][:80]}")

    await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "assistant"))
