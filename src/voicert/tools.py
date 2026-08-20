"""Tool surface per profile — the "tool firewall".

Each profile owns a *disjoint* ToolRegistry. The NPC agent physically has
no CRM tool to call, the sales agent has no game-engine tool: isolation is
enforced by construction (and by tests), not by prompt discipline. A
prompt-injected "call crm_lookup" inside a game simply cannot resolve.

Handlers here are demo stubs returning canned payloads; real integrations
plug in by re-registering a Tool with a live handler — schemas stay the same.
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


def sales_tools() -> ToolRegistry:
    return ToolRegistry(
        "sales",
        [
            Tool(
                "crm_lookup",
                "Look up a customer/deal in the CRM by phone number or name.",
                _p(query={"type": "string"}),
                _stub({"deal": "demo", "stage": "negotiation", "ltv": 48000}),
            ),
            Tool(
                "crm_update_deal",
                "Update deal stage/fields in the CRM.",
                _p(deal_id={"type": "string"}, fields={"type": "object"}),
                _stub({"ok": True}),
            ),
            Tool(
                "crm_log_objection",
                "Record a customer objection and the handling technique used.",
                _p(objection={"type": "string"}, technique={"type": "string"}),
                _stub({"ok": True}),
            ),
            Tool(
                "schedule_callback",
                "Schedule a follow-up call in the account manager's calendar.",
                _p(when_iso={"type": "string"}),
                _stub({"scheduled": True}),
            ),
            Tool(
                "transfer_to_human",
                "Transfer the call to a human account manager (SIP transfer).",
                _p(reason={"type": "string"}),
                _stub({"transferred": True}),
            ),
        ],
    )


def assistant_tools() -> ToolRegistry:
    return ToolRegistry(
        "assistant",
        [
            Tool(
                "web_search",
                "Search the web and return a short digest with sources.",
                _p(query={"type": "string"}),
                _stub({"results": ["demo result"]}),
            ),
            Tool(
                "calendar_create",
                "Create an event in the user's calendar.",
                _p(title={"type": "string"}, when_iso={"type": "string"}),
                _stub({"created": True}),
            ),
            Tool(
                "memory_store",
                "Store a fact about the user in long-term memory.",
                _p(fact={"type": "string"}),
                _stub({"stored": True}),
            ),
            Tool(
                "memory_recall",
                "Recall relevant facts about the user.",
                _p(query={"type": "string"}),
                _stub({"facts": []}),
            ),
            Tool(
                "iot_command",
                "Send a command to a smart-home device (lights, climate, media).",
                _p(device={"type": "string"}, action={"type": "string"}),
                _stub({"ok": True}),
            ),
            Tool(
                "os_open_app",
                "Open an application or file on the user's machine (with confirmation).",
                _p(target={"type": "string"}),
                _stub({"opened": True}),
            ),
        ],
    )


def npc_tools() -> ToolRegistry:
    # Deliberately minimal: game-engine surface only. No web, no OS, no CRM —
    # the lore guardrail is structural, not just prompt-level.
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
