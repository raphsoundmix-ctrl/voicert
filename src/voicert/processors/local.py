"""Fully local providers — no cloud, no API keys, no network beyond localhost.

Each class imports its runtime lazily inside ``__init__`` so the core stays
dependency-free and a missing package produces one clear sentence instead of
an ImportError at module load. Install what you actually use::

    pip install -e ".[local-stt]"    # faster-whisper
    pip install -e ".[local-llm]"    # httpx, talks to Ollama or llama.cpp
    pip install -e ".[local-tts]"    # sherpa-onnx (Piper or Kokoro voices)
    pip install -e ".[cuda]"         # CUDA 12 runtime DLLs so Whisper and sherpa-onnx can use an NVIDIA GPU

On Windows the CUDA build of sherpa-onnx is a separate wheel (see
``docs/local-stack.md``); ``SherpaOnnxTTS(provider="auto")`` uses it when it
is installed and falls back to the CPU otherwise.

Latency is dominated by three serial waits: how long the user's utterance is
(you cannot transcribe what has not been said), the LLM's time to first token,
and the TTS time to first chunk. Only the last two are yours to optimize,
which is why the LLM streams and the TTS synthesizes per sentence.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import os
import re
import sys
import sysconfig
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from voicert.context import RuntimeContext
from voicert.frames import AudioFrame, Frame, TextFrame
from voicert.processors.base import LLMService, STTService, TTSService

logger = logging.getLogger("voicert.processors.local")

_MISSING = (
    "{cls} needs {pkg}. Install it with: pip install -e \".[{extra}]\"\n"
    "  (or: pip install {pkg})"
)

# Handles from os.add_dll_directory; kept alive so the directories stay registered.
_DLL_HANDLES: list[object] = []


def nvidia_dll_dirs() -> list[str]:
    """The ``nvidia/*/bin`` folders of the pip-installed CUDA packages that exist.

    On Windows the CUDA runtime, cuBLAS, cuDNN, cuFFT and cuRAND ship as
    separate ``nvidia-*-cu12`` wheels, each with a ``bin`` folder that nothing
    puts on PATH. onnxruntime's CUDA provider and CTranslate2 both resolve
    those DLLs through the loader's ordinary search, so they must be on PATH
    before the runtime loads or the GPU silently falls back to the CPU.
    """
    paths = sysconfig.get_paths()
    roots = {paths["purelib"], paths["platlib"]}
    return sorted({d for root in roots for d in glob.glob(os.path.join(root, "nvidia", "*", "bin"))})


def enable_nvidia_dlls() -> list[str]:
    """Register the pip-installed CUDA DLL folders with the Windows loader.

    Idempotent: folders already on PATH are left alone. A no-op off Windows,
    where the wheels ship shared objects with an rpath instead.
    """
    if sys.platform != "win32":
        return []
    dirs = nvidia_dll_dirs()
    on_path = os.environ.get("PATH", "").split(os.pathsep)
    missing = [d for d in dirs if d not in on_path]
    for d in missing:
        _DLL_HANDLES.append(os.add_dll_directory(d))
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, *on_path])
    return dirs


_CUDA_PROVIDER_LIBS = (
    "onnxruntime_providers_cuda.dll",
    "libonnxruntime_providers_cuda.so",
    "libonnxruntime_providers_cuda.dylib",
)


def sherpa_cuda_available(lib_dir: Path | None = None) -> bool:
    """True when the installed sherpa-onnx is a CUDA build.

    The CUDA wheels bundle onnxruntime's CUDA provider next to the extension
    module; the CPU wheels do not. That file is the only reliable tell — the
    Python API looks identical on both.
    """
    if lib_dir is None:
        try:
            import sherpa_onnx
        except ImportError:
            return False
        lib_dir = Path(sherpa_onnx.__file__).resolve().parent / "lib"
    return any((lib_dir / name).exists() for name in _CUDA_PROVIDER_LIBS)


def resolve_tts_provider(requested: str, cuda_build: bool) -> str:
    """``"auto"`` becomes ``"cuda"`` on a CUDA build and ``"cpu"`` otherwise.

    An explicit ``"cuda"`` on a CPU-only build raises instead of passing
    through: sherpa-onnx would quietly log "Fallback to cpu!" to stderr and
    build a CPU session, and every later report — ``self.provider``, the load
    line, the number pasted into a doc — would claim a GPU run that never
    happened.
    """
    if requested == "auto":
        return "cuda" if cuda_build else "cpu"
    if requested not in ("cpu", "cuda"):
        raise ValueError(f"provider must be auto, cpu or cuda, not {requested!r}")
    if requested == "cuda" and not cuda_build:
        raise RuntimeError(
            "provider='cuda' but the installed sherpa-onnx is a CPU build "
            f"(no {_CUDA_PROVIDER_LIBS[0]} next to the extension module). "
            "Install the CUDA wheel — see docs/local-stack.md — or ask for provider='auto'."
        )
    return requested


#: Ops onnxruntime's CUDA provider has no kernels for; their presence means the
#: graph is dynamically quantized and will bounce every matmul through the CPU.
_QUANT_OPS = (b"DynamicQuantizeLinear", b"MatMulInteger", b"ConvInteger")


def is_quantized(model: str) -> bool:
    """True when the ONNX graph contains dynamic-quantization ops.

    Read from the file rather than guessed from its name: the Kitten voice
    ships as ``model.onnx`` and is quantized just like Kokoro's
    ``model.int8.onnx``. ONNX serializes ``graph.node`` before
    ``graph.initializer``, so the op-type strings sit in the first megabytes
    and there is no need to parse the protobuf or import onnx.
    """
    if "int8" in os.path.basename(model).lower():
        return True
    try:
        with open(model, "rb") as fh:
            head = fh.read(8 << 20)
    except OSError:
        return False
    return any(op in head for op in _QUANT_OPS)


def int8_on_cuda(model: str, provider: str) -> bool:
    """A dynamically quantized model asked to run on the GPU — a measured trap.

    onnxruntime's CUDA provider has no kernels for ``DynamicQuantizeLinear``
    / ``MatMulInteger`` / ``ConvInteger``, so every quantized matmul bounces
    to the CPU and back. Measured 2026-09-08 on an RTX 4080: Kokoro int8 RTF
    1.85 under CUDA against 0.44 on the CPU; the fp32 model under CUDA 0.07.
    """
    return provider == "cuda" and is_quantized(model)


class WhisperSTT(STTService):
    """faster-whisper, on your own GPU or CPU.

    Expects one **whole utterance** per AudioFrame — see
    ``voicert.utterance.UtteranceSegmenter``, which is what turns a live
    microphone into utterances. Handing Whisper individual 20 ms chunks
    produces confident nonsense, because every chunk looks like a complete
    (very short) sentence to it.

    Model sizes, English-only, on the ``.en`` variants:
      ``tiny.en``  ~75 MB   fastest, noticeably weaker on names
      ``base.en``  ~145 MB  a reasonable starting point for typed-first demos
      ``small.en`` ~484 MB  the pick for a live microphone

    Measured here (examples/stt_bench.py, ten spoken lines, RTX 4080, float16):
    tiny.en 10.0% word error at 22 ms, base.en 12.0% at 27 ms, small.en 4.6% at
    54 ms. The extra 27 ms is three per cent of a turn; the halved error rate is
    the difference between a character who hears the player's name and one who
    does not.

    ``compute_type="int8"`` is the CPU default; on an NVIDIA card use
    ``device="cuda"`` with ``compute_type="float16"``. ``cpu_threads`` caps
    the CTranslate2 thread pool (the library default is every core, which
    breaks the "never more than half the machine" rule).
    """

    name = "stt-whisper-local"

    def __init__(
        self,
        ctx: RuntimeContext,
        model_size: str = "base.en",
        device: str = "auto",
        compute_type: str = "default",
        language: str | None = "en",
        beam_size: int = 1,
        cpu_threads: int = 4,
        engine: Any | None = None,
        lock: "threading.Lock | None" = None,
    ) -> None:
        super().__init__(ctx)
        self.language = language
        # beam_size=1 (greedy) is the right default for conversation: a beam
        # search buys accuracy the player will not notice and latency they will.
        self.beam_size = beam_size
        #: Held across a transcription when several sessions share one model.
        self._lock = lock
        if engine is not None:
            # A model loaded once per process and shared by every NPC session.
            self._model = engine
            logger.debug("whisper: using a shared model handle")
            return
        if device != "cpu" and not enable_nvidia_dlls() and sys.platform == "win32" and device == "cuda":
            logger.warning(
                "no pip-installed CUDA DLLs found (nvidia/*/bin); install .[cuda] "
                "unless the CUDA toolkit is already on PATH"
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                _MISSING.format(cls="WhisperSTT", pkg="faster-whisper", extra="local-stt")
            ) from exc
        self._model = WhisperModel(
            model_size, device=device, compute_type=compute_type, cpu_threads=cpu_threads
        )
        # Log what CTranslate2 resolved, not what was asked for: with device="auto"
        # the difference between a GPU and a CPU model is otherwise invisible.
        inner = self._model.model
        index = inner.device_index  # CTranslate2 reports a list: it can span several GPUs
        logger.info("whisper %s loaded on %s%s (%s)", model_size, inner.device,
                    "".join(f":{i}" for i in index) if isinstance(index, list) else f":{index}",
                    inner.compute_type)

    async def warmup(self) -> None:
        """Transcribe a second of silence so the first real utterance skips CTranslate2's first-run cost."""
        await asyncio.to_thread(self._transcribe_blocking, bytes(16_000 * 2))

    async def transcribe(self, frame: AudioFrame) -> AsyncIterator[TextFrame]:
        if not frame.pcm:
            return
        text = await asyncio.to_thread(self._transcribe_blocking, frame.pcm)
        if text:
            yield TextFrame(text=text, role="user", final=True)

    def _transcribe_blocking(self, pcm: bytes) -> str:
        import numpy as np

        # int16 -> float32 in [-1, 1], which is what the model wants.
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock or contextlib.nullcontext():
            segments, _info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=False,  # our own VAD already segmented this
            )
            # The generator decodes lazily, so it must be drained inside the lock.
            return " ".join(seg.text.strip() for seg in segments).strip()


class OllamaLLM(LLMService):
    """Any model served by Ollama or llama.cpp, over localhost HTTP.

    Streams tokens, because the first sentence has to reach TTS before the
    model has finished thinking about the last one.

    Model choice is a VRAM negotiation with the game, not a quality contest.
    A 14B model wants ~9 GB, which on a 16 GB card leaves little for the
    renderer. For an NPC that speaks in one or two sentences, a 3B-class
    model at ~2 GB is the honest pick; keep the big model for offline
    dialogue authoring.
    """

    name = "llm-ollama"

    def __init__(
        self,
        ctx: RuntimeContext,
        model: str = "qwen3:1.7b",
        base_url: str = "http://127.0.0.1:11434",
        temperature: float = 0.7,
        num_predict: int = 120,
        repeat_penalty: float = 1.2,
        repeat_last_n: int = 4096,
        no_repeat_sentences: bool = True,
        timeout_s: float = 60.0,
        keep_alive: str | int = -1,
        options: dict[str, Any] | None = None,
        max_sentence_chars: int = 180,
    ) -> None:
        super().__init__(ctx)
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                _MISSING.format(cls="OllamaLLM", pkg="httpx", extra="local-llm")
            ) from exc
        self._httpx = httpx
        self.model = model
        self.base_url = base_url.rstrip("/")
        # num_predict caps the reply: an NPC that monologues is a bug, and an
        # uncapped local model will happily produce 500 tokens of lore.
        self.options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": num_predict,
            # A small model with a short persona prompt loops: it will answer three
            # different questions with the same second sentence. Ollama's default
            # penalty of 1.1 is not enough at this size.
            "repeat_penalty": repeat_penalty,
            # Wide enough to cover the persona and the replayed conversation, so
            # the penalty can see the line the model is about to reuse. Ollama
            # 0.33 rejects -1 for "everything" with a 400, hence a number.
            "repeat_last_n": repeat_last_n,
            **(options or {}),
        }
        self.timeout_s = timeout_s
        # Ollama evicts an idle model after ~5 minutes by default. In a game
        # that means the first NPC to speak after a quiet stretch pays the
        # full cold load — measured here at 7.5 s for a 14B, against ~350 ms
        # warm. -1 pins the model in VRAM for as long as the process lives.
        self.keep_alive = keep_alive
        #: Tokens are held until a sentence is complete — see SentenceBuffer.
        self.max_sentence_chars = max_sentence_chars
        #: Per session: the character does not repeat itself within a conversation.
        self._said = SaidOnce() if no_repeat_sentences else None

    async def warmup(self) -> None:
        """Load the model before the player can talk to anyone.

        Call this while the level is still loading. Without it the very
        first line of dialogue in a session is the slow one.
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": 1},
            "think": False,
        }
        async with self._httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
        logger.info("ollama model %s resident", self.model)

    async def generate(self, messages: list[dict[str, str]]) -> AsyncIterator[Frame]:
        # Ollama streams a token at a time; TTS wants a whole clause. Yielding
        # per token gives every word its own falling intonation and its own
        # leading silence — measured here as a 23-word reply taking 20 s of
        # choppy audio instead of 7 s of speech.
        sentences = SentenceBuffer(self.max_sentence_chars)
        spoken_any = False
        held: str | None = None   # a rejected clause, in case the whole reply is one
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self.options,
            # Qwen3 and friends emit <think>...</think> blocks by default.
            # A talking NPC must not read its own reasoning out loud.
            "think": False,
        }
        async with self._httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("skipping non-JSON line from ollama: %r", line[:80])
                        continue
                    if obj.get("error"):
                        raise RuntimeError(f"ollama error: {obj['error']}")
                    chunk = (obj.get("message") or {}).get("content") or ""
                    if chunk:
                        self.note_first_token()  # the model spoke; the clause is still forming
                    for sentence in sentences.push(chunk) if chunk else ():
                        if self._said is not None and not self._said.accept(sentence):
                            held = held or sentence
                            continue
                        spoken_any = True
                        yield TextFrame(text=sentence + " ", role="assistant", final=False)
                    if obj.get("done"):
                        rest = sentences.flush()
                        if rest and (self._said is None or self._said.accept(rest)):
                            spoken_any = True
                            yield TextFrame(text=rest, role="assistant", final=False)
                        elif rest:
                            held = held or rest
                        if not spoken_any and held:
                            # Everything the model produced this turn was something
                            # it had already said. Repeating is bad; saying nothing
                            # at all looks like a broken NPC, which is worse.
                            yield TextFrame(text=held, role="assistant", final=False)
                        return


class SaidOnce:
    """Refuses a clause this character has already spoken in this conversation.

    The prompt asks for this and the model does not comply; sampler penalties
    move the rate around by less than the run-to-run spread. A set does it
    exactly. Two guards keep it from being annoying: only clauses long enough to
    be a line of dialogue are considered, so "Aye." and "No." stay available, and
    it is scoped to one session, so a character may still have a catchphrase the
    player hears again on their next visit.
    """

    def __init__(self, min_chars: int = 10, remember: int = 60) -> None:
        #: Measured: the line a guard would not stop saying was "Passes only."
        #: at thirteen characters. Ten keeps "Aye." and "No." repeatable.
        self.min_chars = min_chars
        self.remember = remember
        self._said: list[str] = []
        self._seen: set[str] = set()

    @staticmethod
    def _key(sentence: str) -> str:
        letters = "".join(c for c in sentence.lower() if c.isalnum() or c.isspace())
        return " ".join(letters.split())

    def accept(self, sentence: str) -> bool:
        """True if this may be spoken; records it when so."""
        if len(sentence) < self.min_chars:
            return True
        key = self._key(sentence)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._said.append(key)
        if len(self._said) > self.remember:
            self._seen.discard(self._said.pop(0))
        return True


#: Sentence enders followed by whitespace, or the end of the buffer.
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")


class SentenceBuffer:
    """Collects LLM tokens and releases whole sentences.

    Synthesizing per token sounds exactly like it reads: each word gets its
    own falling intonation. TTS wants a full clause to place stress and
    breath. This holds tokens until a sentence boundary, with a length
    escape hatch so a model that forgets punctuation still speaks.
    """

    def __init__(self, max_chars: int = 180) -> None:
        self.max_chars = max_chars
        self._buf = ""

    def push(self, text: str) -> list[str]:
        """Add tokens; return whichever complete sentences are now ready."""
        self._buf += text
        out: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buf)
            if match:
                out.append(self._buf[: match.end()].strip())
                self._buf = self._buf[match.end() :]
                continue
            if len(self._buf) >= self.max_chars:
                # No punctuation in sight: break at the last space so we cut
                # between words rather than mid-word.
                cut = self._buf.rfind(" ", 0, self.max_chars)
                if cut <= 0:  # rfind gives -1 for "no space"; 0 would cut nothing
                    cut = self.max_chars
                out.append(self._buf[:cut].strip())
                self._buf = self._buf[cut:].lstrip()
                continue
            break
        return [s for s in out if s]

    def flush(self) -> str:
        """Whatever is left when the stream ends."""
        rest, self._buf = self._buf.strip(), ""
        return rest


class SherpaOnnxTTS(TTSService):
    """Local neural TTS through sherpa-onnx (Apache-2.0 runtime).

    sherpa-onnx runs Piper (VITS), Kokoro and Kitten voices offline on
    Windows, Linux, macOS, Android and iOS. **The runtime's license is not
    the voice's license** — check each model's own terms before shipping one
    in a game; ``docs/licensing.md`` covers the traps.

    ``model_kind`` picks the model family:

    * ``"vits"``   — Piper-style voices: ``model`` (.onnx), ``tokens``,
      ``data_dir`` (espeak-ng data).
    * ``"kokoro"`` — Kokoro: ``model``, ``voices`` (voices.bin), ``tokens``,
      ``data_dir`` (espeak-ng data), optional ``dict_dir`` and ``lexicon``
      (comma-separated lexicon files).
    * ``"kitten"`` — Kitten: ``model``, ``voices``, ``tokens``, ``data_dir``.

    Synthesis streams: sherpa-onnx hands back audio per sentence through a
    callback, and each chunk is yielded as soon as it exists, so a
    multi-sentence reply starts playing after its first sentence rather
    than after the last. A barge-in cancels the consumer, and the callback
    then returns 0, which stops the synthesis thread instead of letting it
    burn CPU on audio nobody will hear.

    ``provider`` picks where the model runs: ``"cuda"``, ``"cpu"``, or
    ``"auto"`` (CUDA when the installed sherpa-onnx is a CUDA build). Only
    fp32 models gain from the GPU — see ``int8_on_cuda``.

    Measured on this machine 2026-09-08, RTX 4080 / i9-12900K:
      Kokoro fp32, cuda: first chunk 91–175 ms, RTF 0.07–0.11, +0.77 GB VRAM
      Kokoro fp32, cpu:  first chunk 291–505 ms, RTF 0.31–0.44
      Kokoro int8, cpu:  RTF ~0.77;  int8 under cuda: RTF 1.85 (worse than cpu)
      Kitten int8, cpu:  RTF ~0.4
    """

    name = "tts-sherpa-onnx"

    def __init__(
        self,
        ctx: RuntimeContext,
        model: str = "",
        tokens: str = "",
        data_dir: str = "",
        *,
        model_kind: str = "vits",
        engine: Any | None = None,
        lock: "threading.Lock | None" = None,
        voices: str = "",
        lexicon: str = "",
        dict_dir: str = "",
        speaker_id: int = 0,
        speed: float = 1.0,
        num_threads: int = 4,
        max_num_sentences: int = 1,
        provider: str = "auto",
    ) -> None:
        super().__init__(ctx)
        self.speaker_id = speaker_id
        self.speed = speed
        #: Held across a synthesis when several sessions share one engine.
        self._lock = lock
        if engine is not None:
            # An engine loaded once per process; only the speaker id is per session.
            self._tts = engine
            self.output_sample_rate = int(engine.sample_rate)
            self.provider = provider if provider != "auto" else "shared"
            return
        if not model:
            raise ValueError("SherpaOnnxTTS needs a model path (or an already-built engine)")
        if provider != "cpu":
            enable_nvidia_dlls()
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                _MISSING.format(cls="SherpaOnnxTTS", pkg="sherpa-onnx", extra="local-tts")
            ) from exc
        requested, provider = provider, resolve_tts_provider(provider, sherpa_cuda_available())
        if int8_on_cuda(model, provider):
            if requested == "auto":
                # "auto" means the best available for *this* model, and for a
                # quantized graph that is the CPU (measured: RTF 0.44 vs 1.85).
                provider = "cpu"
                logger.info("%s is quantized: auto picks the CPU, which is faster than the "
                            "CUDA provider for quantized ops", model)
            else:
                logger.warning(
                    "%s is quantized: onnxruntime runs its quantized ops on the CPU even under "
                    "the CUDA provider, which is slower than plain CPU. Use the fp32 model on the GPU.",
                    model,
                )
        if model_kind == "vits":
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=model, tokens=tokens, data_dir=data_dir, lexicon=lexicon
                ),
                num_threads=num_threads,
                provider=provider,
            )
        elif model_kind == "kokoro":
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=model, voices=voices, tokens=tokens, data_dir=data_dir,
                    dict_dir=dict_dir, lexicon=lexicon,
                ),
                num_threads=num_threads,
                provider=provider,
            )
        elif model_kind == "kitten":
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
                    model=model, voices=voices, tokens=tokens, data_dir=data_dir
                ),
                num_threads=num_threads,
                provider=provider,
            )
        else:
            raise ValueError(f"model_kind must be vits, kokoro or kitten, not {model_kind!r}")
        config = sherpa_onnx.OfflineTtsConfig(
            model=model_config, max_num_sentences=max_num_sentences
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self.output_sample_rate = int(self._tts.sample_rate)
        self.provider = provider
        logger.info(
            "sherpa-onnx tts loaded: %s %s on %s @ %d Hz, %d speakers",
            model_kind, model, provider, self.output_sample_rate, self._tts.num_speakers,
        )

    async def warmup(self) -> None:
        """Synthesize one word so the first real line skips onnxruntime's first-run cost.

        Arena allocation and, on CUDA, kernel selection happen on the first
        ``generate``. Call this during level load, like ``OllamaLLM.warmup``.
        """
        def run() -> None:
            with self._lock or contextlib.nullcontext():
                self._tts.generate("Ready.", sid=self.speaker_id, speed=self.speed)

        await asyncio.to_thread(run)

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        text = speakable(text)
        if not text.strip():
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        rate = self.output_sample_rate
        cancelled = False

        def on_chunk(samples: Any, progress: float) -> int:
            # sherpa-onnx thread: hand the chunk to the loop, keep going
            # unless the consumer has already been cancelled by a barge-in.
            if cancelled:
                return 0
            loop.call_soon_threadsafe(queue.put_nowait, _pcm16(samples))
            return 1

        def run() -> None:
            try:
                with self._lock or contextlib.nullcontext():
                    self._tts.generate(text, sid=self.speaker_id, speed=self.speed, callback=on_chunk)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        worker = asyncio.ensure_future(asyncio.to_thread(run))
        try:
            while True:
                pcm = await queue.get()
                if pcm is None:
                    break
                if pcm:
                    yield AudioFrame(pcm=pcm, source="agent", sample_rate=rate)
            # The sentinel comes from run()'s finally, so the thread is on its way
            # out: awaiting it costs nothing and re-raises whatever generate() hit
            # (CUDA OOM, a bad speaker id). Without this a failed synthesis is
            # indistinguishable from a finished one — the NPC just goes mute.
            await worker
        finally:
            cancelled = True
            if not worker.done():
                # A barge-in abandoned this synthesis; do not block the loop on the
                # thread, but do not leave its exception unretrieved either.
                worker.add_done_callback(_log_abandoned_synthesis)


def _log_abandoned_synthesis(fut: "asyncio.Future[None]") -> None:
    if not fut.cancelled() and (exc := fut.exception()) is not None:
        logger.warning("tts synthesis failed after barge-in: %r", exc)


def speakable(text: str) -> str:
    """Strip what a lexicon-based TTS cannot say: emoji, pictographs, control chars.

    Small models decorate replies with symbols ("❓", "✨"); sherpa-onnx logs an
    "Unknown token" for each and the lexicon drops them anyway. Letters in any
    script (with their combining marks), digits, whitespace and punctuation pass
    through unchanged, and so do the ASCII symbols espeak-ng actually pronounces
    — "2 + 2 = 4" should not become "2  2  4".
    """
    import unicodedata

    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N", "P", "Z") or ch.isspace():
            out.append(ch)
        elif cat == "Sc" or ch == "\N{DEGREE SIGN}" or (cat[0] == "S" and ch.isascii()):
            out.append(ch)
    return "".join(out)


def _pcm16(samples: Any) -> bytes:
    """float32 [-1, 1] samples -> little-endian PCM16 bytes.

    Clip before scaling: a model that overshoots 1.0 would wrap around and
    turn a loud vowel into a crack.
    """
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    return bytes((np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())
