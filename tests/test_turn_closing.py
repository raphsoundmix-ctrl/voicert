"""A turn that fails must still be a finished turn.

Found in review, with a reproduction: when a provider raised, `commit_assistant`
and the final frame were both after the `try`, so the assistant turn stayed open
forever. Everything that walks the history in order stops at the first turn that
is not final — so one Ollama timeout silently ended memory persistence for the
rest of the session, while the conversation itself carried on looking healthy.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from voicert.config import ConfigFactory
from voicert.frames import Frame, TextFrame
from voicert.game.agents import AgentRegistry, ConversationRecorder, MemoryStore, NpcPersona, World
from voicert.processors.base import LLMService


class ExplodingLLM(LLMService):
    """A provider that dies the way a real one does: mid-turn, on the network."""

    name = "llm-exploding"

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        raise RuntimeError("ollama went away")
        yield  # pragma: no cover — makes this an async generator


class LateExplodingLLM(LLMService):
    """One clause out, then failure — the shape a read timeout actually takes."""

    name = "llm-late-exploding"

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        yield TextFrame(text="No entry without a pass.", role="assistant", final=False)
        raise RuntimeError("read timeout")


async def _run_and_fail(stage: LLMService, ctx) -> None:
    turn = ctx.state.add_user_final("can I enter?")
    frame = TextFrame(text="can I enter?", role="user", final=True, turn_id=turn.turn_id)
    with pytest.raises(RuntimeError):
        async for _ in stage.process_frame(frame):
            pass


@pytest.mark.parametrize("provider", [ExplodingLLM, LateExplodingLLM])
async def test_a_failed_turn_is_still_closed(provider):
    ctx = ConfigFactory.build("npc").ctx
    await _run_and_fail(provider(ctx), ctx)
    assistant = [t for t in ctx.state.turns if t.role == "assistant"]
    assert assistant, "the turn was opened"
    assert all(t.final for t in assistant), "a failed turn left the history open forever"
    assert ctx.state.current_assistant_turn() is None


async def test_a_failure_does_not_stop_memory_for_the_rest_of_the_session(tmp_path):
    """The blocker as the player would meet it: one bad turn, then a good one."""
    registry = AgentRegistry(
        World(personas={"guard": NpcPersona(npc_id="guard", name="Merrick")}),
        MemoryStore(tmp_path),
    )
    agent = registry.acquire("guard")
    ctx = ConfigFactory.build("npc").ctx
    recorder = ConversationRecorder(agent, registry, ctx.state)

    await _run_and_fail(ExplodingLLM(ctx), ctx)
    recorder.flush()

    # The next exchange completes normally.
    turn = ctx.state.add_user_final("my name is Alex")
    assistant = ctx.state.begin_assistant_turn()
    ctx.state.append_assistant_text(assistant.turn_id, "Aye, Alex.")
    ctx.state.commit_assistant(assistant.turn_id)
    recorder.flush()

    remembered = [m["text"] for m in agent.memory.transcript]
    assert "my name is Alex" in remembered, "a later turn was never written down"
    assert "Aye, Alex." in remembered


async def test_a_barge_in_still_leaves_the_turn_open_for_reconciliation():
    """The one case that must NOT be closed early.

    A cancelled turn belongs to the InterruptionManager, which truncates it to
    the prefix that was actually spoken. Committing it here would race that.
    """
    ctx = ConfigFactory.build("npc").ctx

    class Slow(LLMService):
        name = "llm-slow"

        async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
            yield TextFrame(text="That door has been shut for years.", role="assistant", final=False)
            await asyncio.sleep(10)

    turn = ctx.state.add_user_final("what is in there?")
    frame = TextFrame(text="what is in there?", role="user", final=True, turn_id=turn.turn_id)
    agen = Slow(ctx).process_frame(frame)
    await agen.__anext__()                     # first clause out, turn open
    await agen.aclose()                        # what a barge-in does

    open_turn = ctx.state.current_assistant_turn()
    assert open_turn is not None, "the interruption manager needs this turn open"
