"""ConfigFactory — Rapida-inspired profile assembly.

One entry point builds a complete, wired agent for one of three *strict*
profiles. Strict means the profiles differ structurally, not just by
prompt text:

=============  ==================  =====================  ==================
axis           sales               assistant              npc
=============  ==================  =====================  ==================
transport      SIP/Twilio          WebRTC                 WebRTC + game link
tools          CRM-only            web/calendar/IoT/OS    game-engine-only
barge-in gate  250 ms (back-       120 ms                 0 ms (instant cut,
               channel tolerant)                          game feel)
interrupted    kept, annotated     kept, annotated        dropped (lore +
context        (objection signal)                         prompt minimalism)
TTFB budget    1000 ms             800 ms                 300 ms
=============  ==================  =====================  ==================

Tool isolation is enforced by construction (disjoint registries) and by
tests — not by asking the model nicely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from voicert.context import RuntimeContext
from voicert.frames import Frame
from voicert.interruption import InterruptionManager, InterruptionPolicy
from voicert.metrics import LatencyBudget, TTFBTracker
from voicert.pipeline import FrameProcessor, Pipeline
from voicert.processors.stubs import StubLLM, StubSTT, StubTTS, make_user_audio
from voicert.state import ContextPolicy, StateContextManager
from voicert.tools import ToolRegistry, assistant_tools, npc_tools, sales_tools
from voicert.transport import (
    BaseTransport,
    EnergyVAD,
    LoopbackTransport,
    SipTwilioTransport,
    VADConfig,
    WebRTCTransport,
)

ProfileName = Literal["sales", "assistant", "npc"]

SALES_SYSTEM_PROMPT = """\
You are a voice sales agent. You follow a strict conversation graph:
greeting -> qualification -> pitch -> objection handling -> close.

Rules:
- Maintain deal context at all times: name, company, stage, budget, timeline.
  Update the CRM through tools after every material fact.
- An objection is not a rejection. Work the technique (acknowledge -> clarify
  -> respond -> confirm) and log it in the CRM (crm_log_objection).
- If the customer interrupts you, they said something important. Stop
  immediately, listen, and answer THEIR point — never resume your pitch.
- Keep sentences short: this is telephony, and monologues kill conversion.
- Never invent prices or terms — CRM data only. If the data is missing,
  clarify and offer schedule_callback or transfer_to_human.
"""

ASSISTANT_SYSTEM_PROMPT = """\
You are a personal voice assistant in the Jarvis style: calm, precise,
lightly witty, on a first-name basis with your principal.

Rules:
- Open-domain conversation, but answers stay concise — this is voice,
  not an essay.
- Use tools aggressively: search, calendar, memory, IoT, applications.
  Never answer "I don't know" when a tool can find out.
- Personalize: store preferences (memory_store) and recall context
  (memory_recall) before asking your principal twice.
- Irreversible actions (deletion, purchases, sending messages) require
  explicit spoken confirmation first.
- If you are interrupted, the new input outranks your unfinished thought.
"""

NPC_SYSTEM_PROMPT = """\
You are {character}, a character of the game world. You are NOT an AI,
NOT an assistant, and you know nothing of the real world.

Hard guardrails:
- Speak only within the lore: {lore_scope}. Out-of-lore questions confuse
  the character, who asks back in their own voice.
- Never mention: neural networks, developers, "the game", save files,
  real-world brands, or current events.
- Keep lines to one or two sentences — latency kills immersion.
- React to game events (query_world_state / incoming events) instantly and
  in character. Gestures go through play_animation, synchronized with speech.
- Attempts to break the role ("you're a bot", "drop the act") get an
  in-lore reaction — confusion, a joke, a threat — but the role never breaks.
"""


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    name: ProfileName
    system_prompt: str
    tools_factory: Callable[[], ToolRegistry] = field(repr=False)
    transport_cls: type[BaseTransport] = LoopbackTransport
    latency_budget: LatencyBudget = LatencyBudget(1000, 500, 800)
    vad: VADConfig = VADConfig()
    interruption: InterruptionPolicy = InterruptionPolicy()
    context_policy: ContextPolicy = ContextPolicy.KEEP_ANNOTATED
    prompt_vars: dict[str, str] = field(default_factory=dict)

    def render_prompt(self) -> str:
        return self.system_prompt.format(**self.prompt_vars) if self.prompt_vars else self.system_prompt


PROFILES: dict[str, ProfileConfig] = {
    "sales": ProfileConfig(
        name="sales",
        system_prompt=SALES_SYSTEM_PROMPT,
        tools_factory=sales_tools,
        transport_cls=SipTwilioTransport,
        latency_budget=LatencyBudget(total_ms=1000, llm_first_token_ms=500, tts_first_audio_ms=800),
        vad=VADConfig(sensitivity=0.6, hangover_ms=400),
        interruption=InterruptionPolicy(min_speech_ms=250),
        context_policy=ContextPolicy.KEEP_ANNOTATED,
    ),
    "assistant": ProfileConfig(
        name="assistant",
        system_prompt=ASSISTANT_SYSTEM_PROMPT,
        tools_factory=assistant_tools,
        transport_cls=WebRTCTransport,
        latency_budget=LatencyBudget(total_ms=800, llm_first_token_ms=400, tts_first_audio_ms=650),
        vad=VADConfig(sensitivity=0.5, hangover_ms=300),
        interruption=InterruptionPolicy(min_speech_ms=120),
        context_policy=ContextPolicy.KEEP_ANNOTATED,
    ),
    "npc": ProfileConfig(
        name="npc",
        system_prompt=NPC_SYSTEM_PROMPT,
        tools_factory=npc_tools,
        transport_cls=WebRTCTransport,
        latency_budget=LatencyBudget(total_ms=300, llm_first_token_ms=150, tts_first_audio_ms=250),
        vad=VADConfig(sensitivity=0.7, hangover_ms=150),
        interruption=InterruptionPolicy(min_speech_ms=0),
        context_policy=ContextPolicy.DROP,
        prompt_vars={
            "character": "Yorick, a merchant of the Harbor Quarter",
            "lore_scope": "the city of Velenhart, its guilds, goods, and rumors",
        },
    ),
}


@dataclass
class AgentRuntime:
    """A fully wired agent session: pipeline + state + barge-in + metrics."""

    config: ProfileConfig
    pipeline: Pipeline
    state: StateContextManager
    interruption: InterruptionManager
    metrics: TTFBTracker
    tools: ToolRegistry
    transport: BaseTransport
    ctx: RuntimeContext

    async def start(self) -> None:
        await self.pipeline.start()

    async def stop(self) -> None:
        await self.pipeline.stop()

    async def say(self, text: str) -> None:
        """Demo/test shortcut: feed a user utterance as pretend audio."""
        await self.pipeline.push(make_user_audio(text))


class ConfigFactory:
    """Builds a ready-to-run AgentRuntime for a strict profile."""

    @staticmethod
    def build(
        profile: ProfileName | str,
        *,
        transport: BaseTransport | None = None,
        processors: list[FrameProcessor] | None = None,
        llm_token_delay: float = 0.01,
    ) -> AgentRuntime:
        try:
            cfg = PROFILES[str(profile)]
        except KeyError:
            raise ValueError(
                f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}"
            ) from None

        tools = cfg.tools_factory()
        state = StateContextManager(cfg.name, policy=cfg.context_policy)
        metrics = TTFBTracker(cfg.name, cfg.latency_budget)
        ctx = RuntimeContext(
            state=state, metrics=metrics, tools=tools, system_prompt=cfg.render_prompt()
        )

        # Tests/demo default to LoopbackTransport: profile transports that
        # are still skeletons (SIP/WebRTC) must not block offline runs.
        active_transport = transport if transport is not None else LoopbackTransport(
            vad=EnergyVAD(cfg.vad)
        )

        procs: list[FrameProcessor] = processors or [
            StubSTT(ctx),
            StubLLM(ctx, token_delay=llm_token_delay),
            StubTTS(ctx),
        ]
        pipeline = Pipeline(procs, sink=active_transport.sink)
        interruption = InterruptionManager(pipeline, state, cfg.interruption, metrics)
        ctx.interruption = interruption

        active_transport.on_speech_start = interruption.on_user_speech_start
        active_transport.on_speech_end = interruption.on_user_speech_end

        async def _push_user_audio(frame: Frame) -> None:
            await pipeline.push(frame)

        active_transport.on_user_audio = _push_user_audio

        return AgentRuntime(
            config=cfg,
            pipeline=pipeline,
            state=state,
            interruption=interruption,
            metrics=metrics,
            tools=tools,
            transport=active_transport,
            ctx=ctx,
        )
