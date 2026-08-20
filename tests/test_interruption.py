"""Step 2 acceptance — barge-in correctness, including the race cases:

(a) plain interruption mid-LLM-generation;
(b) interruption while TTS is already streaming audio;
(c) rapid double interruption (race) — no deadlock, no exception,
    pipeline still serves the next turn.
"""

import asyncio

from tests.conftest import (
    audio_frames,
    interruption_frames,
    wait_for_first_audio,
    wait_until_quiet,
)
from voicert.frames import InterruptionReason


async def test_a_interrupt_mid_generation(assistant_runtime):
    await assistant_runtime.say("Tell me a long story")
    await wait_for_first_audio(assistant_runtime)

    performed = await assistant_runtime.interruption.interrupt(InterruptionReason.USER_BARGE_IN)
    assert performed is True

    # InterruptionFrame must reach the sink so the transport flushes playback.
    assert len(interruption_frames(assistant_runtime)) == 1

    turn = next(t for t in assistant_runtime.state.turns if t.role == "assistant")
    assert turn.interrupted and turn.final
    assert assistant_runtime.metrics.interruptions == 1


async def test_b_interrupt_while_tts_streaming_stops_audio(assistant_runtime):
    await assistant_runtime.say("Tell me a very long story about space")
    await wait_for_first_audio(assistant_runtime)
    assert audio_frames(assistant_runtime), "TTS must already be streaming"

    await assistant_runtime.interruption.interrupt()
    frames_at_cut = len(audio_frames(assistant_runtime))

    # Generous settling window: any stale frame produced before the cut
    # would land here and fail the assertion.
    await asyncio.sleep(0.3)
    assert len(audio_frames(assistant_runtime)) == frames_at_cut, (
        "no audio may arrive after the interruption cut"
    )

    turn = next(t for t in assistant_runtime.state.turns if t.role == "assistant")
    assert turn.interrupted
    # Spoken-prefix reconciliation: history keeps only what was voiced.
    assert len(turn.text) <= turn.spoken_chars or turn.spoken_chars == len(turn.text)


async def test_b2_spoken_prefix_shorter_than_generated(assistant_runtime):
    await assistant_runtime.say("Tell me a very long story")
    await wait_for_first_audio(assistant_runtime)
    await assistant_runtime.interruption.interrupt()
    await wait_until_quiet(assistant_runtime)

    turn = next(t for t in assistant_runtime.state.turns if t.role == "assistant")
    # The stub reply is long; cutting right after first audio must leave
    # the kept text strictly shorter than the full canned reply.
    full_reply_len = len(
        "[assistant] Understood: “Tell me a very long story”. "
        "Here is a deliberately long and detailed answer, spoken slowly "
        "enough that you can barge in and cut me off mid-sentence. "
    )
    assert 0 < len(turn.text) < full_reply_len


async def test_c_rapid_double_interrupt_is_safe(assistant_runtime):
    await assistant_runtime.say("Tell me a long story about the sea")
    await wait_for_first_audio(assistant_runtime)

    # Race: two interrupts fired concurrently. Exactly one performs the
    # cut; the other coalesces. No exception, no deadlock.
    results = await asyncio.wait_for(
        asyncio.gather(
            assistant_runtime.interruption.interrupt(),
            assistant_runtime.interruption.interrupt(),
        ),
        timeout=3.0,
    )
    assert sorted(results) == [False, True]
    assert len(interruption_frames(assistant_runtime)) == 1

    # Pipeline must survive and serve the next turn end-to-end.
    loop_transport = assistant_runtime.transport
    loop_transport.first_audio.clear()
    before = len(audio_frames(assistant_runtime))
    await assistant_runtime.say("Now briefly: what time is it?")
    await wait_for_first_audio(assistant_runtime)
    assert len(audio_frames(assistant_runtime)) > before


async def test_vad_gate_backchannel_does_not_interrupt(assistant_runtime):
    """A speech burst shorter than min_speech_ms (back-channel "uh-huh")
    must NOT cut the agent: speech_end disarms the gate."""
    await assistant_runtime.say("Tell me a long story about mountains")
    await wait_for_first_audio(assistant_runtime)

    mgr = assistant_runtime.interruption
    mgr.on_user_speech_start()          # gate armed (120 ms for assistant)
    await asyncio.sleep(0.02)
    mgr.on_user_speech_end()            # burst ended early -> disarm
    await asyncio.sleep(0.25)

    assert interruption_frames(assistant_runtime) == []
    assert assistant_runtime.metrics.interruptions == 0


async def test_vad_gate_sustained_speech_interrupts(assistant_runtime):
    await assistant_runtime.say("Tell me a long story about the forest")
    await wait_for_first_audio(assistant_runtime)

    assistant_runtime.interruption.on_user_speech_start()
    await asyncio.sleep(0.3)  # > 120 ms gate -> cut fires

    assert len(interruption_frames(assistant_runtime)) == 1
    turn = next(t for t in assistant_runtime.state.turns if t.role == "assistant")
    assert turn.interrupted


async def test_interrupt_when_agent_silent_is_noop(assistant_runtime):
    performed = await assistant_runtime.interruption.interrupt()
    assert performed is False
    assert interruption_frames(assistant_runtime) == []

