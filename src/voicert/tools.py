"""Tool surface — the NPC's "tool firewall".

A profile owns a closed ToolRegistry. The NPC's holds game-engine tools and
nothing else, so a prompt-injected "search the web" or "open that file"
inside a game has nothing to resolve to: ``ToolRegistry.get`` raises
``PermissionError``. The guardrail is the structure, not prompt discipline.

Handlers here are demo stubs returning canned payloads; a game plugs in live
handlers by re-registering a Tool with the same schema.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    handler: ToolHandler | None = None


class ToolRegistry:
    def __init__(self, profile: str, tools: list[Tool]) -> None:
        self.profile = profile
        self._tools: Mapping[str, Tool] = MappingProxyType({t.name: t for t in tools})

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise PermissionError(
                f"tool '{name}' is not available in profile '{self.profile}'"
            ) from None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.get(name)
        if tool.handler is None:
            raise NotImplementedError(f"tool '{name}' has no live handler yet (demo stub)")
        return await tool.handler(arguments)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def _stub(payload: dict[str, Any]) -> ToolHandler:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        return payload

    return handler


def _p(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props)}


def npc_tools() -> ToolRegistry:
    # Deliberately minimal: game-engine surface only. No web, no file system,
    # no OS — the lore guardrail is structural, not just prompt-level.
    return ToolRegistry(
        "npc",
        [
            Tool(
                "emit_game_event",
                "Emit an event into the game engine (WebSocket/gRPC).",
                _p(event={"type": "string"}, payload={"type": "object"}),
                _stub({"emitted": True}),
            ),
            Tool(
                "query_world_state",
                "Query world/quest state from the game engine.",
                _p(key={"type": "string"}),
                _stub({"state": {}}),
            ),
            Tool(
                "play_animation",
                "Play a character animation/gesture, synchronized with the line.",
                _p(animation={"type": "string"}),
                _stub({"playing": True}),
            ),
        ],
    )
