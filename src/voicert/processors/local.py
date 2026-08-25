"""Fully local providers — no cloud, no API keys, no network beyond localhost.

Each class imports its runtime lazily inside ``__init__`` so the core stays
dependency-free and a missing package produces one clear sentence instead of
an ImportError at module load. Install what you actually use::

    pip install -e ".[local-stt]"    # faster-whisper
    pip install -e ".[local-llm]"    # httpx, talks to Ollama or llama.cpp
    pip install -e ".[local-tts]"    # sherpa-onnx (Piper or Kokoro voices)

Latency is dominated by three serial waits: how long the user's utterance is
(you cannot transcribe what has not been said), the LLM's time to first token,
and the TTS time to first chunk. Only the last two are yours to optimize,
which is why the LLM streams and the TTS synthesizes per sentence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from voicert.context import RuntimeContext
from voicert.frames import AudioFrame, Frame, TextFrame
from voicert.processors.base import LLMService, STTService, TTSService

logger = logging.getLogger("voicert.processors.local")

_MISSING = (
    "{cls} needs {pkg}. Install it with: pip install -e \".[{extra}]\"\n"
    "  (or: pip install {pkg})"
)


class WhisperSTT(STTService):
    """faster-whisper, on your own GPU or CPU.

    Expects one **whole utterance** per AudioFrame — see
    ``voicert.utterance.UtteranceSegmenter``, which is what turns a live
    microphone into utterances. Handing Whisper individual 20 ms chunks
    produces confident nonsense, because every chunk looks like a complete
    (very short) sentence to it.

    Model sizes, English-only, on the ``.en`` variants:
      ``tiny.en``  ~75 MB   fastest, noticeably weaker on names
      ``base.en``  ~145 MB  the usual starting point for NPC dialogue
      ``small.en`` ~484 MB  better with accents, still comfortably real time on a GPU

    ``compute_type="int8"`` is the CPU default; on an NVIDIA card use
    ``device="cuda"`` with ``compute_type="float16"``.
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
    ) -> None:
        super().__init__(ctx)
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                _MISSING.format(cls="WhisperSTT", pkg="faster-whisper", extra="local-stt")
            ) from exc
        self.language = language
        # beam_size=1 (greedy) is the right default for conversation: a beam
        # search buys accuracy the player will not notice and latency they will.
        self.beam_size = beam_size
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("whisper %s loaded on %s (%s)", model_size, device, compute_type)

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
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=False,  # our own VAD already segmented this
        )
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
        timeout_s: float = 60.0,
        keep_alive: str | int = -1,
        options: dict[str, Any] | None = None,
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
            **(options or {}),
        }
        self.timeout_s = timeout_s
        # Ollama evicts an idle model after ~5 minutes by default. In a game
        # that means the first NPC to speak after a quiet stretch pays the
        # full cold load — measured here at 7.5 s for a 14B, against ~350 ms
        # warm. -1 pins the model in VRAM for as long as the process lives.
        self.keep_alive = keep_alive

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
                        yield TextFrame(text=chunk, role="assistant", final=False)
                    if obj.get("done"):
                        return


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
                cut = self._buf.rfind(" ", 0, self.max_chars) or self.max_chars
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

    sherpa-onnx runs Piper (VITS) and Kokoro voices offline on Windows,
    Linux, macOS, Android and iOS. **The runtime's license is not the
    voice's license** — check each model's own terms before shipping one in
    a game. The repo README's licensing section covers the traps.

    Point ``model`` at the ``.onnx`` and ``tokens`` at its ``tokens.txt``.
    For Piper voices ``data_dir`` is the espeak-ng data folder.
    """

    name = "tts-sherpa-onnx"

    def __init__(
        self,
        ctx: RuntimeContext,
        model: str,
        tokens: str,
        data_dir: str = "",
        speaker_id: int = 0,
        speed: float = 1.0,
        num_threads: int = 2,
    ) -> None:
        super().__init__(ctx)
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                _MISSING.format(cls="SherpaOnnxTTS", pkg="sherpa-onnx", extra="local-tts")
            ) from exc
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=model, tokens=tokens, data_dir=data_dir
                ),
                num_threads=num_threads,
            )
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self.speaker_id = speaker_id
        self.speed = speed
        logger.info("sherpa-onnx tts loaded: %s @ %d Hz", model, self._tts.sample_rate)

    async def synthesize(self, text: str) -> AsyncIterator[AudioFrame]:
        if not text.strip():
            return
        pcm, rate = await asyncio.to_thread(self._synthesize_blocking, text)
        if pcm:
            yield AudioFrame(pcm=pcm, source="agent", sample_rate=rate)

    def _synthesize_blocking(self, text: str) -> tuple[bytes, int]:
        import numpy as np

        audio = self._tts.generate(text, sid=self.speaker_id, speed=self.speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        # Clip before scaling: a model that overshoots 1.0 would wrap around
        # and turn a loud vowel into a crack.
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        return pcm16.tobytes(), int(audio.sample_rate)
