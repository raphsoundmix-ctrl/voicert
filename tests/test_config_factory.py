"""ConfigFactory acceptance — the NPC profile is a contract: engine-only
tools that a prompt cannot widen, a 0 ms barge-in gate, interrupted replies
dropped, a game-feel latency budget, and a smoke turn end to end."""

from dataclasses import replace

import pytest

from tests.conftest import audio_frames, wait_for_first_audio, wait_until_quiet
from voicert.config import PROFILES, ConfigFactory
from voicert.frames import FunctionCallFrame
from voicert.state import ContextPolicy
from voicert.transport import LoopbackTransport

NPC_TOOLS = {"emit_game_event", "query_world_state", "play_animation"}


def test_npc_is_the_only_stock_profile():
    assert set(PROFILES) == {"npc"}


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        ConfigFactory.build("hr-bot")


def test_npc_prompt_renders_lore_vars():
    prompt = ConfigFactory.build("npc").ctx.system_prompt
    assert "{character}" not in prompt and "{lore_scope}" not in prompt
    assert "Yorick" in prompt
    assert "lore" in prompt.lower()
    assert "You are heard, not read" in prompt  # the spoken-style rules ride along


def test_npc_tool_set_is_exactly_the_engine_surface():
    assert ConfigFactory.build("npc").tools.names() == NPC_TOOLS


@pytest.mark.parametrize("outside", ["web_search", "read_file", "send_email", "run_shell"])
async def test_tool_firewall_rejects_anything_outside_the_engine(outside):
    tools = ConfigFactory.build("npc").tools
    assert outside not in tools
    with pytest.raises(PermissionError):
        tools.get(outside)
    # The call path goes through the same lookup: a prompt-injected tool name
    # never reaches a handler.
    with pytest.raises(PermissionError):
        await tools.call(outside, {})


def test_derived_character_keeps_the_contract():
    guard = replace(
        PROFILES["npc"],
        name="guard",
        prompt_vars={"character": "Brann, a gate guard", "lore_scope": "the north gate"},
    )
    runtime = ConfigFactory.build(guard)
    assert "Brann" in runtime.ctx.system_prompt
    assert runtime.tools.names() == NPC_TOOLS
    assert runtime.config.interruption.min_speech_ms == 0
    assert runtime.state.profile == "guard"


def test_offline_build_uses_loopback():
    assert isinstance(ConfigFactory.build("npc").transport, LoopbackTransport)


def test_npc_latency_budget_is_the_game_feel_contract():
    budget = PROFILES["npc"].latency_budget
    assert budget.llm_first_token_ms <= 150
    assert budget.total_ms <= 300


def test_npc_interruption_policy_is_instant_and_drops_the_cut_reply():
    assert PROFILES["npc"].interruption.min_speech_ms == 0
    assert PROFILES["npc"].context_policy is ContextPolicy.DROP


async def test_smoke_turn():
    runtime = ConfigFactory.build("npc")
    await runtime.start()
    try:
        await runtime.say("Hello there!")
        await wait_for_first_audio(runtime)
        await wait_until_quiet(runtime)
        assert audio_frames(runtime), "pipeline must produce audio"
        assert runtime.state.turns[0].role == "user"
    finally:
        await runtime.stop()


async def test_tool_call_path_uses_the_npc_registry():
    runtime = ConfigFactory.build("npc")
    await runtime.start()
    try:
        await runtime.say("Use a tool to check the quest state")
        await wait_for_first_audio(runtime)
        await wait_until_quiet(runtime)
        calls = [f for f in runtime.transport.outbox if isinstance(f, FunctionCallFrame)]
        assert calls and calls[0].tool_name in NPC_TOOLS
    finally:
        await runtime.stop()
