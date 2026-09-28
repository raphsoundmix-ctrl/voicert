import asyncio
from dataclasses import replace

import pytest

from voicert.config import PROFILES, AgentRuntime, ConfigFactory, ProfileConfig
from voicert.frames import AudioFrame, Frame, InterruptionFrame, TextFrame
from voicert.interruption import InterruptionPolicy
from voicert.state import ContextPolicy
from voicert.transport import LoopbackTransport

#: The NPC profile with two knobs turned, to cover the paths the stock NPC
#: never takes: a 120 ms VAD gate (so a short back-channel can be told apart
#: from a real interruption) and KEEP_ANNOTATED (the cut reply stays in the
#: model's context, marked as interrupted). The stock NPC — 0 ms and DROP —
#: has its own test in test_interruption.py.
ANNOTATED_NPC: ProfileConfig = replace(
    PROFILES["npc"],
    name="npc-annotated",
    interruption=InterruptionPolicy(min_speech_ms=120),
    context_policy=ContextPolicy.KEEP_ANNOTATED,
)


def loopback(runtime: AgentRuntime) -> LoopbackTransport:
    assert isinstance(runtime.transport, LoopbackTransport)
    return runtime.transport


async def wait_for_first_audio(runtime: AgentRuntime, timeout: float = 3.0) -> None:
    await asyncio.wait_for(loopback(runtime).first_audio.wait(), timeout)


async def wait_until_quiet(runtime: AgentRuntime, quiet_for: float = 0.08, timeout: float = 5.0) -> None:
    """Wait until the sink stops receiving new frames (turn settled)."""

    box = loopback(runtime).outbox

    async def _settle() -> None:
        seen = -1
        while True:
            if len(box) == seen:
                return
            seen = len(box)
            await asyncio.sleep(quiet_for)

    await asyncio.wait_for(_settle(), timeout)


def audio_frames(runtime: AgentRuntime) -> list[AudioFrame]:
    return [f for f in loopback(runtime).outbox if isinstance(f, AudioFrame)]


def interruption_frames(runtime: AgentRuntime) -> list[InterruptionFrame]:
    return [f for f in loopback(runtime).outbox if isinstance(f, InterruptionFrame)]


def text_frames(runtime: AgentRuntime) -> list[TextFrame]:
    return [f for f in loopback(runtime).outbox if isinstance(f, TextFrame)]


def sink_frames(runtime: AgentRuntime) -> list[Frame]:
    return loopback(runtime).outbox


@pytest.fixture
async def annotated_runtime():
    runtime = ConfigFactory.build(ANNOTATED_NPC, llm_token_delay=0.02)
    await runtime.start()
    yield runtime
    await runtime.stop()
