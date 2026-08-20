"""Step 1 acceptance: Pipeline([StubSTT, StubLLM, StubTTS]) carries an
AudioFrame end-to-end into agent AudioFrames, with metrics stamped."""

import pytest

from tests.conftest import audio_frames, wait_for_first_audio, wait_until_quiet
from voicert.config import ConfigFactory
from voicert.frames import AudioFrame, TextFrame
from voicert.pipeline import Pipeline
from voicert.processors.stubs import make_user_audio


async def test_end_to_end_audio_roundtrip(assistant_runtime):
    await assistant_runtime.say("Hi, tell me what you can do")
    await wait_for_first_audio(assistant_runtime)
    await wait_until_quiet(assistant_runtime)

    audio = audio_frames(assistant_runtime)
    assert audio, "pipeline must emit agent audio for a user utterance"
    assert all(f.source == "agent" for f in audio)
    assert sum(len(f.pcm) for f in audio) > 0


async def test_metrics_ttfb_recorded(assistant_runtime):
    await assistant_runtime.say("Hello")
    await wait_for_first_audio(assistant_runtime)
    await wait_until_quiet(assistant_runtime)

    report = assistant_runtime.metrics.report(1)
    assert report["stt_final"] is not None
    assert report["llm_first_token"] is not None
    assert report["tts_first_audio"] is not None
    # Causality: audio cannot precede the first token.
    assert report["tts_first_audio"] >= report["llm_first_token"]


async def test_state_history_after_turn(assistant_runtime):
    await assistant_runtime.say("Remember: I like my coffee black")
    await wait_for_first_audio(assistant_runtime)
    await wait_until_quiet(assistant_runtime)

    roles = [t.role for t in assistant_runtime.state.turns]
    assert roles[0] == "user"
    assert "assistant" in roles
    assistant_turn = next(t for t in assistant_runtime.state.turns if t.role == "assistant")
    assert assistant_turn.final and not assistant_turn.interrupted
    assert len(assistant_turn.text) > 0


async def test_pipeline_rejects_empty_processor_list():
    with pytest.raises(ValueError):
        Pipeline([])


async def test_push_before_start_raises():
    runtime = ConfigFactory.build("assistant")
    with pytest.raises(RuntimeError):
        await runtime.pipeline.push(make_user_audio("test"))


async def test_frames_are_immutable():
    frame = TextFrame(text="hello")
    with pytest.raises(AttributeError):
        frame.text = "mutated"  # type: ignore[misc]
    audio = AudioFrame(pcm=b"\x00\x01")
    with pytest.raises(AttributeError):
        audio.pcm = b""  # type: ignore[misc]
