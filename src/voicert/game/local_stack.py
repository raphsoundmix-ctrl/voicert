"""One process, one set of models, many NPCs.

``EngineBridgeServer`` builds a fresh ``AgentRuntime`` per connection, and a
runtime factory written the obvious way — the one in ``examples/run_local_npc.py``
— constructs its providers inside that call. With the local stack that means
loading a 325 MB voice and a Whisper model *per NPC*: eight NPCs would hold
around nine gigabytes of VRAM to run one conversation.

``LocalStack`` loads the models once, at startup, and hands each session a thin
processor bound to the shared handle. What stays per session is what must:
the runtime context, the dialogue history, the speaker id, and the Ollama
client (whose model lives in the Ollama process anyway).

It also owns the part that is not a model at all — who each character is, what
they are allowed to know, and what they remember about the player (see
``voicert.game.agents``). That belongs here because it is per-process and
shared: the guard has one memory whether the player walks up to him once or
five times.

    stack = LocalStack(tts_dir=Path(...), whisper_size="base.en",
                       world_file=Path("examples/worlds/harbour-town.json"))
    stack.load()
    await stack.warmup()
    server = EngineBridgeServer(host, port, runtime_factory=stack.runtime_factory)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voicert.config import AgentRuntime, ConfigFactory
from voicert.context import RuntimeContext
from voicert.game.agents import (
    DEFAULT_PLAYER,
    AgentRegistry,
    ConversationRecorder,
    MemoryStore,
    NpcAgent,
    NpcPersona,
    World,
    extract_self_facts,
    load_world,
)
from voicert.pipeline import FrameProcessor
from voicert.processors.local import (
    OllamaLLM,
    SherpaOnnxTTS,
    WhisperSTT,
    enable_nvidia_dlls,
    is_quantized,
    resolve_tts_provider,
    sherpa_cuda_available,
)
from voicert.transport import BaseTransport
from voicert.utterance import UtteranceConfig, UtteranceSegmenter

logger = logging.getLogger("voicert.game.local_stack")

#: Speaker order is baked into ``voices.bin``; these tables are the id lists the
#: sherpa-onnx export scripts used (``scripts/kokoro/<version>/generate_voices_bin.py``).
#: Overall grades in the comments are from hexgrad's Kokoro-82M VOICES.md.
KOKORO_V1_0_VOICES: dict[str, int] = {
    "af_alloy": 0, "af_aoede": 1, "af_bella": 2, "af_heart": 3, "af_jessica": 4,
    "af_kore": 5, "af_nicole": 6, "af_nova": 7, "af_river": 8, "af_sarah": 9,
    "af_sky": 10, "am_adam": 11, "am_echo": 12, "am_eric": 13, "am_fenrir": 14,
    "am_liam": 15, "am_michael": 16, "am_onyx": 17, "am_puck": 18, "am_santa": 19,
    "bf_alice": 20, "bf_emma": 21, "bf_isabella": 22, "bf_lily": 23,
    "bm_daniel": 24, "bm_fable": 25, "bm_george": 26, "bm_lewis": 27,
    "ef_dora": 28, "em_alex": 29, "ff_siwis": 30, "hf_alpha": 31, "hf_beta": 32,
    "hm_omega": 33, "hm_psi": 34, "if_sara": 35, "im_nicola": 36, "jf_alpha": 37,
    "jf_gongitsune": 38, "jf_nezumi": 39, "jf_tebukuro": 40, "jm_kumo": 41,
    "pf_dora": 42, "pm_alex": 43, "pm_santa": 44, "zf_xiaobei": 45, "zf_xiaoni": 46,
    "zf_xiaoxiao": 47, "zf_xiaoyi": 48, "zm_yunjian": 49, "zm_yunxi": 50,
    "zm_yunxia": 51, "zm_yunyang": 52, "em_santa": 53,
}

#: v1.1-zh is a Chinese-first release: only three of its 103 voices are English
#: (ids 3..102 are ``zf_001``/``zm_0xx``), which is why the demo uses v1.0.
KOKORO_V1_1_VOICES: dict[str, int] = {"af_maple": 0, "af_sol": 1, "bf_vale": 2}

VOICE_TABLES: dict[str, dict[str, int]] = {
    "kokoro-multi-lang-v1_0": KOKORO_V1_0_VOICES,
    "kokoro-int8-multi-lang-v1_0": KOKORO_V1_0_VOICES,
    "kokoro-multi-lang-v1_1": KOKORO_V1_1_VOICES,
    "kokoro-int8-multi-lang-v1_1": KOKORO_V1_1_VOICES,
}

#: Casting for the demo, so a world file can name a role instead of a model detail.
#: Grades are Kokoro's own: af_heart A, af_bella A-, bf_emma/af_nicole B-,
#: am_fenrir/am_michael/am_puck C+, bm_george/bm_fable C. The male voices are a
#: grade below the best female ones — that is the model, not the wiring.
VOICE_ALIASES: dict[str, str] = {
    "default": "am_michael",   # C+, the most neutral male
    "keeper": "bm_george",     # C, older British innkeeper
    "merchant": "am_fenrir",   # C+, warmer male
    "smith": "am_puck",        # C+, brighter male
    "guard": "bm_fable",       # C, clipped British
    "healer": "af_heart",      # A, the best voice in the model
    "scholar": "bf_emma",      # B-, British female
    "narrator": "af_bella",    # A-
}

#: What a summarizer is asked to do when a conversation outgrows the verbatim
#: window. Deliberately narrow: durable facts about the player, nothing else.
COMPACT_PROMPT = """\
Below is part of a conversation between a character and a player, oldest first.

List only the durable facts the character learned about this player — a name, a
stated intention, something they own, something they promised, something they
were told. One short sentence per fact, each beginning "The player". No
greetings, no small talk, no facts about the character, nothing you had to
guess. If there is nothing durable, answer exactly: NONE
"""


def resolve_speaker_id(voice: str, table: dict[str, int]) -> int:
    """Map a voice name to a Kokoro speaker id.

    Accepts a role from ``VOICE_ALIASES`` ("keeper"), a Kokoro voice name
    ("bm_george") or a bare id ("26"). Anything unknown falls back to the
    table's first voice with a warning — a wrong voice is a better demo than a
    dead NPC, but it should never be silent about it.
    """
    key = (voice or "default").strip()
    name = VOICE_ALIASES.get(key.lower(), key)
    if name in table:
        return table[name]
    if key.isdigit() and int(key) in set(table.values()):
        return int(key)
    fallback = next(iter(table.values()), 0)
    logger.warning("unknown voice %r; falling back to speaker %d", voice, fallback)
    return fallback


def persona_from_hello(hello: Any) -> NpcPersona:
    """A character described entirely by the game, for a world file that has
    never heard of them. Keeps the bridge usable with no authored world."""
    return NpcPersona(
        npc_id=str(getattr(hello, "npc_id", "npc")),
        name=str(getattr(hello, "character", "") or "a character of this world"),
        role="",
        voice=str(getattr(hello, "voice", "default")),
        knows=tuple(
            part.strip() for part in str(getattr(hello, "lore_scope", "")).split(";") if part.strip()
        ),
    )


@dataclass
class LocalStack:
    """The GPU models, loaded once and shared by every NPC session.

    ``tts_dir`` is a sherpa-onnx Kokoro directory (``model.onnx``,
    ``voices.bin``, ``tokens.txt``, ``espeak-ng-data``, and for the multi-lang
    releases ``dict`` plus the lexicons). Use the **fp32** model: a quantized
    graph has no CUDA kernels for its ``MatMulInteger``/``DynamicQuantizeLinear``
    ops and measures slower on the GPU than on the CPU.
    """

    tts_dir: Path
    #: small.en over base.en for a live microphone: measured 4.6% word error
    #: against 12.0% on spoken test lines, for 27 ms and 400 MiB more. Names
    #: are why — a character who mishears "My name is Alex" has nothing to
    #: remember. See examples/stt_bench.py.
    whisper_size: str = "small.en"
    device: str = "cuda"
    compute_type: str = "float16"
    num_threads: int = 4
    provider: str = "auto"
    #: Measured against the demo's own personas: 1.7b invents lore and answers
    #: real-world questions, 4b (Thinking-2507) speaks its reasoning aloud, 8b
    #: holds the role at 5.6 GB of VRAM. See VERSIONS.md, "choosing the model".
    llm_model: str = "qwen3:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    num_predict: int = 60
    default_voice: str = "default"
    #: Utterances arrive whole; without a segmenter Whisper would be handed
    #: 20 ms chunks and answer each one with confident nonsense.
    segment_audio: bool = True
    #: Authored characters and shared lore. Without it, HELLO describes the NPC.
    world_file: Path | None = None
    #: Where each character's memory of the player is kept between sessions.
    state_dir: Path | None = None
    #: Messages replayed verbatim into a new session before facts take over.
    memory_messages: int = 24
    #: Transcribe the utterance in progress so the game can show it being heard.
    partial_stt: bool = True

    #: Filled by ``load``: what the provider actually resolved to.
    resolved_provider: str = field(default="", init=False)
    registry: AgentRegistry = field(init=False, repr=False)
    _whisper: Any = field(default=None, init=False, repr=False)
    _tts: Any = field(default=None, init=False, repr=False)
    #: One GPU, one model handle per stage: serialize so two NPCs answering at
    #: once cannot enter the same engine concurrently.
    _whisper_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _tts_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _compacting: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    #: asyncio keeps only a weak reference to a running task, so a fire-and-forget
    #: create_task can be collected mid-flight. Hold them until they finish.
    _tasks: set[Any] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        world = load_world(self.world_file) if self.world_file else World()
        store = MemoryStore(self.state_dir) if self.state_dir else None
        self.registry = AgentRegistry(world, store, max_messages=self.memory_messages)

    # -- startup -----------------------------------------------------------

    def load(self) -> None:
        """Build both model handles. Blocking, and the slow part of startup."""
        import sherpa_onnx
        from faster_whisper import WhisperModel

        enable_nvidia_dlls()
        model = self.tts_dir / "model.onnx"
        if not model.is_file():
            raise FileNotFoundError(f"no model.onnx in {self.tts_dir}")
        provider = resolve_tts_provider(self.provider, sherpa_cuda_available())
        if provider == "cuda" and is_quantized(str(model)):
            provider = "cpu"
            logger.warning(
                "%s is quantized: running it on the CPU, which is faster than the CUDA "
                "provider for quantized ops. Use the fp32 Kokoro release for the GPU.", model,
            )
        lexicons = [p for p in ("lexicon-us-en.txt", "lexicon-zh.txt") if (self.tts_dir / p).is_file()]
        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=str(model),
            voices=str(self.tts_dir / "voices.bin"),
            tokens=str(self.tts_dir / "tokens.txt"),
            data_dir=str(self.tts_dir / "espeak-ng-data"),
            dict_dir=str(self.tts_dir / "dict") if (self.tts_dir / "dict").is_dir() else "",
            lexicon=",".join(str(self.tts_dir / p) for p in lexicons),
        )
        self._tts = sherpa_onnx.OfflineTts(
            sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=kokoro, num_threads=self.num_threads, provider=provider
                ),
                max_num_sentences=1,  # stream per sentence: first audio must not wait for the last
            )
        )
        self.resolved_provider = provider
        self._whisper = WhisperModel(
            self.whisper_size, device=self.device, compute_type=self.compute_type,
            cpu_threads=self.num_threads,
        )
        logger.info(
            "local stack loaded: kokoro %s on %s @ %d Hz (%d speakers), whisper %s on %s",
            self.tts_dir.name, provider, self.sample_rate, self._tts.num_speakers,
            self.whisper_size, self.device,
        )

    async def warmup(self) -> None:
        """Pay every first-run cost before the first player is listening.

        onnxruntime picks CUDA kernels on the first ``generate`` (measured 380 ms
        against 23 ms warm) and Ollama loads the model on the first request
        (~4 s in process, ~34 s from disk).
        """
        ctx = ConfigFactory.build("npc").ctx
        for stage in self.processors(ctx, voice=self.default_voice):
            if hasattr(stage, "warmup"):
                await stage.warmup()

    @property
    def voices(self) -> dict[str, int]:
        return VOICE_TABLES.get(self.tts_dir.name, KOKORO_V1_0_VOICES)

    @property
    def sample_rate(self) -> int:
        return int(self._tts.sample_rate) if self._tts is not None else 0

    # -- per session -------------------------------------------------------

    def processors(self, ctx: RuntimeContext, *, voice: str) -> list[FrameProcessor]:
        """The three stages for one NPC session, over the shared handles."""
        if self._tts is None or self._whisper is None:
            raise RuntimeError("LocalStack.load() must run before a session is built")
        return [
            WhisperSTT(ctx, engine=self._whisper, lock=self._whisper_lock),
            OllamaLLM(ctx, model=self.llm_model, base_url=self.ollama_url, num_predict=self.num_predict),
            SherpaOnnxTTS(
                ctx, engine=self._tts, lock=self._tts_lock, provider=self.resolved_provider,
                speaker_id=self.speaker_id(voice),
            ),
        ]

    def speaker_id(self, voice: str) -> int:
        """Resolve a voice against the table *and* against the model that is loaded.

        The table is chosen by directory name, so a renamed or swapped model can
        be handed an id it does not have: v1.1-zh has three English voices where
        v1.0 has fifty-four.
        """
        sid = resolve_speaker_id(voice, self.voices)
        available = int(self._tts.num_speakers) if self._tts is not None else sid + 1
        if sid >= available:
            logger.warning("voice %r maps to speaker %d but %s has %d; using speaker 0",
                           voice, sid, self.tts_dir.name, available)
            return 0
        return sid

    def runtime_factory(self, hello: Any, transport: BaseTransport) -> AgentRuntime:
        """Build one NPC session — the callable ``EngineBridgeServer`` expects."""
        npc_id = str(getattr(hello, "npc_id", "npc"))
        player_id = str(getattr(hello, "player_id", DEFAULT_PLAYER))
        agent = self.registry.acquire(
            npc_id, player_id=player_id, fallback=persona_from_hello(hello)
        )
        voice = self._voice_for(agent, hello)
        runtime = ConfigFactory.build(
            "npc", transport=transport,
            processors=lambda ctx: self.processors(ctx, voice=voice),
        )
        if self.segment_audio:
            runtime.transport.segmenter = UtteranceSegmenter(UtteranceConfig(), sample_rate=16_000)

        # The character, in full: role, world lore, and what they remember.
        runtime.ctx.system_prompt = agent.system_prompt()
        replayed = runtime.state.add_history(agent.history())
        recorder = ConversationRecorder(agent, self.registry, runtime.state)
        runtime.on_turn_complete = lambda rt: self._after_turn(recorder, agent, rt)
        runtime.on_session_closed = lambda rt: recorder.close()
        if self.partial_stt:
            runtime.partial_transcriber = self.transcribe_partial

        logger.info(
            "npc %s (%s): voice %s -> speaker %d, %d message(s) replayed, %d fact(s)",
            npc_id, agent.persona.display, voice, self.speaker_id(voice),
            replayed, len(agent.memory.facts),
        )
        return runtime

    def _voice_for(self, agent: NpcAgent, hello: Any) -> str:
        """The world owns a character's voice; HELLO speaks for the ones it
        does not know about."""
        if self.registry.world.persona(agent.npc_id) is not None:
            return agent.persona.voice
        return str(getattr(hello, "voice", "default")) or "default"

    # -- memory ------------------------------------------------------------

    def _after_turn(
        self, recorder: ConversationRecorder, agent: NpcAgent, runtime: AgentRuntime
    ) -> None:
        recorder.flush()
        if self._learn_about_player(agent, runtime):
            # The prompt is assembled once per session, so a fact learned in turn
            # two would not reach turn three without this.
            runtime.ctx.system_prompt = agent.system_prompt()
            self.registry.save(agent)
        key = (agent.npc_id, agent.memory.player_id)
        if agent.memory.overflow > 0 and key not in self._compacting:
            self._compacting.add(key)
            task = asyncio.create_task(self._compact(agent))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _learn_about_player(agent: NpcAgent, runtime: AgentRuntime) -> bool:
        """Pick up what the player said about themselves in this exchange."""
        learned = False
        for turn in reversed(runtime.state.turns):
            if turn.role != "user":
                continue
            for fact in extract_self_facts(turn.text):
                if agent.memory.note_fact(fact):
                    logger.info("%s learned: %s", agent.npc_id, fact)
                    learned = True
            break   # only the turn that just finished
        return learned

    async def _compact(self, agent: NpcAgent) -> None:
        """Turn the oldest messages into durable facts.

        Runs after the reply is already being spoken, so the player never waits
        for it. The messages are detached first: a summarizer that fails must
        still not let the prompt grow, and losing the oldest small talk is a much
        better failure than a turn that times out behind it.
        """
        try:
            old = agent.memory.take_overflow()
            if not old:
                return
            rendered = "\n".join(
                f"{'Player' if m['role'] == 'user' else agent.persona.display}: {m['text']}"
                for m in old
            )
            answer = await self._ask(COMPACT_PROMPT, rendered)
            kept = 0
            for line in (answer or "").splitlines():
                fact = line.strip().lstrip("-*• ").strip()
                if fact and fact.upper() != "NONE" and len(fact) > 8:
                    kept += agent.memory.note_fact(fact)
            logger.info("compacted %d message(s) of %s into %d fact(s)",
                        len(old), agent.npc_id, kept)
            self.registry.save(agent)
        except Exception:  # noqa: BLE001 — memory upkeep must never break a session
            logger.exception("compaction failed for %s", agent.npc_id)
        finally:
            self._compacting.discard((agent.npc_id, agent.memory.player_id))

    async def _ask(self, system: str, user: str, num_predict: int = 200) -> str:
        """One non-streaming call to the same local model. Bookkeeping only."""
        import httpx

        payload = {
            "model": self.llm_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False, "think": False, "keep_alive": -1,
            "options": {"temperature": 0.1, "num_predict": num_predict},
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{self.ollama_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            return str((resp.json().get("message") or {}).get("content") or "").strip()

    # -- partial transcription ---------------------------------------------

    async def transcribe_partial(self, pcm: bytes) -> str | None:
        """Best-effort transcript of an utterance still being spoken.

        Never waits for the model: if the shared Whisper handle is busy — which
        means a real utterance is being transcribed — the preview is skipped.
        Showing the player a word late is fine; delaying the answer is not.
        """
        if self._whisper is None or not pcm:
            return None
        return await asyncio.to_thread(self._partial_blocking, pcm)

    def _partial_blocking(self, pcm: bytes) -> str | None:
        import numpy as np

        if not self._whisper_lock.acquire(blocking=False):
            return None
        try:
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _info = self._whisper.transcribe(
                audio, language="en", beam_size=1, vad_filter=False
            )
            return " ".join(seg.text.strip() for seg in segments).strip() or None
        finally:
            self._whisper_lock.release()
