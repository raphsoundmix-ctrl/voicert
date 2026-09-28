"""What the game is told a conversation is doing, and when.

The state machine exists for the player: without it, "you were not heard", "you
were heard and I am thinking" and "I am talking" all look like silence. Its one
hard rule is that it must never raise — it is driven from the audio path and
from a network reader — so the tests below are as much about what it refuses to
do as about what it does.
"""

import pytest

from voicert.game.session import SessionState, SessionStateMachine
from voicert.processors.local import SaidOnce, SentenceBuffer


@pytest.fixture
def seen():
    return []


@pytest.fixture
def machine(seen):
    return SessionStateMachine(seen.append, label="test")


def test_a_spoken_turn_walks_the_expected_path(machine, seen):
    assert machine.heard_speech()
    assert machine.utterance_captured()
    assert machine.voice_started()
    assert machine.turn_ended()
    assert seen == [
        SessionState.LISTENING, SessionState.PROCESSING,
        SessionState.SPEAKING, SessionState.IDLE,
    ]


def test_a_cough_returns_to_idle_without_a_turn(machine, seen):
    machine.heard_speech()
    assert machine.speech_dropped()
    assert seen == [SessionState.LISTENING, SessionState.IDLE]


def test_speech_dropped_does_nothing_once_the_utterance_is_away(machine):
    machine.heard_speech()
    machine.utterance_captured()
    assert not machine.speech_dropped(), "the turn is already running"
    assert machine.state is SessionState.PROCESSING


def test_barge_in_reports_the_cut_and_then_the_listening(machine, seen):
    """The ordinary shape of a barge-in, and a live-test regression.

    The VAD hears the player and announces LISTENING first; the cut it triggers
    only reaches the sink a moment later. With LISTENING -> INTERRUPTED
    disallowed, the game was told the player was talking and never told the
    reply had been dropped — which is what a live run showed.
    """
    machine.utterance_captured()
    machine.voice_started()
    machine.heard_speech()                      # the player talks over the answer
    assert machine.interrupted(still_listening=True)
    assert seen[-2:] == [SessionState.INTERRUPTED, SessionState.LISTENING]


def test_barge_in_with_the_player_already_quiet_lands_on_interrupted(machine):
    machine.utterance_captured()
    machine.voice_started()
    machine.interrupted(still_listening=False)
    assert machine.state is SessionState.INTERRUPTED


def test_a_turn_that_ends_while_the_player_talks_stays_listening(machine):
    machine.utterance_captured()
    machine.voice_started()
    machine.heard_speech()
    assert not machine.turn_ended()
    assert machine.state is SessionState.LISTENING


def test_a_failed_stage_ends_the_turn_and_not_the_conversation(machine):
    machine.utterance_captured()
    assert machine.failed()
    assert machine.utterance_captured(), "the next thing the player says must still work"


def test_typing_over_a_reply_is_a_new_turn(machine):
    machine.utterance_captured()
    machine.voice_started()
    assert machine.utterance_captured(), "a second typed line while the first is spoken"


def test_disconnected_is_terminal(machine):
    machine.closed()
    assert not machine.heard_speech()
    assert not machine.failed()
    assert machine.state is SessionState.DISCONNECTED


def test_an_illegal_transition_is_dropped_not_raised(machine, seen):
    assert not machine.voice_started(), "nothing is being said yet"
    assert machine.state is SessionState.IDLE
    assert seen == []


def test_a_broken_display_callback_cannot_break_a_turn():
    def explode(_state):
        raise RuntimeError("the panel is gone")

    machine = SessionStateMachine(explode)
    assert machine.heard_speech()          # must not raise
    assert machine.state is SessionState.LISTENING


# -- not saying the same thing twice ------------------------------------------


def test_a_clause_is_refused_the_second_time():
    said = SaidOnce()
    assert said.accept("No entry without a pass.")
    assert not said.accept("No entry without a pass.")
    assert not said.accept("no entry without a pass"), "punctuation and case do not make it new"


def test_short_interjections_stay_available():
    said = SaidOnce()
    for _ in range(3):
        assert said.accept("Aye."), "a character may say aye as often as they like"
    assert said.accept("No.")


def test_the_line_that_started_this():
    """"Passes only." is thirteen characters, and was the whole problem.

    With the threshold at eighteen it slipped under, and the guard answered four
    different questions with it. Ten catches it and still leaves "Aye." alone.
    """
    said = SaidOnce()
    assert said.accept("Passes only.")
    assert not said.accept("Passes only.")


def test_the_memory_of_said_clauses_is_bounded():
    said = SaidOnce(remember=3)
    for i in range(5):
        assert said.accept(f"This is sentence number {i}.")
    # The first two have aged out, so they are speakable again.
    assert said.accept("This is sentence number 0.")
    assert not said.accept("This is sentence number 4.")


def test_sentence_buffer_still_cuts_between_words_without_punctuation():
    buffer = SentenceBuffer(max_chars=20)
    out = buffer.push("one two three four five six seven")
    assert out and all(" " in s or len(s) <= 20 for s in out)
    assert not any(s.startswith(" ") for s in out)
