import asyncio

import pytest

from voicert.config import AgentRuntime, ConfigFactory
from voicert.frames import AudioFrame, Frame, InterruptionFrame, TextFrame
from voicert.transport import LoopbackTransport


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
async def assistant_runtime():
    runtime = ConfigFactory.build("assistant", llm_token_delay=0.02)
    await runtime.start()
    yield runtime
    await runtime.stop()
