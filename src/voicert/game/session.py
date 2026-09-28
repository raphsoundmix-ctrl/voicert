"""What a voice session is doing right now, as one word the game can show.

The pipeline already knows all of this — the VAD knows the player is talking,
the LLM knows it is generating, the TTS knows it is speaking — but it knows it
in five places, and the game needs it in one. This is that one place.

It exists for the player, not for the code: someone standing in front of an NPC
after saying something needs to see the difference between "you were not heard",
"you were heard and I am thinking" and "I am talking now". Without it, every one
of those looks identical — silence — and the honest 400 ms of thinking reads as
a broken NPC.

Transitions are checked and *never raise*: this is driven from the audio path
and from a network reader, and a state machine that throws there would take a
conversation down over a display detail. An illegal transition is dropped with a
debug line; the failure sinks (ERROR, DISCONNECTED) are always reachable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger("voicert.game.session")


class SessionState(str, Enum):
    """Where a conversation stands. The string value is what goes on the wire."""

    IDLE = "idle"                  # connected, nobody talking
    LISTENING = "listening"        # the player is speaking; audio is being captured
    PROCESSING = "processing"      # utterance captured: STT, then the model
    SPEAKING = "speaking"          # the NPC's voice is on the wire
    INTERRUPTED = "interrupted"    # barge-in: the reply was cut
    ERROR = "error"                # a stage failed; the turn produced nothing
    DISCONNECTED = "disconnected"  # terminal


#: Every state can fail or drop out, so ERROR and DISCONNECTED are added to each
#: row below rather than written out seven times.
_FAILURE = frozenset({SessionState.ERROR, SessionState.DISCONNECTED})

_ALLOWED: dict[SessionState, frozenset[SessionState]] = {
    SessionState.IDLE: frozenset({SessionState.LISTENING, SessionState.PROCESSING}),
    # INTERRUPTED is reachable from LISTENING because that is the ordinary shape
    # of a barge-in: the VAD hears the player first and announces LISTENING, and
    # the cut it triggers only reaches the sink a moment later. Without this the
    # game is told the player is talking but never told the reply was dropped.
    SessionState.LISTENING: frozenset(
        {SessionState.PROCESSING, SessionState.IDLE, SessionState.INTERRUPTED}
    ),
    # PROCESSING may go straight back to IDLE: a model can answer with nothing,
    # and a refusal that is filtered out never reaches the voice.
    SessionState.PROCESSING: frozenset(
        {SessionState.SPEAKING, SessionState.IDLE, SessionState.INTERRUPTED, SessionState.LISTENING}
    ),
    # PROCESSING is reachable from SPEAKING because a player can type a second
    # line while the first answer is still being voiced.
    SessionState.SPEAKING: frozenset(
        {SessionState.IDLE, SessionState.INTERRUPTED, SessionState.LISTENING,
         SessionState.PROCESSING}
    ),
    SessionState.INTERRUPTED: frozenset(
        {SessionState.LISTENING, SessionState.IDLE, SessionState.PROCESSING}
    ),
    # An error ends a turn, it does not end the conversation.
    SessionState.ERROR: frozenset(
        {SessionState.IDLE, SessionState.LISTENING, SessionState.PROCESSING}
    ),
    SessionState.DISCONNECTED: frozenset(),
}


class SessionStateMachine:
    """One per connection. ``on_change`` fires only on an actual change."""

    def __init__(
        self,
        on_change: Callable[[SessionState], None] | None = None,
        *,
        label: str = "session",
    ) -> None:
        self.state = SessionState.IDLE
        self.label = label
        self._on_change = on_change

    def can(self, target: SessionState) -> bool:
        if target is self.state:
            return False
        if self.state is SessionState.DISCONNECTED:
            return False   # terminal: a closed session has nothing left to report
        return target in _FAILURE or target in _ALLOWED[self.state]

    def to(self, target: SessionState) -> bool:
        """Move to ``target``. Returns whether anything changed."""
        if target is self.state:
            return False
        if not self.can(target):
            logger.debug("%s: ignoring %s -> %s", self.label, self.state.value, target.value)
            return False
        previous, self.state = self.state, target
        logger.debug("%s: %s -> %s", self.label, previous.value, target.value)
        if self._on_change is not None:
            try:
                self._on_change(target)
            except Exception:  # noqa: BLE001 — a display callback must never break a turn
                logger.exception("%s: state callback failed", self.label)
        return True

    # -- the events a session actually has ------------------------------

    def heard_speech(self) -> bool:
        return self.to(SessionState.LISTENING)

    def utterance_captured(self) -> bool:
        """Audio (or a typed line) is on its way to the model."""
        return self.to(SessionState.PROCESSING)

    def speech_dropped(self) -> bool:
        """The VAD opened and closed on a cough: nothing was sent."""
        return self.to(SessionState.IDLE) if self.state is SessionState.LISTENING else False

    def voice_started(self) -> bool:
        return self.to(SessionState.SPEAKING)

    def turn_ended(self) -> bool:
        """Back to idle unless the player is already talking again."""
        if self.state is SessionState.LISTENING:
            return False
        return self.to(SessionState.IDLE)

    def interrupted(self, *, still_listening: bool = False) -> bool:
        """Barge-in. Lands on LISTENING when the player is still talking, so the
        display shows the reason for the cut rather than an idle NPC."""
        changed = self.to(SessionState.INTERRUPTED)
        if still_listening:
            changed = self.to(SessionState.LISTENING) or changed
        return changed

    def failed(self) -> bool:
        return self.to(SessionState.ERROR)

    def closed(self) -> bool:
        return self.to(SessionState.DISCONNECTED)
