"""voicert — real-time, interruptible voice for game NPCs.

The runtime behind one character asset: the player's microphone in, an
in-character reply out, voiced into the game's own FMOD mix. The core is
stdlib-only asyncio; local providers, the engine bridge and the economics
ledger live in ``voicert.processors``, ``voicert.game`` and
``voicert.economics``.

Design references (studied, nothing ported):
  * Pipecat        -> frame/pipeline composition model
  * LiveKit Agents -> transport-level VAD + barge-in interruption handling
  * Rapida AI      -> profile assembly, state orchestration, latency metrics
"""

from voicert.config import AgentRuntime, ConfigFactory, ProfileConfig
from voicert.frames import (
    AudioFrame,
    EndFrame,
    Frame,
    FunctionCallFrame,
    InterruptionFrame,
    InterruptionReason,
    TextFrame,
)
from voicert.interruption import InterruptionManager
from voicert.pipeline import FrameProcessor, Pipeline
from voicert.state import StateContextManager

__version__ = "0.1.0"

__all__ = [
    "AgentRuntime",
    "AudioFrame",
    "ConfigFactory",
    "EndFrame",
    "Frame",
    "FrameProcessor",
    "FunctionCallFrame",
    "InterruptionFrame",
    "InterruptionManager",
    "InterruptionReason",
    "Pipeline",
    "ProfileConfig",
    "StateContextManager",
    "TextFrame",
    "__version__",
]
