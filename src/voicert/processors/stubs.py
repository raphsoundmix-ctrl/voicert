"""Deterministic offline providers — run the full pipeline with zero API keys.

The stub convention: an ``AudioFrame.pcm`` payload starting with ``b"text:"``
carries a UTF-8 transcript ("pretend audio"). This keeps end-to-end tests
and the demo fully deterministic while exercising every real code path —
streaming partials, token pacing, cancellation points, spoken-progress
reporting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voicert.context import RuntimeContext
from voicert.frames import AudioFrame, Frame, FunctionCallFrame, TextFrame
from voicert.processors.base import LLMService, STTService, TTSService

TEXT_PREFIX = b"text:"


def make_user_audio(text: str) -> AudioFrame:
    """Helper for tests/demo: wrap a transcript as pretend user audio."""
    return AudioFrame(pcm=TEXT_PREFIX + text.encode("utf-8"), source="user")


class StubSTT(STTService):
    """Emits one interim partial (first half) then the final transcript."""

    async def transcribe(self, frame: AudioFrame) -> AsyncIterator[TextFrame]:
        if not frame.pcm.startswith(TEXT_PREFIX):
            return
        text = frame.pcm[len(TEXT_PREFIX) :].decode("utf-8")
        half = text[: max(len(text) // 2, 1)]
        yield TextFrame(text=half, role="user", final=False)
        await asyncio.sleep(0.005)
        yield TextFrame(text=text, role="user", final=True)


class StubLLM(LLMService):
    """Streams a canned reply word-by-word with awaits between tokens —
    every gap is a cancellation point, exactly like a real provider stream.

    A user message containing ``"tool"`` triggers a FunctionCallFrame
    against the first tool of the profile registry, so the tool path is
    testable offline too.
    """

    def __init__(self, ctx: RuntimeContext, reply: str | None = None, token_delay: float = 0.01) -> None:
        super().__init__(ctx)
        self.reply = reply
        self.token_delay = token_delay

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        if "tool" in last_user.lower():
            names = sorted(self.ctx.tools.names())
            if names:
                result = await self.ctx.tools.call(names[0], {})
                yield FunctionCallFrame(
                    tool_name=names[0], arguments={}, call_id="stub-1", result=result
                )
        reply = self.reply or (
            f"[{self.ctx.state.profile}] Understood: “{last_user}”. "
            "Here is a deliberately long and detailed answer, spoken slowly "
            "enough that you can barge in and cut me off mid-sentence."
        )
        for word in reply.split(" "):
            await asyncio.sleep(self.token_delay)
            yield TextFrame(text=word + " ", role="assistant", final=False)


class StubTTS(TTSService):
    """One synthetic PCM chunk per text token; chunk length scales with the
    text so 'audio duration' correlates with what a real TTS would produce."""

    def __init__(self, ctx: RuntimeContext, chunk_delay: float = 0.005) -> None:
        super().__init__(ctx)
        self.chunk_delay = chunk_delay

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        await asyncio.sleep(self.chunk_delay)
        pcm = b"\x00\x01" * (160 * max(len(text), 1))
        yield AudioFrame(pcm=pcm, source="agent", sample_rate=16_000)
