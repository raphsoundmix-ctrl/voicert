"""Dialogue LOD — level of detail, applied to speech instead of geometry.

Nobody renders every blade of grass in an open world at full resolution, and
nobody should run a full STT->LLM->TTS pipeline for an NPC muttering forty
metres away. The tiers below cost radically different amounts, and an NPC
moves between them the same way a mesh swaps LOD levels:

===========  ================================  ===========================
tier         what runs                         marginal cost per NPC
===========  ================================  ===========================
LIVE         full pipeline: STT + LLM + TTS     one voice-pool slot
BARK         LLM *selects* a pre-baked line     ~0 (asset playback)
CROWD        ambient murmur bed, no per-NPC     ~0 (one looping bed)
OFF          nothing; state frozen              0
===========  ================================  ===========================

The crucial property: **cost scales with what the player can hear, not with
how many NPCs the world contains.** A market square with 200 vendors still
runs at most a handful of LIVE agents, because a human can only hold one
conversation and only a few voices are ever intelligible at once.

Hysteresis is not a nicety here. Without it, an NPC standing exactly on a tier
boundary — or a player strafing along it — would thrash between tiers, which
means repeatedly spinning up and tearing down agents. Every threshold
therefore has a separate, wider exit distance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class DialogueTier(IntEnum):
    """Ordered by cost. Higher value = more expensive."""

    OFF = 0
    CROWD = 1
    BARK = 2
    LIVE = 3


@dataclass(frozen=True, slots=True)
class LODConfig:
    """Distances in world units (metres by convention).

    ``*_exit`` values must exceed their enter counterparts — that gap is the
    hysteresis band that prevents tier thrashing on a boundary.
    """

    live_enter_m: float = 6.0
    live_exit_m: float = 9.0
    bark_enter_m: float = 25.0
    bark_exit_m: float = 32.0
    crowd_enter_m: float = 60.0
    crowd_exit_m: float = 75.0
    #: An NPC the player has addressed stays LIVE regardless of distance
    #: until the conversation ends — walking away mid-sentence should not
    #: silence a character.
    conversation_overrides_distance: bool = True

    def __post_init__(self) -> None:
        if not (self.live_enter_m < self.live_exit_m < self.bark_enter_m):
            raise ValueError("live band must be ordered and disjoint from bark band")
        if not (self.bark_enter_m < self.bark_exit_m < self.crowd_enter_m):
            raise ValueError("bark band must be ordered and disjoint from crowd band")
        if self.crowd_enter_m >= self.crowd_exit_m:
            raise ValueError("crowd_exit_m must exceed crowd_enter_m")


@dataclass(slots=True)
class NPCState:
    """What the engine reports each tick for one NPC.

    ``occluded`` and ``audible`` come from the middleware, which already
    computes them for ordinary sounds — reuse those results rather than
    duplicating the raycasts.
    """

    npc_id: str
    distance_m: float
    audible: bool = True
    occluded: bool = False
    in_conversation: bool = False
    #: Quest-critical or scripted NPCs win pool slots over ambient ones.
    priority: float = 0.0
    tier: DialogueTier = DialogueTier.OFF


class DialogueLOD:
    """Assigns a tier per NPC per tick, with hysteresis.

    Usage from the engine's game thread::

        lod = DialogueLOD(LODConfig())
        for npc in npcs:
            tier = lod.evaluate(npc)   # NPCState is updated in place
    """

    def __init__(self, config: LODConfig | None = None) -> None:
        self.config = config or LODConfig()

    def evaluate(self, npc: NPCState) -> DialogueTier:
        cfg = self.config
        current = npc.tier

        if npc.in_conversation and cfg.conversation_overrides_distance:
            npc.tier = DialogueTier.LIVE
            return npc.tier

        if not npc.audible:
            npc.tier = DialogueTier.OFF
            return npc.tier

        d = npc.distance_m
        # Occlusion costs an NPC one tier: a voice through a wall does not
        # deserve a live agent, but should still register as presence.
        occlusion_penalty = 1 if npc.occluded else 0

        # Each threshold uses the exit distance when we are already in that
        # tier or above, and the (nearer) enter distance when climbing into it.
        if d <= self._threshold(current, DialogueTier.LIVE, cfg.live_enter_m, cfg.live_exit_m):
            target = DialogueTier.LIVE
        elif d <= self._threshold(current, DialogueTier.BARK, cfg.bark_enter_m, cfg.bark_exit_m):
            target = DialogueTier.BARK
        elif d <= self._threshold(current, DialogueTier.CROWD, cfg.crowd_enter_m, cfg.crowd_exit_m):
            target = DialogueTier.CROWD
        else:
            target = DialogueTier.OFF

        if occlusion_penalty:
            target = DialogueTier(max(DialogueTier.OFF, target - occlusion_penalty))

        npc.tier = target
        return target

    @staticmethod
    def _threshold(
        current: DialogueTier, candidate: DialogueTier, enter_m: float, exit_m: float
    ) -> float:
        """Already at/above this tier -> the wider exit distance keeps us here;
        climbing up into it -> the stricter enter distance must be met."""
        return exit_m if current >= candidate else enter_m

    def partition(self, npcs: list[NPCState]) -> dict[DialogueTier, list[NPCState]]:
        """Evaluate a whole tick's worth of NPCs and group them by tier."""
        buckets: dict[DialogueTier, list[NPCState]] = {t: [] for t in DialogueTier}
        for npc in npcs:
            buckets[self.evaluate(npc)].append(npc)
        return buckets
