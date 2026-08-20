"""Transport & VAD layer — LiveKit-inspired edge of the framework.

Everything upstream of the pipeline: audio I/O and voice activity
detection. The core rule: VAD is **local** (no network round-trip) because
barge-in latency is bounded by how fast we *notice* the user speaking.

Shipping now: EnergyVAD (stdlib-only, deterministic, good enough for demo
and tests) + LoopbackTransport. Skeletons with documented contracts:
SileroVAD (ONNX), WebRTCTransport (aiortc), SipTwilioTransport (telephony
for the sales profile).
"""

from __future__ import annotations

import asyncio
import logging
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from voicert.frames import AudioFrame, Frame

logger = logging.getLogger("voicert.transport")


class VADEvent(Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass(frozen=True, slots=True)
class VADConfig:
    """``sensitivity`` 0..1 (higher = triggers on quieter speech);
    ``hangover_ms`` — silence needed before SPEECH_END (endpointing)."""

    sensitivity: float = 0.5
    hangover_ms: int = 300


class EnergyVAD:
    """RMS-threshold VAD over int16 PCM. Zero dependencies, fully
    deterministic — the reference implementation for tests and the demo.

    Not production-grade for noisy rooms; that's SileroVAD's job. The
    interface is identical, so swapping is a one-line config change.
    """

    def __init__(self, config: VADConfig, sample_rate: int = 16_000) -> None:
        self.config = config
        self.sample_rate = sample_rate
        # sensitivity 0..1 -> threshold ~3000..300 (int16 RMS units)
        self._threshold = 3000 - 2700 * min(max(config.sensitivity, 0.0), 1.0)
        self._speaking = False
        self._silence_ms = 0.0

    def feed(self, pcm: bytes, sample_rate: int | None = None) -> VADEvent | None:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        if not samples:
            return None
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        # Hangover accounting must use the *actual* rate of this chunk, or
        # 8 kHz telephony audio would look twice as long as it really is.
        frame_ms = len(samples) / (sample_rate or self.sample_rate) * 1000.0
        if rms >= self._threshold:
            self._silence_ms = 0.0
            if not self._speaking:
                self._speaking = True
                return VADEvent.SPEECH_START
        elif self._speaking:
            self._silence_ms += frame_ms
            if self._silence_ms >= self.config.hangover_ms:
                self._speaking = False
                return VADEvent.SPEECH_END
        return None


class SileroVAD:
    """Silero VAD v5 over onnxruntime — production choice (per-frame
    inference <1 ms on CPU, robust to noise/music). Contract identical to
    EnergyVAD: ``feed(pcm) -> VADEvent | None`` on 30-ms 16 kHz chunks.

    Requires ``pip install voicert[vad]``. Implementation lands with the
    provider wiring milestone."""

    def __init__(self, config: VADConfig, sample_rate: int = 16_000) -> None:
        raise NotImplementedError(
            "SileroVAD: install voicert[vad] (onnxruntime) — skeleton until model wiring."
        )


class BaseTransport:
    """Audio edge: feeds user audio + VAD events in, plays agent audio out.

    ``on_speech_start`` / ``on_speech_end`` are wired to the
    InterruptionManager by the ConfigFactory. ``sink`` is where the
    pipeline's output frames land (playback + interruption flush).
    """

    name = "transport"

    def __init__(self, vad: EnergyVAD | None = None) -> None:
        self.vad = vad
        self.on_speech_start: Callable[[], None] | None = None
        self.on_speech_end: Callable[[], None] | None = None
        self.on_user_audio: Callable[[AudioFrame], Awaitable[None]] | None = None

    async def feed_input(self, pcm: bytes, sample_rate: int = 16_000) -> None:
        """Push microphone/line audio into the framework."""
        if self.vad is not None:
            event = self.vad.feed(pcm, sample_rate)
            if event is VADEvent.SPEECH_START and self.on_speech_start:
                self.on_speech_start()
            elif event is VADEvent.SPEECH_END and self.on_speech_end:
                self.on_speech_end()
        if self.on_user_audio is not None:
            await self.on_user_audio(AudioFrame(pcm=pcm, sample_rate=sample_rate, source="user"))

    async def sink(self, frame: Frame) -> None:
        """Pipeline output lands here. Override: play audio, flush on
        InterruptionFrame."""
        raise NotImplementedError


class LoopbackTransport(BaseTransport):
    """In-memory transport for tests and the offline demo: collects every
    output frame and exposes simple await-until-audio helpers."""

    name = "loopback"

    def __init__(self, vad: EnergyVAD | None = None) -> None:
        super().__init__(vad)
        self.outbox: list[Frame] = []
        self.first_audio = asyncio.Event()

    async def sink(self, frame: Frame) -> None:
        self.outbox.append(frame)
        if isinstance(frame, AudioFrame):
            self.first_audio.set()


class WebRTCTransport(BaseTransport):
    """aiortc-based WebRTC: Opus in/out, jitter buffer, DataChannel for
    events. Default for assistant & npc profiles. ``pip install
    voicert[webrtc]``. Skeleton until the transport milestone."""

    name = "webrtc"

    def __init__(self, vad: EnergyVAD | None = None) -> None:
        super().__init__(vad)
        logger.warning("WebRTCTransport is a documented skeleton — wire aiortc to go live.")

    async def sink(self, frame: Frame) -> None:
        raise NotImplementedError("WebRTCTransport: install voicert[webrtc] and implement.")


class SipTwilioTransport(BaseTransport):
    """Twilio Media Streams over websocket: 8 kHz μ-law both ways, DTMF
    passthrough, <Connect><Stream> TwiML entrypoint. Default for the sales
    profile. Skeleton until the telephony milestone."""

    name = "sip-twilio"

    def __init__(self, vad: EnergyVAD | None = None) -> None:
        super().__init__(vad)
        logger.warning("SipTwilioTransport is a documented skeleton — wire Twilio to go live.")

    async def sink(self, frame: Frame) -> None:
        raise NotImplementedError("SipTwilioTransport: wire Twilio Media Streams to go live.")
