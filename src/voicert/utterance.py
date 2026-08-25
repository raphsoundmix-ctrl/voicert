"""Utterance segmentation — the piece between a live microphone and STT.

A microphone delivers 20-50 ms chunks forever. Whisper-class models want one
complete utterance. Something has to decide where an utterance starts and
stops, and that something is the VAD plus this buffer.

Two details make the difference between "it transcribes" and "it transcribes
correctly":

**Pre-roll.** A VAD needs energy before it fires, so by the time SPEECH_START
arrives the first 30-100 ms of the word is already past. Feed the model what
it heard from that moment and "hello" arrives as "ello". This keeps a rolling
ring of the recent past and prepends it, so the model gets the whole word.

**Hangover is the VAD's job, not ours.** ``EnergyVAD`` already waits out a
gap before declaring SPEECH_END, which is what stops a pause between words
from cutting an utterance in half. This class trusts that decision and only
adds the two guards a VAD cannot make: a minimum length, so a cough or a door
slam never reaches the model, and a maximum, so a stuck-open VAD (a noisy room,
a fan near the mic) cannot grow the buffer without bound.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("voicert.utterance")

BYTES_PER_SAMPLE = 2  # PCM16 mono


@dataclass(frozen=True, slots=True)
class UtteranceConfig:
    """All durations in milliseconds, at the transport's sample rate."""

    #: Audio kept from *before* the VAD fired, so the first phoneme survives.
    preroll_ms: int = 300
    #: Shorter than this is a cough, a click, or a chair — not speech.
    min_utterance_ms: int = 250
    #: Longer than this and something is wrong (stuck VAD, constant noise).
    #: The buffer is flushed to STT rather than grown further.
    max_utterance_ms: int = 20_000

    def __post_init__(self) -> None:
        if self.preroll_ms < 0:
            raise ValueError("preroll_ms cannot be negative")
        if self.min_utterance_ms < 0:
            raise ValueError("min_utterance_ms cannot be negative")
        if self.max_utterance_ms <= self.min_utterance_ms:
            raise ValueError("max_utterance_ms must exceed min_utterance_ms")


class UtteranceSegmenter:
    """Turns a stream of PCM chunks plus VAD edges into whole utterances.

    Drive it from the transport::

        seg = UtteranceSegmenter(UtteranceConfig(), sample_rate=16_000)
        seg.feed(chunk)                       # every chunk, always
        seg.begin()                           # on VAD SPEECH_START
        pcm = seg.end()                       # on VAD SPEECH_END -> to STT

    ``feed`` returns the utterance when the max-length guard trips, so a
    caller that only ever sees SPEECH_START never leaks memory.
    """

    def __init__(self, config: UtteranceConfig | None = None, sample_rate: int = 16_000) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.config = config or UtteranceConfig()
        self.sample_rate = sample_rate
        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0
        self._capture: list[bytes] = []
        self._capture_bytes = 0
        self._capturing = False

    # -- sizing helpers ---------------------------------------------------

    def _ms_to_bytes(self, ms: int) -> int:
        return int(self.sample_rate * ms / 1000) * BYTES_PER_SAMPLE

    def _bytes_to_ms(self, n: int) -> float:
        return n / BYTES_PER_SAMPLE / self.sample_rate * 1000

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    @property
    def captured_ms(self) -> float:
        return self._bytes_to_ms(self._capture_bytes)

    # -- the stream -------------------------------------------------------

    def feed(self, pcm: bytes) -> bytes | None:
        """Take one chunk. Returns an utterance only if the max guard trips."""
        if self._capturing:
            self._capture.append(pcm)
            self._capture_bytes += len(pcm)
            if self._capture_bytes >= self._ms_to_bytes(self.config.max_utterance_ms):
                logger.warning(
                    "utterance hit the %d ms ceiling; flushing to STT",
                    self.config.max_utterance_ms,
                )
                return self._flush()
            return None

        # Not capturing: keep the recent past for pre-roll.
        self._preroll.append(pcm)
        self._preroll_bytes += len(pcm)
        limit = self._ms_to_bytes(self.config.preroll_ms)
        while self._preroll_bytes > limit and self._preroll:
            self._preroll_bytes -= len(self._preroll.popleft())
        return None

    def begin(self) -> None:
        """VAD says speech started. Seed the capture with the pre-roll."""
        if self._capturing:
            return
        self._capturing = True
        self._capture = list(self._preroll)
        self._capture_bytes = self._preroll_bytes
        self._preroll.clear()
        self._preroll_bytes = 0

    def end(self) -> bytes | None:
        """VAD says speech ended. Returns the utterance, or None if too short."""
        if not self._capturing:
            return None
        duration_ms = self.captured_ms
        pcm = self._flush()
        if duration_ms < self.config.min_utterance_ms:
            logger.debug("dropped %.0f ms blip (below min_utterance_ms)", duration_ms)
            return None
        return pcm

    def reset(self) -> None:
        """Throw away everything. Used on barge-in and on disconnect."""
        self._capturing = False
        self._capture = []
        self._capture_bytes = 0
        self._preroll.clear()
        self._preroll_bytes = 0

    def _flush(self) -> bytes:
        pcm = b"".join(self._capture)
        self._capturing = False
        self._capture = []
        self._capture_bytes = 0
        return pcm
