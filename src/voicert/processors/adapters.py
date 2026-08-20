"""Provider adapter skeletons — the seams where real models plug in.

Deliberately not implemented yet (модели подключаются позже): each class
documents its wire protocol, env var, and target latency so wiring a
provider is a mechanical ~50-line diff against a stable interface, with
zero changes to the core pipeline.

Planned matrix (latency figures = vendor-published p50 for streaming):

    STT   Deepgram Nova-3 (ws stream, ~150 ms interim)   DEEPGRAM_API_KEY
          faster-whisper local (GPU, offline fallback)   —
    LLM   Anthropic Claude (Haiku for NPC, Sonnet+)      ANTHROPIC_API_KEY
          OpenAI GPT-4.1-mini / OpenRouter any-model     OPENAI_API_KEY / OPENROUTER_API_KEY
    TTS   ElevenLabs Flash v2.5 (~75 ms TTFB)            ELEVENLABS_API_KEY
          Cartesia Sonic (~90 ms TTFB, ws stream)        CARTESIA_API_KEY
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from voicert.frames import AudioFrame, Frame, TextFrame
from voicert.processors.base import LLMService, STTService, TTSService

_NOT_WIRED = (
    "{name} is an adapter skeleton: set {env} and implement the streaming "
    "client (interface is final, core pipeline needs no changes)."
)


def _require_env(var: str, name: str) -> str:
    value = os.environ.get(var, "")
    if not value:
        raise RuntimeError(_NOT_WIRED.format(name=name, env=var))
    return value


class DeepgramSTT(STTService):
    """Deepgram Nova-3 over websocket: send 16 kHz mono PCM, receive interim
    (``is_final=false``) and final transcripts. Map 1:1 onto TextFrame partials."""

    name = "stt-deepgram"

    async def transcribe(self, frame: AudioFrame) -> AsyncIterator[TextFrame]:
        _require_env("DEEPGRAM_API_KEY", "DeepgramSTT")
        raise NotImplementedError(_NOT_WIRED.format(name="DeepgramSTT", env="DEEPGRAM_API_KEY"))
        yield  # pragma: no cover


class WhisperLocalSTT(STTService):
    """faster-whisper on local GPU — offline fallback, no network, no key.
    Higher latency than Deepgram streaming; fine for assistant profile."""

    name = "stt-whisper-local"

    async def transcribe(self, frame: AudioFrame) -> AsyncIterator[TextFrame]:
        raise NotImplementedError(
            "WhisperLocalSTT: pip install voicert[vad] + faster-whisper, then implement."
        )
        yield  # pragma: no cover


class AnthropicLLM(LLMService):
    """Claude via Messages API streaming. Profile mapping: NPC -> Haiku
    (latency), Sales/Assistant -> Sonnet. Tools pass through as
    FunctionCallFrame."""

    name = "llm-anthropic"

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        _require_env("ANTHROPIC_API_KEY", "AnthropicLLM")
        raise NotImplementedError(_NOT_WIRED.format(name="AnthropicLLM", env="ANTHROPIC_API_KEY"))
        yield  # pragma: no cover


class OpenRouterLLM(LLMService):
    """Any model behind one key — cheap experimentation across providers."""

    name = "llm-openrouter"

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        _require_env("OPENROUTER_API_KEY", "OpenRouterLLM")
        raise NotImplementedError(_NOT_WIRED.format(name="OpenRouterLLM", env="OPENROUTER_API_KEY"))
        yield  # pragma: no cover


class ElevenLabsTTS(TTSService):
    """ElevenLabs Flash v2.5 websocket streaming — lowest published TTFB,
    first choice for the NPC profile."""

    name = "tts-elevenlabs"

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        _require_env("ELEVENLABS_API_KEY", "ElevenLabsTTS")
        raise NotImplementedError(_NOT_WIRED.format(name="ElevenLabsTTS", env="ELEVENLABS_API_KEY"))
        yield  # pragma: no cover


class CartesiaTTS(TTSService):
    """Cartesia Sonic — state-space TTS, stable prosody on long sessions."""

    name = "tts-cartesia"

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        _require_env("CARTESIA_API_KEY", "CartesiaTTS")
        raise NotImplementedError(_NOT_WIRED.format(name="CartesiaTTS", env="CARTESIA_API_KEY"))
        yield  # pragma: no cover
