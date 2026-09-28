"""Frame types — the only currency that moves through a pipeline.

Every piece of data (audio chunk, text partial, tool call, interruption signal)
is an immutable frame. Processors consume frames and yield frames; nothing else
crosses processor boundaries, which is what makes providers swappable.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_seq = itertools.count(1)


def _next_id() -> int:
    return next(_seq)


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """Base frame. ``id`` is a process-wide monotonic sequence number,
    ``ts`` is a monotonic timestamp used for latency accounting (TTFB)."""

    id: int = field(default_factory=_next_id)
    ts: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioFrame(Frame):
    """A chunk of PCM audio.

    ``source`` distinguishes user microphone audio (pipeline input) from
    agent TTS audio (pipeline output) so the same frame type can travel
    both directions without ambiguity.
    """

    pcm: bytes
    sample_rate: int = 16_000
    channels: int = 1
    source: str = "user"  # "user" | "agent"


@dataclass(frozen=True, slots=True, kw_only=True)
class TextFrame(Frame):
    """Text produced by STT (user) or LLM (assistant).

    ``final=False`` marks streaming partials: STT interim hypotheses and
    LLM token deltas. Downstream processors must treat partials as
    replaceable, not cumulative facts.
    """

    text: str
    role: str = "user"  # "user" | "assistant" | "system"
    final: bool = True
    turn_id: int = 0
    #: For assistant frames: the user turn this one answers. Turn ids are
    #: otherwise related only by "the user's came first", which stops being true
    #: the moment two typed lines arrive before the model opens its reply.
    reply_to: int = 0


class InterruptionReason(str, Enum):
    USER_BARGE_IN = "user_barge_in"
    GAME_EVENT = "game_event"
    POLICY = "policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptionFrame(Frame):
    """Barge-in signal. Broadcast by the pipeline to every processor so each
    one cancels its in-flight work; then delivered downstream to the sink so
    the transport can flush its playback buffer."""

    reason: InterruptionReason
    turn_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FunctionCallFrame(Frame):
    """A tool invocation emitted by the LLM processor, or its result on the
    way back. Profiles own disjoint tool registries — see voicert.tools."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    result: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorFrame(Frame):
    """A stage failed and this turn will produce nothing more.

    Errors travel *forward* like any other frame so the transport can tell the
    engine. Without one, a provider failure — Ollama evicted, CUDA out of
    memory, a bad speaker id — is logged on the server and looks to the game
    exactly like an NPC that decided not to answer.
    """

    message: str
    stage: str = ""
    turn_id: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class EndFrame(Frame):
    """Terminates a pipeline run. Flows through every processor so each can
    flush, then stops the pumps."""
