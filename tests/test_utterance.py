"""Utterance segmentation: the gate between a live microphone and real STT.

Without this, every 20 ms chunk reaches the model as its own "sentence".
"""

import pytest

from voicert.frames import AudioFrame
from voicert.transport import BaseTransport, EnergyVAD, LoopbackTransport, VADConfig
from voicert.utterance import UtteranceConfig, UtteranceSegmenter

RATE = 16_000


def chunk(ms: int, value: int = 0) -> bytes:
    """One PCM16 chunk of `ms` milliseconds at 16 kHz."""
    samples = RATE * ms // 1000
    return value.to_bytes(2, "little", signed=True) * samples


def ms_of(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE * 1000


# -- config ------------------------------------------------------------


def test_rejects_incoherent_bounds():
    with pytest.raises(ValueError):
        UtteranceConfig(min_utterance_ms=500, max_utterance_ms=400)
    with pytest.raises(ValueError):
        UtteranceConfig(preroll_ms=-1)


def test_rejects_bad_sample_rate():
    with pytest.raises(ValueError):
        UtteranceSegmenter(sample_rate=0)


# -- pre-roll ----------------------------------------------------------


def test_preroll_recovers_speech_from_before_the_vad_fired():
    """The whole point: a VAD needs energy before it fires, so the first
    phoneme is already in the past by the time SPEECH_START arrives."""
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=300, min_utterance_ms=0), RATE)
    for _ in range(10):
        seg.feed(chunk(30, value=111))       # 300 ms of "already spoken" audio
    seg.begin()
    seg.feed(chunk(500, value=222))
    utterance = seg.end()

    assert utterance is not None
    assert ms_of(utterance) == pytest.approx(800, abs=1), "300 ms pre-roll + 500 ms captured"
    assert utterance.startswith((111).to_bytes(2, "little")), "starts with pre-roll audio"


def test_preroll_ring_does_not_grow_without_bound():
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=200, min_utterance_ms=0), RATE)
    for _ in range(200):                      # 20 s of silence
        seg.feed(chunk(100))
    seg.begin()
    utterance = seg.end()
    assert utterance is not None
    assert ms_of(utterance) <= 300, "only the recent past is kept"


# -- capture -----------------------------------------------------------


def test_chunks_are_joined_into_one_utterance():
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=0, min_utterance_ms=0), RATE)
    seg.begin()
    for _ in range(50):
        assert seg.feed(chunk(20)) is None, "nothing escapes mid-utterance"
    utterance = seg.end()
    assert utterance is not None
    assert ms_of(utterance) == pytest.approx(1000, abs=1)


def test_blip_below_the_floor_is_discarded():
    """A cough or a chair scrape must never reach the model."""
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=0, min_utterance_ms=250), RATE)
    seg.begin()
    seg.feed(chunk(100))
    assert seg.end() is None


def test_utterance_above_the_floor_survives():
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=0, min_utterance_ms=250), RATE)
    seg.begin()
    seg.feed(chunk(400))
    assert seg.end() is not None


def test_stuck_vad_flushes_instead_of_growing_forever():
    """A fan near the mic can hold the VAD open indefinitely. The buffer
    must flush rather than consume memory until the process dies."""
    seg = UtteranceSegmenter(
        UtteranceConfig(preroll_ms=0, min_utterance_ms=0, max_utterance_ms=1000), RATE
    )
    seg.begin()
    flushed = None
    for _ in range(20):                       # 2 s of speech, ceiling is 1 s
        out = seg.feed(chunk(100))
        if out is not None:
            flushed = out
            break
    assert flushed is not None
    assert ms_of(flushed) == pytest.approx(1000, abs=100)
    assert not seg.is_capturing, "flushing ends the utterance"


def test_end_without_begin_is_harmless():
    seg = UtteranceSegmenter()
    assert seg.end() is None


def test_double_begin_does_not_restart_capture():
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=0, min_utterance_ms=0), RATE)
    seg.begin()
    seg.feed(chunk(200))
    seg.begin()                                # spurious second SPEECH_START
    seg.feed(chunk(200))
    utterance = seg.end()
    assert utterance is not None
    assert ms_of(utterance) == pytest.approx(400, abs=1), "audio was not dropped"


def test_reset_drops_everything():
    seg = UtteranceSegmenter(UtteranceConfig(preroll_ms=0, min_utterance_ms=0), RATE)
    seg.begin()
    seg.feed(chunk(500))
    seg.reset()
    assert not seg.is_capturing
    assert seg.end() is None


# -- transport integration ---------------------------------------------


class _Recorder(LoopbackTransport):
    """Captures what reaches the pipeline."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.pushed: list[AudioFrame] = []


async def _wire(transport: BaseTransport) -> list[AudioFrame]:
    pushed: list[AudioFrame] = []

    async def collect(frame: AudioFrame) -> None:
        pushed.append(frame)

    transport.on_user_audio = collect
    return pushed


async def test_without_a_segmenter_every_chunk_still_becomes_a_frame():
    """Default behaviour is unchanged — the stub providers rely on it."""
    transport = LoopbackTransport()
    pushed = await _wire(transport)
    for _ in range(5):
        await transport.feed_input(chunk(20))
    assert len(pushed) == 5


async def test_with_a_segmenter_only_whole_utterances_reach_the_pipeline():
    loud = 6000     # above the EnergyVAD threshold
    transport = LoopbackTransport(vad=EnergyVAD(VADConfig(sensitivity=0.5, hangover_ms=100)))
    transport.segmenter = UtteranceSegmenter(
        UtteranceConfig(preroll_ms=100, min_utterance_ms=100), RATE
    )
    pushed = await _wire(transport)

    await transport.feed_input(chunk(50))                 # silence before
    for _ in range(8):
        await transport.feed_input(chunk(50, value=loud))  # 400 ms of speech
    assert pushed == [], "nothing is emitted while the user is still talking"

    for _ in range(6):
        await transport.feed_input(chunk(50))              # silence -> SPEECH_END

    assert len(pushed) == 1, "exactly one utterance, not one frame per chunk"
    assert ms_of(pushed[0].pcm) >= 400
    assert pushed[0].source == "user"


async def test_barge_in_still_fires_on_the_leading_edge():
    """Segmentation must not delay barge-in until the user stops talking."""
    transport = LoopbackTransport(vad=EnergyVAD(VADConfig(sensitivity=0.5, hangover_ms=100)))
    transport.segmenter = UtteranceSegmenter(UtteranceConfig(preroll_ms=0), RATE)
    fired: list[str] = []
    transport.on_speech_start = lambda: fired.append("start")
    transport.on_speech_end = lambda: fired.append("end")
    await _wire(transport)

    await transport.feed_input(chunk(50, value=6000))
    assert fired == ["start"], "interrupt signal is immediate, not deferred"
