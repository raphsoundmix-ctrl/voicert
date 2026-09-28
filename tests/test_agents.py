"""Who an NPC is, and what they are allowed to remember.

The properties worth guarding here are the ones a player would notice going
wrong: a character who forgets between visits, a character who knows something
they were never told, and a character who quietly turns into a general-purpose
assistant because the role rules fell out of the prompt.
"""

import json

import pytest

from voicert.game.agents import (
    AgentRegistry,
    ConversationRecorder,
    MemoryStore,
    NpcAgent,
    NpcMemory,
    NpcPersona,
    World,
    extract_self_facts,
    load_world,
    safe_id,
)
from voicert.state import ContextPolicy, StateContextManager

GUARD = NpcPersona(
    npc_id="guard",
    name="Merrick",
    role="the night guard on the warehouse door",
    voice="guard",
    knows=("Nobody passes without a Harbour Office pass",),
    forbidden=("what is stored inside the warehouse",),
    deflect="and get back to watching the door",
)

WORLD = World(name="Kettleport", facts=("Ships come in on the morning tide",),
              personas={"guard": GUARD})


def agent(memory: NpcMemory | None = None) -> NpcAgent:
    return NpcAgent(persona=GUARD, memory=memory or NpcMemory(npc_id="guard"), world=WORLD)


# -- the prompt -------------------------------------------------------------


def test_prompt_carries_role_world_and_limits():
    prompt = agent().system_prompt()
    assert "Merrick" in prompt
    assert "Ships come in on the morning tide" in prompt          # world lore
    assert "Nobody passes without a Harbour Office pass" in prompt  # role lore
    assert "what is stored inside the warehouse" in prompt          # per-character limit
    assert "and get back to watching the door" in prompt            # how they deflect


def test_restriction_is_the_last_thing_the_model_reads():
    """Order is load-bearing, not cosmetic.

    Measured on qwen3:1.7b with this persona: with the rules in the middle and
    the delivery notes after them, the guard explained quantum entanglement and
    described a list-reversal algorithm. Moved last, both became one refusal.
    """
    prompt = agent().system_prompt()
    assert prompt.index("What you know:") < prompt.index("How you answer")
    assert prompt.index("You are heard, not read") < prompt.index("How you answer")
    assert "Do not answer it." in prompt


def test_a_character_who_has_met_the_player_says_so():
    without = agent().system_prompt()
    assert "you have not spoken before" in without

    memory = NpcMemory(npc_id="guard")
    memory.note_fact("The player is called Alex")
    with_memory = agent(memory).system_prompt()
    assert "The player is called Alex" in with_memory
    assert "you have not spoken before" not in with_memory


def test_history_replays_in_order():
    memory = NpcMemory(npc_id="guard")
    memory.remember("user", "My name is Alex")
    memory.remember("assistant", "Aye, Alex.")
    assert [m["role"] for m in agent(memory).history()] == ["user", "assistant"]


# -- memory ------------------------------------------------------------------


def test_facts_are_deduplicated_case_insensitively():
    memory = NpcMemory(npc_id="guard")
    assert memory.note_fact("The player is called Alex")
    assert not memory.note_fact("the player is called alex")
    assert memory.facts == ["The player is called Alex"]


def test_blank_turns_are_not_remembered():
    memory = NpcMemory(npc_id="guard")
    memory.remember("user", "   ")
    memory.remember("system", "not a speaker")
    assert memory.transcript == []


def test_overflow_detaches_the_oldest_and_leaves_the_window():
    memory = NpcMemory(npc_id="guard", max_messages=4)
    for i in range(7):
        memory.remember("user", f"line {i}")
    assert memory.overflow == 3
    taken = memory.take_overflow()
    assert [m["text"] for m in taken] == ["line 0", "line 1", "line 2"]
    assert len(memory.transcript) == 4
    assert memory.overflow == 0


def test_store_round_trips_and_is_scoped_per_character(tmp_path):
    store = MemoryStore(tmp_path)
    guard = NpcMemory(npc_id="guard")
    guard.note_fact("The player is called Alex")
    guard.remember("user", "My name is Alex")
    store.save(guard)

    reloaded = store.load("guard")
    assert reloaded.facts == ["The player is called Alex"]
    assert reloaded.transcript[0]["text"] == "My name is Alex"

    # The fruit vendor was never told, and there is no query that could tell them.
    assert store.load("fruit-vendor").facts == []
    assert store.load("fruit-vendor").transcript == []


def test_memory_is_scoped_per_player_too(tmp_path):
    store = MemoryStore(tmp_path)
    alex = NpcMemory(npc_id="guard", player_id="alex")
    alex.note_fact("The player is called Alex")
    store.save(alex)
    assert store.load("guard", "bo").facts == []


def test_a_corrupt_memory_file_starts_the_acquaintance_over(tmp_path):
    store = MemoryStore(tmp_path)
    path = store.path("guard")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    memory = store.load("guard")   # must not raise: the guard still has to talk
    assert memory.facts == []


def test_save_is_atomic_enough_to_leave_no_debris(tmp_path):
    store = MemoryStore(tmp_path)
    memory = NpcMemory(npc_id="guard")
    memory.note_fact("The player is called Alex")
    store.save(memory)
    store.save(memory)
    files = sorted(p.name for p in store.path("guard").parent.iterdir())
    assert files == ["guard.json"]
    assert not memory.dirty


@pytest.mark.parametrize("raw,expected", [
    ("Guard", "guard"),
    ("../../etc/passwd", "etc-passwd"),
    ("fruit vendor!", "fruit-vendor"),
    ("", "unknown"),
])
def test_ids_off_the_wire_cannot_escape_the_directory(raw, expected):
    assert safe_id(raw) == expected


# -- the registry -------------------------------------------------------------


def test_one_memory_per_character_even_across_sessions(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    first = registry.acquire("guard")
    second = registry.acquire("guard")
    assert first is second, "two sockets for one NPC must not diverge into two memories"


def test_an_unknown_character_falls_back_to_the_game_s_description(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    fallback = NpcPersona(npc_id="ghost", name="A Ghost", role="haunts the pier")
    ghost = registry.acquire("ghost", fallback=fallback)
    assert "A Ghost" in ghost.system_prompt()
    assert "haunts the pier" in ghost.system_prompt()


def test_release_persists_and_wipe_forgets(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    guard = registry.acquire("guard")
    guard.memory.remember("user", "My name is Alex")
    registry.release(guard)
    assert MemoryStore(tmp_path).load("guard").transcript

    registry.wipe()
    assert MemoryStore(tmp_path).load("guard").transcript == []
    assert registry.acquire("guard").memory.transcript == []


def test_world_file_round_trip(tmp_path):
    path = tmp_path / "world.json"
    path.write_text(json.dumps({
        "name": "Kettleport",
        "facts": ["It is late evening"],
        "npcs": {"guard": {"name": "Merrick", "role": "the night guard", "voice": "guard",
                           "knows": ["Nobody passes without a pass"]}},
    }), encoding="utf-8")
    world = load_world(path)
    assert world.name == "Kettleport"
    assert world.persona("guard").name == "Merrick"
    assert world.persona("nobody") is None


def test_a_missing_world_file_is_not_an_error(tmp_path):
    assert load_world(tmp_path / "nope.json").personas == {}


# -- the recorder --------------------------------------------------------------


def test_recorder_writes_finished_turns_once(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    guard = registry.acquire("guard")
    state = StateContextManager("npc")
    recorder = ConversationRecorder(guard, registry, state)

    state.add_user_final("Can I enter?")
    turn = state.begin_assistant_turn()
    state.append_assistant_text(turn.turn_id, "No passes, no entry.")
    assert recorder.flush() == 1, "the assistant turn is still open"
    state.commit_assistant(turn.turn_id)
    assert recorder.flush() == 1
    assert recorder.flush() == 0, "a second flush must not duplicate the exchange"
    assert [m["text"] for m in guard.memory.transcript] == ["Can I enter?", "No passes, no entry."]


def test_recorder_ignores_the_history_it_was_seeded_with(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    guard = registry.acquire("guard")
    guard.memory.remember("user", "My name is Alex")
    guard.memory.remember("assistant", "Aye, Alex.")

    state = StateContextManager("npc")
    state.add_history(guard.history())
    recorder = ConversationRecorder(guard, registry, state)
    assert recorder.flush() == 0
    assert len(guard.memory.transcript) == 2, "replayed turns must not be written back"


def test_recorder_keeps_only_what_was_spoken_after_a_barge_in(tmp_path):
    registry = AgentRegistry(WORLD, MemoryStore(tmp_path))
    guard = registry.acquire("guard")
    state = StateContextManager("npc")
    recorder = ConversationRecorder(guard, registry, state)

    state.add_user_final("Tell me about the warehouse")
    turn = state.begin_assistant_turn()
    state.append_assistant_text(turn.turn_id, "That door has been shut for years and nobody")
    state.mark_spoken(turn.turn_id, len("That door has been shut for years"))
    state.interrupt_assistant(turn.turn_id)

    recorder.close()
    spoken = guard.memory.transcript[-1]["text"]
    assert spoken == "That door has been shut for years"
    assert "nobody" not in spoken


# -- what the player says about themselves ------------------------------------


@pytest.mark.parametrize("said,expected", [
    ("My name is Alex, by the way.", ["The player is called Alex"]),
    ("I'm called Bo", ["The player is called Bo"]),
    ("call me Nell", ["The player is called Nell"]),
    ("Name's Merrick.", ["The player is called Merrick"]),
    # Not an introduction, and the point of requiring a capital letter.
    ("I am tired", []),
    ("What is my name?", []),
    ("my name is alex", []),
])
def test_an_introduction_becomes_a_fact(said, expected):
    assert extract_self_facts(said) == expected


def test_the_name_survives_the_transcript_being_trimmed():
    """Why this is a fact and not just a line in the conversation.

    Measured: told his name mid-argument, the guard answered "Ain't names I care
    much for" and could not repeat it a turn later — one line in a transcript
    does not survive against a prompt full of what the character must not know.
    A fact is listed where the model actually reads.
    """
    memory = NpcMemory(npc_id="guard", max_messages=2)
    for fact in extract_self_facts("My name is Alex, by the way."):
        memory.note_fact(fact)
    for i in range(6):
        memory.remember("user", f"and another thing {i}")
    memory.take_overflow()
    assert memory.facts == ["The player is called Alex"]
    assert "The player is called Alex" in agent(memory).system_prompt()


# -- the failure paths the review found ----------------------------------------


def test_an_interrupted_line_is_not_replayed_as_a_finished_one(tmp_path):
    """A cut-off line is true, and still must not come back as ordinary dialogue.

    The NPC profile sets ContextPolicy.DROP so the model never sees an
    interrupted turn. Persisting the spoken prefix and replaying it next session
    as a complete assistant line reverses that decision behind the profile's
    back, and teaches the model to answer in fragments.
    """
    memory = NpcMemory(npc_id="guard")
    memory.remember("user", "Tell me about the warehouse")
    memory.remember("assistant", "That door has been shut", interrupted=True)
    memory.remember("user", "Fine")
    memory.remember("assistant", "Aye.")

    dropping = StateContextManager("npc", policy=ContextPolicy.DROP)
    assert dropping.add_history(memory.transcript) == 3
    assert all("shut" not in t.text for t in dropping.turns)

    keeping = StateContextManager("npc-annotated", policy=ContextPolicy.KEEP_ANNOTATED)
    assert keeping.add_history(memory.transcript) == 4
    assert any(t.interrupted for t in keeping.turns)


def test_the_interrupted_flag_survives_the_disk(tmp_path):
    store = MemoryStore(tmp_path)
    memory = NpcMemory(npc_id="guard")
    memory.remember("assistant", "That door has been shut", interrupted=True)
    store.save(memory)
    assert store.load("guard").transcript[0]["interrupted"] is True


@pytest.mark.parametrize("raw", ["con", "CON", "nul", "com1", "LPT9", "aux"])
def test_windows_device_names_cannot_become_a_memory_file(raw):
    """`con.json` is the console on Windows, whatever the extension.

    An npc_id comes off the wire, and reading it would block the event loop on a
    name the client chose.
    """
    assert safe_id(raw) not in {"con", "nul", "aux", "com1", "lpt9"}
    assert safe_id(raw).endswith("-npc")
