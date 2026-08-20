"""voicert — real-time voice AI agent framework.

Architecture synthesis of three reference systems:
  * Pipecat        -> frame/pipeline composition model
  * LiveKit Agents -> transport-level VAD + barge-in interruption handling
  * Rapida AI      -> profile routing, state orchestration, latency metrics

Three strict operating profiles are first-class citizens:
  * ``sales``      — scripted phone agent (SIP/Twilio, CRM tools, objection handling)
  * ``assistant``  — open-domain Jarvis-style assistant (WebRTC, web/IoT/OS tools)
  * ``npc``        — game character (lore guardrails, lowest latency, game-engine link)
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
