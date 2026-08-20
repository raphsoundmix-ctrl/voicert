"""InterruptionManager — LiveKit-inspired barge-in orchestration.

Cancellation mechanism — and why
--------------------------------
We use **cooperative ``asyncio.Task.cancel()``** on the per-frame child
tasks the Pipeline spawns, rather than a hand-rolled cancel-token flag:

* Every provider call is an awaitable in a single event loop, so
  ``CancelledError`` raised at the next await point propagates through the
  async generators (``process_frame``), closing them via the normal
  ``aclose()`` machinery — buffers unwind through ``finally`` blocks
  instead of leaking.
* A cancel-token would require every provider adapter to poll the flag
  between chunks; forgetting one poll silently breaks barge-in. Task
  cancellation is enforced by the runtime at *every* await, so a provider
  cannot opt out by accident.
* The cost of this choice: state mutations that must survive cancellation
  are done *after* the cancel completes, from this manager (turn
  reconciliation below) — never from inside the cancelled task.

Sequencing on user speech during agent playback::

    VAD speech-start
      └─ policy gate (min_speech_ms per profile — "угу" must not kill a pitch)
          └─ pipeline.interrupt()      # cancel work -> drain queues -> notify
              └─ state.interrupt_assistant(turn)   # keep only the SPOKEN prefix
                  └─ metrics.turn_interrupted(turn)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from voicert.frames import InterruptionReason
from voicert.metrics import TTFBTracker
from voicert.pipeline import Pipeline
from voicert.state import StateContextManager

logger = logging.getLogger("voicert.interruption")


@dataclass(frozen=True, slots=True)
class InterruptionPolicy:
    """Per-profile barge-in aggressiveness.

    ``min_speech_ms`` — how long the user must be speaking before we cut
    the agent. Sales tolerates back-channel ("угу", "ага") without
    dropping the pitch; NPC cuts instantly for game feel.
    """

    min_speech_ms: int = 100
    allow_barge_in: bool = True


class InterruptionManager:
    def __init__(
        self,
        pipeline: Pipeline,
        state: StateContextManager,
        policy: InterruptionPolicy,
        metrics: TTFBTracker | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.state = state
        self.policy = policy
        self.metrics = metrics
        self._speaking_turn_id: int | None = None
        self._gate_task: asyncio.Task[None] | None = None

    # -- agent playback tracking (called by TTS via RuntimeContext) -----

    def agent_started_speaking(self, turn_id: int) -> None:
        self._speaking_turn_id = turn_id

    def agent_stopped_speaking(self) -> None:
        self._speaking_turn_id = None

    @property
    def agent_is_speaking(self) -> bool:
        return self._speaking_turn_id is not None

    # -- VAD event entrypoints -------------------------------------------

    def on_user_speech_start(self) -> None:
        """Transport/VAD callback. Synchronous by design: VAD callbacks come
        from the audio path and must never block on pipeline machinery.

        The gate arms not only while TTS is audibly speaking but also during
        the "thinking gap" — LLM generation before the first audio chunk
        (detected via an open assistant turn). Otherwise a user speaking
        during that gap would be ignored and both replies would collide.
        """
        if not self.policy.allow_barge_in:
            return
        if not self.agent_is_speaking and self.state.current_assistant_turn() is None:
            return  # agent fully idle — nothing to cut
        if self._gate_task is not None and not self._gate_task.done():
            return  # gate already armed for this speech burst
        self._gate_task = asyncio.get_running_loop().create_task(self._gated_interrupt())

    def on_user_speech_end(self) -> None:
        """Speech burst ended before the gate elapsed -> back-channel, not
        a barge-in. Disarm."""
        if self._gate_task is not None and not self._gate_task.done():
            self._gate_task.cancel()
            self._gate_task = None

    async def _gated_interrupt(self) -> None:
        if self.policy.min_speech_ms > 0:
            await asyncio.sleep(self.policy.min_speech_ms / 1000.0)
        await self.interrupt(InterruptionReason.USER_BARGE_IN)

    # -- the cut ---------------------------------------------------------

    async def interrupt(self, reason: InterruptionReason = InterruptionReason.USER_BARGE_IN) -> bool:
        """Execute a barge-in. Idempotent under rapid double calls: the
        pipeline coalesces concurrent interrupts, and turn reconciliation
        only runs for the call that actually performed the cut."""
        turn_id = self._speaking_turn_id
        if turn_id is None:
            open_turn = self.state.current_assistant_turn()
            if open_turn is None:
                return False
            turn_id = open_turn.turn_id
        performed = await self.pipeline.interrupt(reason=reason, turn_id=turn_id)
        if not performed:
            return False
        turn = self.state.interrupt_assistant(turn_id)
        self._speaking_turn_id = None
        if self.metrics is not None:
            self.metrics.turn_interrupted(turn_id)
        logger.info(
            "barge-in: turn %d cut, spoken %d/%d chars kept",
            turn_id,
            turn.spoken_chars,
            len(turn.text),
        )
        return True
