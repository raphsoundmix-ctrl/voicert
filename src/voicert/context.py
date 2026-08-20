"""RuntimeContext — shared services injected into every processor.

One context object per agent session. Processors receive it at construction
so STT can stamp metrics, LLM can read history and the tool registry, and
TTS can report spoken progress for barge-in reconciliation — without any
processor importing another processor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from voicert.metrics import TTFBTracker
from voicert.state import StateContextManager
from voicert.tools import ToolRegistry

if TYPE_CHECKING:
    from voicert.interruption import InterruptionManager


@dataclass
class RuntimeContext:
    state: StateContextManager
    metrics: TTFBTracker
    tools: ToolRegistry
    system_prompt: str
    #: Wired by ConfigFactory after the InterruptionManager exists.
    interruption: "InterruptionManager | None" = field(default=None)

    def agent_started_speaking(self, turn_id: int) -> None:
        if self.interruption is not None:
            self.interruption.agent_started_speaking(turn_id)

    def agent_stopped_speaking(self) -> None:
        if self.interruption is not None:
            self.interruption.agent_stopped_speaking()
