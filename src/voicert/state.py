"""StateContextManager — Rapida-inspired single source of conversational truth.

The manager's key trick is **spoken-prefix reconciliation** on barge-in:
when the user interrupts, the LLM may have generated 400 characters while
TTS only voiced 90 of them. Committing the full 400 to history would make
the model believe it said things the user never heard. We keep only the
prefix that was actually synthesized (reported by the TTS processor), so
follow-up turns reason about the real conversation, not the imagined one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextPolicy(str, Enum):
    """What happens to an interrupted assistant turn in LLM context.

    KEEP_ANNOTATED — keep spoken prefix, tag it as interrupted (sales:
    an interrupted pitch is signal — usually an objection incoming;
    assistant: keeps dialogue natural).
    DROP — remove the partial turn entirely (npc: lore consistency and a
    minimal prompt beat conversational bookkeeping; latency is king).
    """

    KEEP_ANNOTATED = "keep_annotated"
    DROP = "drop"


@dataclass
class Turn:
    turn_id: int
    role: str
    text: str = ""
    final: bool = False
    interrupted: bool = False
    spoken_chars: int = 0
    ts: float = field(default_factory=time.monotonic)


class StateContextManager:
    """Dialogue history + per-session metadata + profile-aware context policy."""

    def __init__(self, profile: str, policy: ContextPolicy = ContextPolicy.KEEP_ANNOTATED) -> None:
        self.profile = profile
        self.policy = policy
        self.turns: list[Turn] = []
        #: Free-form session data: deal fields (sales), user prefs
        #: (assistant), world/quest state (npc).
        self.session_meta: dict[str, Any] = {}
        self._next_turn_id = 1

    # -- turn lifecycle -------------------------------------------------

    def add_user_final(self, text: str) -> Turn:
        turn = Turn(turn_id=self._alloc_id(), role="user", text=text, final=True)
        self.turns.append(turn)
        return turn

    def begin_assistant_turn(self) -> Turn:
        turn = Turn(turn_id=self._alloc_id(), role="assistant")
        self.turns.append(turn)
        return turn

    def append_assistant_text(self, turn_id: int, delta: str) -> None:
        self._get(turn_id).text += delta

    def mark_spoken(self, turn_id: int, chars: int) -> None:
        """TTS processor reports how many characters have actually been
        synthesized into audio so far. Monotonic max — late reports after
        an interrupt must not extend the spoken prefix."""
        turn = self._get(turn_id)
        if not turn.interrupted:
            turn.spoken_chars = max(turn.spoken_chars, chars)

    def commit_assistant(self, turn_id: int) -> None:
        """LLM stream finished. Deliberately does NOT touch spoken_chars:
        TTS may still be voicing the tail, and a barge-in during that tail
        must still truncate to what was actually spoken."""
        turn = self._get(turn_id)
        turn.final = True

    def interrupt_assistant(self, turn_id: int) -> Turn:
        """Reconcile an interrupted turn: keep only the spoken prefix."""
        turn = self._get(turn_id)
        turn.interrupted = True
        turn.final = True
        if turn.spoken_chars < len(turn.text):
            turn.text = turn.text[: turn.spoken_chars]
        return turn

    def current_assistant_turn(self) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.role == "assistant" and not turn.final:
                return turn
        return None

    # -- LLM context ----------------------------------------------------

    def llm_messages(self, system_prompt: str) -> list[dict[str, str]]:
        """Render history for the LLM according to the profile policy."""
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for turn in self.turns:
            if not turn.final:
                continue
            if turn.interrupted:
                if self.policy is ContextPolicy.DROP:
                    continue
                content = turn.text + " [interrupted by user — response incomplete]"
            else:
                content = turn.text
            if content:
                messages.append({"role": turn.role, "content": content})
        return messages

    # -- internals --------------------------------------------------------

    def _alloc_id(self) -> int:
        tid = self._next_turn_id
        self._next_turn_id += 1
        return tid

    def _get(self, turn_id: int) -> Turn:
        for turn in self.turns:
            if turn.turn_id == turn_id:
                return turn
        raise KeyError(f"unknown turn_id {turn_id}")
