#!/usr/bin/env python3
"""Reference runtime factory for the local-model container.

Wired in by ``VOICERT_RUNTIME_FACTORY=local_factory:build``.

What it does: swaps ``StubLLM`` for ``OllamaLLM`` pointed at the Ollama
running on the Windows *host*, and leaves STT/TTS as stubs.

What it deliberately does NOT do: wire ``WhisperSTT`` or
``SherpaOnnxTTS``. Both load a model file in ``__init__``, and this
factory is called once per NPC connection -- that would load Whisper
per NPC. Sharing those needs a process-wide model handle that
``voicert`` does not expose yet; until it does, they belong outside
this factory. ``OllamaLLM.__init__`` by contrast only stores config
(the model lives in the Ollama process), so per-session construction
is free.

The ctx dance below is deliberate. ``ConfigFactory.build`` creates the
``RuntimeContext`` *inside itself*, but provider constructors need a ctx
*before* the build. So: one throwaway build to obtain a context of the
right shape, then the real build with our processors, then rebind each
processor's public ``.ctx`` to the context that is actually wired to the
live pipeline, state and metrics. Skipping the rebind gives you a
runtime whose metrics and history silently go to an orphan object.
"""

from __future__ import annotations

import logging
import os

from voicert.config import AgentRuntime, ConfigFactory
from voicert.game.bridge import Hello
from voicert.processors.local import OllamaLLM
from voicert.processors.stubs import StubSTT, StubTTS
from voicert.transport import BaseTransport

LOG = logging.getLogger("voicert.docker.local")

PROFILE = os.environ.get("VOICERT_PROFILE", "npc").strip() or "npc"
OLLAMA_URL = os.environ.get("VOICERT_OLLAMA_URL", "").strip() or "http://host.docker.internal:11434"
OLLAMA_MODEL = os.environ.get("VOICERT_OLLAMA_MODEL", "").strip() or "qwen3:1.7b"
NUM_PREDICT = int(os.environ.get("VOICERT_OLLAMA_NUM_PREDICT", "120"))

#: The healthcheck opens a real session every interval. Give it the cheap
#: stub runtime so liveness probing never touches a model backend.
HEALTHCHECK_NPC_ID = "docker-healthcheck"


def _apply_character(runtime: AgentRuntime, hello: Hello) -> None:
    """Same prompt shaping the bridge's default factory does."""
    if hello.character or hello.lore_scope:
        runtime.ctx.system_prompt = runtime.config.system_prompt.format(
            character=hello.character or "a character of this world",
            lore_scope=hello.lore_scope or "this game world",
        )


async def warmup() -> None:
    """Pin the Ollama model before the first NPC speaks.

    The entrypoint awaits this in the background at boot if it exists.
    It is not optional in practice: measured against a cold qwen3:1.7b,
    the first NPC turn exceeded OllamaLLM's 60 s httpx timeout and the
    whole turn was dropped; the next turn still cost ~15 s to first token.
    Neither number is Docker's fault -- it is Ollama loading weights -- but
    paying it at container start instead of mid-dialogue is the difference
    between a slow boot and a broken first line.
    """
    seed = ConfigFactory.build(PROFILE)
    llm = OllamaLLM(seed.ctx, model=OLLAMA_MODEL, base_url=OLLAMA_URL)
    await llm.warmup()


def build(hello: Hello, transport: BaseTransport) -> AgentRuntime:
    if hello.npc_id == HEALTHCHECK_NPC_ID:
        runtime = ConfigFactory.build(PROFILE, transport=transport)
        _apply_character(runtime, hello)
        return runtime

    # Throwaway build purely to obtain a context; its default
    # LoopbackTransport is never started and never touches `transport`.
    seed = ConfigFactory.build(PROFILE)
    processors = [
        StubSTT(seed.ctx),
        OllamaLLM(
            seed.ctx,
            model=OLLAMA_MODEL,
            base_url=OLLAMA_URL,
            num_predict=NUM_PREDICT,
        ),
        StubTTS(seed.ctx),
    ]

    runtime = ConfigFactory.build(PROFILE, transport=transport, processors=processors)
    for processor in processors:
        processor.ctx = runtime.ctx  # rebind to the context that is actually wired

    _apply_character(runtime, hello)
    LOG.info("npc %s -> ollama %s @ %s", hello.npc_id, OLLAMA_MODEL, OLLAMA_URL)
    return runtime
