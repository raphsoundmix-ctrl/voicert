"""Step 3 acceptance — ConfigFactory builds all three strict profiles;
tool sets do not leak across profiles; each profile runs a smoke turn."""

import pytest

from tests.conftest import audio_frames, wait_for_first_audio, wait_until_quiet
from voicert.config import PROFILES, ConfigFactory
from voicert.state import ContextPolicy
from voicert.transport import LoopbackTransport, SipTwilioTransport, WebRTCTransport


def test_three_profiles_exist():
    assert set(PROFILES) == {"sales", "assistant", "npc"}


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        ConfigFactory.build("hr-bot")


@pytest.mark.parametrize(
    ("profile", "marker"),
    [
        ("sales", "objection"),      # objection handling is the sales core
        ("assistant", "Jarvis"),
        ("npc", "lore"),
    ],
)
def test_system_prompts_are_distinct(profile, marker):
    runtime = ConfigFactory.build(profile)
    assert marker.lower() in runtime.ctx.system_prompt.lower()
    others = [p for p in PROFILES if p != profile]
    for other in others:
        assert runtime.ctx.system_prompt != ConfigFactory.build(other).ctx.system_prompt


def test_npc_prompt_renders_lore_vars():
    runtime = ConfigFactory.build("npc")
    assert "{character}" not in runtime.ctx.system_prompt
    assert "Yorick" in runtime.ctx.system_prompt


def test_tool_sets_exact():
    assert ConfigFactory.build("sales").tools.names() == {
        "crm_lookup",
        "crm_update_deal",
        "crm_log_objection",
        "schedule_callback",
        "transfer_to_human",
    }
    assert ConfigFactory.build("assistant").tools.names() == {
        "web_search",
        "calendar_create",
        "memory_store",
        "memory_recall",
        "iot_command",
        "os_open_app",
    }
    assert ConfigFactory.build("npc").tools.names() == {
        "emit_game_event",
        "query_world_state",
        "play_animation",
    }


def test_tool_firewall_no_cross_profile_leak():
    sales = ConfigFactory.build("sales").tools
    assistant = ConfigFactory.build("assistant").tools
    npc = ConfigFactory.build("npc").tools

    assert sales.names() & assistant.names() == frozenset()
    assert sales.names() & npc.names() == frozenset()
    assert assistant.names() & npc.names() == frozenset()

    # NPC physically cannot reach CRM; sales cannot reach the game engine.
    assert "crm_lookup" not in npc
    assert "emit_game_event" not in sales
    with pytest.raises(PermissionError):
        npc.get("crm_lookup")
    with pytest.raises(PermissionError):
        sales.get("emit_game_event")


def test_transport_defaults_per_profile():
    assert PROFILES["sales"].transport_cls is SipTwilioTransport
    assert PROFILES["assistant"].transport_cls is WebRTCTransport
    assert PROFILES["npc"].transport_cls is WebRTCTransport
    # Offline builds fall back to loopback so skeleton transports never block.
    assert isinstance(ConfigFactory.build("sales").transport, LoopbackTransport)


def test_latency_budgets_ordered_npc_tightest():
    npc = PROFILES["npc"].latency_budget
    assistant = PROFILES["assistant"].latency_budget
    sales = PROFILES["sales"].latency_budget
    assert npc.total_ms < assistant.total_ms < sales.total_ms
    assert npc.total_ms <= 300, "NPC budget is the game-feel contract"


def test_interruption_policy_npc_instant_sales_tolerant():
    assert PROFILES["npc"].interruption.min_speech_ms == 0
    assert PROFILES["sales"].interruption.min_speech_ms >= 200
    assert PROFILES["npc"].context_policy is ContextPolicy.DROP
    assert PROFILES["sales"].context_policy is ContextPolicy.KEEP_ANNOTATED


@pytest.mark.parametrize("profile", ["sales", "assistant", "npc"])
async def test_smoke_turn_per_profile(profile):
    runtime = ConfigFactory.build(profile)
    await runtime.start()
    try:
        await runtime.say("Hello there!")
        await wait_for_first_audio(runtime)
        await wait_until_quiet(runtime)
        assert audio_frames(runtime), f"{profile}: pipeline must produce audio"
        assert runtime.state.turns[0].role == "user"
    finally:
        await runtime.stop()


async def test_tool_call_path_uses_profile_registry():
    runtime = ConfigFactory.build("sales")
    await runtime.start()
    try:
        await runtime.say("Use a tool to look up the deal")
        await wait_for_first_audio(runtime)
        await wait_until_quiet(runtime)
        from voicert.frames import FunctionCallFrame

        calls = [f for f in runtime.transport.outbox if isinstance(f, FunctionCallFrame)]
        assert calls and calls[0].tool_name in runtime.tools.names()
    finally:
        await runtime.stop()
