"""Bounded voice pool — the reason 500 NPCs cost the same as 3.

Game audio middleware has solved this problem for two decades: you declare a
playback limit, and the engine decides which sounds are physical and which are
virtual. This pool applies the identical discipline to dialogue agents.

The pool size is a *compute contract*, not a guess. It is chosen from the CPU
budget the studio is willing to spend (see ``voicert.game.budget``), and the
world can then contain any number of NPCs without changing that number.

Eviction is by priority, and priority is not just distance: a quest-giver
mid-sentence outranks a closer ambient vendor. Anything evicted degrades to a
cheaper tier rather than falling silent — the NPC keeps speaking, from the
pre-baked bark bank instead of live synthesis.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from voicert.config import AgentRuntime
from voicert.game.lod import NPCState

logger = logging.getLogger("voicert.game.pool")


@dataclass(slots=True)
class PooledVoice:
    """One live agent bound to one NPC."""

    npc_id: str
    runtime: AgentRuntime
    priority: float = 0.0
    #: Monotonic tick of last use — breaks priority ties in favour of whoever
    #: spoke most recently, so an NPC is not cut off mid-line by a tie.
    last_tick: int = 0


class NPCVoicePool:
    """Fixed-capacity pool of live dialogue agents.

    ``factory`` builds a fresh NPC-profile runtime; the pool never constructs
    agents itself so that tests and the engine can inject their own.

    ``on_evict`` is where the caller degrades the NPC to the bark tier. It is
    called *after* the slot is freed, so the callback may immediately request
    a new acquisition without recursion hazards.
    """

    def __init__(
        self,
        capacity: int,
        factory: Callable[[str], AgentRuntime],
        on_evict: Callable[[str], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("voice pool capacity must be at least 1")
        self.capacity = capacity
        self._factory = factory
        self._on_evict = on_evict
        self._voices: dict[str, PooledVoice] = {}
        self._tick = 0

    # -- introspection ---------------------------------------------------

    def __len__(self) -> int:
        return len(self._voices)

    def __contains__(self, npc_id: object) -> bool:
        return npc_id in self._voices

    @property
    def free_slots(self) -> int:
        return self.capacity - len(self._voices)

    def active_ids(self) -> frozenset[str]:
        return frozenset(self._voices)

    def get(self, npc_id: str) -> PooledVoice | None:
        return self._voices.get(npc_id)

    # -- lifecycle -------------------------------------------------------

    def tick(self) -> None:
        """Advance the pool clock. Call once per game tick."""
        self._tick += 1

    def acquire(self, npc: NPCState) -> PooledVoice | None:
        """Bind a live agent to this NPC, evicting a lower-priority one if the
        pool is full. Returns ``None`` when every held slot outranks the
        request — the caller should then keep the NPC on the bark tier."""
        existing = self._voices.get(npc.npc_id)
        if existing is not None:
            existing.priority = npc.priority
            existing.last_tick = self._tick
            return existing

        if self.free_slots == 0 and not self._evict_for(npc):
            logger.debug("pool full, %s stays on bark tier", npc.npc_id)
            return None

        voice = PooledVoice(
            npc_id=npc.npc_id,
            runtime=self._factory(npc.npc_id),
            priority=npc.priority,
            last_tick=self._tick,
        )
        self._voices[npc.npc_id] = voice
        return voice

    def release(self, npc_id: str) -> PooledVoice | None:
        """Free a slot (NPC left the live tier or the conversation ended)."""
        return self._voices.pop(npc_id, None)

    def _evict_for(self, incoming: NPCState) -> bool:
        """Evict the weakest held voice if the incoming NPC outranks it."""
        weakest = min(
            self._voices.values(), key=lambda v: (v.priority, v.last_tick), default=None
        )
        if weakest is None or weakest.priority >= incoming.priority:
            return False
        self._voices.pop(weakest.npc_id, None)
        logger.info(
            "evicted %s (prio %.2f) for %s (prio %.2f)",
            weakest.npc_id,
            weakest.priority,
            incoming.npc_id,
            incoming.priority,
        )
        if self._on_evict is not None:
            self._on_evict(weakest.npc_id)
        return True

    def reconcile(self, live_npcs: list[NPCState]) -> list[PooledVoice]:
        """One-call-per-tick convenience: bind the highest-priority live NPCs,
        release anyone who left the live tier.

        Returns the voices that are live after reconciliation.
        """
        self.tick()
        wanted = sorted(live_npcs, key=lambda n: n.priority, reverse=True)
        wanted_ids = {n.npc_id for n in wanted}

        for npc_id in list(self._voices):
            if npc_id not in wanted_ids:
                self.release(npc_id)

        for npc in wanted:
            self.acquire(npc)
        return list(self._voices.values())
