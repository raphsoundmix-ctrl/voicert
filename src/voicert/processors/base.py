"""Provider-agnostic processor contracts for the three model stages.

Adding a provider = subclass one of these and implement one generator.
Nothing in the core pipeline changes: providers speak frames, not SDKs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voicert.context import RuntimeContext
from voicert.frames import AudioFrame, Frame, InterruptionFrame, TextFrame
from voicert.pipeline import FrameProcessor


class STTService(FrameProcessor):
    """AudioFrame(user) -> TextFrame partials + one final TextFrame.

    Implementations override ``transcribe``. The base class handles frame
    routing and metrics stamping so providers stay ~50 lines.
    """

    name = "stt"

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx

    async def process_frame(self, frame: Frame) -> AsyncIterator[Frame]:
        if not isinstance(frame, AudioFrame) or frame.source != "user":
            yield frame
            return
        async for text_frame in self.transcribe(frame):
            if text_frame.final:
                turn = self.ctx.state.add_user_final(text_frame.text)
                self.ctx.metrics.turn_started(turn.turn_id)
                self.ctx.metrics.mark(turn.turn_id, "stt_final")
                yield TextFrame(
                    text=text_frame.text, role="user", final=True, turn_id=turn.turn_id
                )
            else:
                yield text_frame

    async def transcribe(self, frame: AudioFrame) -> AsyncIterator[TextFrame]:
        raise NotImplementedError
        yield  # pragma: no cover  — makes this an async generator for typing


class LLMService(FrameProcessor):
    """Final user TextFrame -> streamed assistant TextFrames (+ tool calls).

    The base class owns turn bookkeeping (state history, metrics, partial
    accumulation); providers only implement token generation over messages.
    """

    name = "llm"

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx

    async def process_frame(self, frame: Frame) -> AsyncIterator[Frame]:
        if not (isinstance(frame, TextFrame) and frame.role == "user" and frame.final):
            if not isinstance(frame, TextFrame):
                yield frame
            return
        user_turn_id = frame.turn_id
        assistant_turn = self.ctx.state.begin_assistant_turn()
        messages = self.ctx.state.llm_messages(self.ctx.system_prompt)
        try:
            async for out in self.generate(messages):
                if isinstance(out, TextFrame):
                    self.ctx.metrics.mark(user_turn_id, "llm_first_token")
                    self.ctx.state.append_assistant_text(assistant_turn.turn_id, out.text)
                    yield TextFrame(
                        text=out.text,
                        role="assistant",
                        final=False,
                        turn_id=assistant_turn.turn_id,
                    )
                else:
                    yield out
        finally:
            # Reached on normal completion AND on CancelledError from a
            # barge-in: on cancel we leave the turn open — the
            # InterruptionManager reconciles it with the spoken prefix.
            pass
        self.ctx.state.commit_assistant(assistant_turn.turn_id)
        yield TextFrame(text="", role="assistant", final=True, turn_id=assistant_turn.turn_id)

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        raise NotImplementedError
        yield  # pragma: no cover


class TTSService(FrameProcessor):
    """Assistant TextFrames -> AudioFrames(agent), with spoken-progress reporting.

    Reports ``mark_spoken`` per synthesized chunk — this is what makes the
    state manager's spoken-prefix reconciliation truthful after a barge-in.
    """

    name = "tts"

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self._spoken_in_turn: dict[int, int] = {}

    async def process_frame(self, frame: Frame) -> AsyncIterator[Frame]:
        if not (isinstance(frame, TextFrame) and frame.role == "assistant"):
            yield frame
            return
        if frame.final or not frame.text:
            if frame.final:
                self.ctx.agent_stopped_speaking()
                self._spoken_in_turn.pop(frame.turn_id, None)
                # The end-of-turn marker travels on to the sink so a
                # transport (or a game engine bridge) can close its
                # subtitle / lip-sync segment without peeking at state.
                yield frame
            return
        turn_id = frame.turn_id
        if turn_id not in self._spoken_in_turn:
            self._spoken_in_turn[turn_id] = 0
            self.ctx.agent_started_speaking(turn_id)
        # A text frame counts as spoken once its first audio chunk is out;
        # mark_spoken is a monotonic max, so repeating the same target per
        # chunk is idempotent and never overcounts multi-chunk synthesis.
        spoken_target = self._spoken_in_turn[turn_id] + len(frame.text)
        async for audio in self.synthesize(frame.text):
            # Stamp first-audio TTFB against the *user* turn that started
            # this exchange (turn ids are sequential: user, then assistant).
            self.ctx.metrics.mark(max(turn_id - 1, 1), "tts_first_audio")
            self._spoken_in_turn[turn_id] = spoken_target
            self.ctx.state.mark_spoken(turn_id, spoken_target)
            yield audio
        # The text follows its own audio downstream, so subtitles and
        # viseme generation receive the words at the moment they are voiced.
        yield frame

    async def on_interrupt(self, frame: InterruptionFrame) -> None:
        self._spoken_in_turn.clear()
        self.ctx.agent_stopped_speaking()

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        raise NotImplementedError
        yield  # pragma: no cover
