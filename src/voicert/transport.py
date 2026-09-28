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
from voicert.utterance import UtteranceSegmenter

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

    def reset(self) -> None:
        """Forget that speech was in progress. Used when something outside the
        audio path ends an utterance, so the next chunk is judged on its own."""
        self._speaking = False
        self._silence_ms = 0.0

    @property
    def speaking(self) -> bool:
        """Whether the VAD currently believes the user is mid-utterance.

        A caller that wants to know *why* nothing was dispatched after a
        SPEECH_END needs this: a closed VAD means the utterance was dropped as
        too short, an open one means it is still being captured.
        """
        return self._speaking

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

    def __init__(
        self,
        vad: EnergyVAD | None = None,
        segmenter: UtteranceSegmenter | None = None,
    ) -> None:
        self.vad = vad
        #: Set this when a real microphone is attached. Without it every
        #: incoming chunk becomes its own AudioFrame, which is right for the
        #: stub providers and wrong for any real STT model — see
        #: ``voicert.utterance`` for why.
        self.segmenter = segmenter
        #: Peak absolute sample of the last chunk, 0..1 — a level meter for the
        #: game, so a player whose microphone is muted can see that it is.
        self.input_level: float = 0.0
        self.on_speech_start: Callable[[], None] | None = None
        self.on_speech_end: Callable[[], None] | None = None
        #: Set by the first ``end_utterance()``: this client marks its own
        #: utterance boundaries (a push-to-talk key), so silence inside one must
        #: not close it. Without this a pause mid-sentence became a second turn,
        #: and the answer to the first half arrived over the second.
        self.client_endpoints = False
        self.on_user_audio: Callable[[AudioFrame], Awaitable[None]] | None = None

    @property
    def vad_speaking(self) -> bool:
        return bool(getattr(self.vad, "speaking", False))

    async def feed_input(self, pcm: bytes, sample_rate: int = 16_000) -> None:
        """Push microphone/line audio into the framework."""
        event = self.vad.feed(pcm, sample_rate) if self.vad is not None else None
        self.input_level = _peak_level(pcm)

        if self.segmenter is None:
            # Chunk-per-frame: what the stub providers and the offline demo want.
            if event is VADEvent.SPEECH_START and self.on_speech_start:
                self.on_speech_start()
            elif event is VADEvent.SPEECH_END and self.on_speech_end:
                self.on_speech_end()
            if self.on_user_audio is not None:
                await self.on_user_audio(
                    AudioFrame(pcm=pcm, sample_rate=sample_rate, source="user")
                )
            return

        # Utterance mode: the segmenter sees every chunk, but only a complete
        # utterance reaches the pipeline.
        overflow = self.segmenter.feed(pcm)
        if event is VADEvent.SPEECH_START:
            self.segmenter.begin()
            if self.on_speech_start:
                self.on_speech_start()   # barge-in still fires on the leading edge
        elif event is VADEvent.SPEECH_END:
            if self.on_speech_end:
                self.on_speech_end()
            # A client with its own endpoint key keeps the utterance open across
            # the pauses inside a sentence; only its ENDPOINT closes one.
            if not self.client_endpoints:
                overflow = self.segmenter.end() or overflow

        if overflow and self.on_user_audio is not None:
            await self.on_user_audio(
                AudioFrame(pcm=overflow, sample_rate=sample_rate, source="user")
            )

    async def end_utterance(self) -> bool:
        """Close the current utterance immediately, whatever the VAD thinks.

        Returns whether anything was actually dispatched: a key tapped by
        mistake, or a word too quiet for the VAD to open on, produces no
        utterance, and the caller has to say so rather than leave the game
        waiting for an answer nobody asked for.

        This is what a released push-to-talk key means. Endpointing by silence
        costs the hangover on every turn — 450 ms the player waits after they
        have already finished — and a player holding a key has told us exactly
        when they stopped. With no segmenter there is nothing buffered to close,
        because every chunk was dispatched as it arrived.
        """
        self.client_endpoints = True
        if self.segmenter is None:
            return False
        pcm = self.segmenter.end()
        if self.vad is not None:
            self.vad.reset()
        if self.on_speech_end is not None:
            self.on_speech_end()
        if not pcm:
            return False
        if self.on_user_audio is not None:
            await self.on_user_audio(
                AudioFrame(pcm=pcm, sample_rate=self.segmenter.sample_rate, source="user")
            )
        return True

    async def sink(self, frame: Frame) -> None:
        """Pipeline output lands here. Override: play audio, flush on
        InterruptionFrame."""
        raise NotImplementedError


def _peak_level(pcm: bytes) -> float:
    """Peak of an int16 chunk in 0..1. Sampled, not summed: this runs on every
    20 ms chunk of every session and a level meter does not need every sample."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    step = max(1, len(samples) // 128)
    return min(1.0, max(abs(samples[i]) for i in range(0, len(samples), step)) / 32768.0)


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
