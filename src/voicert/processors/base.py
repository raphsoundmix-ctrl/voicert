"""Provider-agnostic processor contracts for the three model stages.

Adding a provider = subclass one of these and implement one generator.
Nothing in the core pipeline changes: providers speak frames, not SDKs.
"""

from __future__ import annotations

import asyncio
import time
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
        # The clock for this turn starts when the utterance reaches us — the
        # VAD endpoint — so ``stt_final`` is the real transcription latency and
        # ``tts_first_audio`` is what the player actually waits.
        arrived = time.monotonic()
        async for text_frame in self.transcribe(frame):
            if text_frame.final:
                self.ctx.notify_user_text(text_frame.text, True)
                turn = self.ctx.state.add_user_final(text_frame.text)
                self.ctx.metrics.turn_started(turn.turn_id, started=arrived)
                self.ctx.metrics.mark(turn.turn_id, "stt_final")
                yield TextFrame(
                    text=text_frame.text, role="user", final=True, turn_id=turn.turn_id
                )
            else:
                self.ctx.notify_user_text(text_frame.text, False)
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
        #: The turn being answered, for providers that report their first token
        #: before they yield their first frame (see ``note_first_token``).
        self._user_turn_id: int | None = None

    def note_first_token(self) -> None:
        """A provider that buffers tokens into clauses calls this on the first token.

        ``llm_first_token`` must stay the time to the model's first *word*: a
        provider that holds tokens back until a sentence is complete would
        otherwise report the clause latency under the token's name, and the
        budget check would blame the model for the buffering.
        """
        if self._user_turn_id is not None:
            self.ctx.metrics.mark(self._user_turn_id, "llm_first_token")

    async def process_frame(self, frame: Frame) -> AsyncIterator[Frame]:
        if not (isinstance(frame, TextFrame) and frame.role == "user" and frame.final):
            if not isinstance(frame, TextFrame):
                yield frame
            return
        user_turn_id = frame.turn_id
        self._user_turn_id = user_turn_id
        assistant_turn = self.ctx.state.begin_assistant_turn()
        messages = self.ctx.state.llm_messages(self.ctx.system_prompt)
        try:
            async for out in self.generate(messages):
                if isinstance(out, TextFrame):
                    # A provider that buffers into clauses has already marked the
                    # token; this marks the clause. For one that does not buffer,
                    # both land here and the gap is zero, which is also true.
                    self.ctx.metrics.mark(user_turn_id, "llm_first_token")
                    self.ctx.metrics.mark(user_turn_id, "llm_first_sentence")
                    self.ctx.state.append_assistant_text(assistant_turn.turn_id, out.text)
                    yield TextFrame(
                        text=out.text,
                        role="assistant",
                        final=False,
                        turn_id=assistant_turn.turn_id,
                        reply_to=user_turn_id,
                    )
                else:
                    yield out
        except asyncio.CancelledError:
            # A barge-in. Leave the turn open on purpose: the InterruptionManager
            # reconciles it against the prefix that was actually spoken.
            raise
        except Exception:
            # Any other failure ends this turn for good, so close it here. An
            # open turn is not just untidy: everything downstream that walks the
            # history in order stops at the first one that is not final, so a
            # single provider timeout would silently end memory persistence for
            # the rest of the session while the conversation carried on.
            self.ctx.state.commit_assistant(assistant_turn.turn_id)
            raise
        finally:
            self._user_turn_id = None
        self.ctx.state.commit_assistant(assistant_turn.turn_id)
        yield TextFrame(text="", role="assistant", final=True, turn_id=assistant_turn.turn_id,
                        reply_to=user_turn_id)

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        raise NotImplementedError
        yield  # pragma: no cover


class TTSService(FrameProcessor):
    """Assistant TextFrames -> AudioFrames(agent), with spoken-progress reporting.

    Reports ``mark_spoken`` per synthesized chunk — this is what makes the
    state manager's spoken-prefix reconciliation truthful after a barge-in.
    """

    name = "tts"

    #: Sample rate of the PCM this service emits. Transports announce it to the
    #: client before the first frame (the engine bridge puts it in READY), so a
    #: provider that synthesizes at 24 kHz must set it or the engine plays its
    #: voice a fifth too low. The stubs and 16 kHz voices leave the default.
    output_sample_rate: int = 16_000

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
            # Stamp first-audio TTFB against the user turn that started this
            # exchange. The frame carries it; the id-minus-one fallback is only
            # for a provider that does not set reply_to.
            self.ctx.metrics.mark(frame.reply_to or max(turn_id - 1, 1), "tts_first_audio")
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
