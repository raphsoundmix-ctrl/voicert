"""The engine bridge, served by the local GPU stack — no cloud, no API keys.

    python -m voicert.game.local_server --port 8767

Whisper on the GPU hears the player, a local model answers through Ollama, and
Kokoro speaks the reply; Unity connects over the bridge protocol and gets
``READY`` with the voice's real sample rate. Everything is loaded and warmed
*before* the listener opens, so the first NPC a player walks up to answers as
fast as the tenth — the readiness line on stdout is what a launcher should wait
for::

    VOICERT READY 127.0.0.1:8767 rate=24000 voices=54

Requires Ollama with the model pulled. If nothing answers on its port the
server starts ``ollama serve`` itself — a game must not ask the player to open
a terminal — and if the model is missing the preflight says so plainly instead
of letting the first turn time out.

Once the models are on disk nothing here touches the network: ``HF_HUB_OFFLINE``
is set unless ``--online`` is given, because faster-whisper otherwise asks
huggingface.co for the model's revision on every load, files or no files.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from voicert import qos
from voicert.game.bridge import EngineBridgeServer
from voicert.game.local_stack import LocalStack
from voicert.transport import VADConfig

logger = logging.getLogger("voicert.game.local_server")

DEFAULT_TTS_DIR = Path(r"D:\VOICE RT\_shared\models\tts\kokoro-multi-lang-v1_0")
#: src/voicert/game/local_server.py -> repository root
_REPO = Path(__file__).resolve().parents[3]
DEFAULT_WORLD = _REPO / "examples" / "worlds" / "harbour-town.json"
#: Outside the repository on purpose: what a character remembers about a player
#: is save data, not source, and it must survive moving or reinstalling the game.
DEFAULT_STATE_DIR = Path.home() / ".voicert" / "memory"


async def ollama_is_up(base_url: str, model: str, timeout_s: float = 3.0) -> str | None:
    """``None`` when Ollama can serve ``model``, else a sentence for the user."""
    try:
        import httpx
    except ImportError:
        return "httpx is not installed (pip install -e \".[local-llm]\")"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            names = {m.get("name", "") for m in response.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable", and why
        return f"no Ollama at {base_url} ({exc!r}); start it with `ollama serve`"
    if model not in names and f"{model}:latest" not in names:
        return f"Ollama has no model {model!r}; pull it with `ollama pull {model}`"
    return None


def ollama_executable() -> str | None:
    """Where Ollama is installed, or ``None``. PATH first; then the per-user
    install location its Windows installer uses, which is not always on PATH
    for a process started by another program."""
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def start_ollama(exe: str) -> "subprocess.Popen[bytes]":
    """``ollama serve`` as a detached, windowless process. It is left running on
    purpose: the next launch attaches to it, and killing it would unload the
    model for nothing. The handle comes back so the caller can notice it dying
    instead of waiting out the whole timeout."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [exe, "serve"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


async def ensure_ollama(base_url: str, model: str, *, wait_s: float = 40.0) -> str | None:
    """``ollama_is_up``, but starts Ollama first when nothing answers.

    Ollama on Windows usually runs as a tray app from login; "usually" is what
    turns a demo silent on somebody else's machine.
    """
    problem = await ollama_is_up(base_url, model)
    if problem is None or not problem.startswith("no Ollama"):
        return problem
    exe = ollama_executable()
    if exe is None:
        return f"{problem}; and it is not installed on this machine"
    logger.info("nothing answers at %s; starting `%s serve`", base_url, exe)
    try:
        process = start_ollama(exe)
    except OSError as exc:
        return f"{problem}; starting it failed too ({exc})"
    deadline = time.monotonic() + wait_s
    while True:
        await asyncio.sleep(0.5)
        problem = await ollama_is_up(base_url, model, timeout_s=1.0)
        if problem is None or not problem.startswith("no Ollama"):
            return problem
        code = process.poll()
        if code is not None:
            # Usually the port is already held by an Ollama we cannot see, or the
            # GPU driver refused it. Either way, waiting out the timeout hides why.
            return f"{problem}; `ollama serve` exited immediately with code {code}"
        if time.monotonic() > deadline:
            return f"{problem}; it was started but did not answer within {wait_s:.0f} s"


async def llm_on_gpu(base_url: str, model: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Whether Ollama actually holds this model in VRAM.

    ``/api/ps`` reports ``size`` and ``size_vram`` per loaded model. A model
    Ollama chose to run on the CPU — no room, no CUDA — reports ``size_vram``
    of zero, or a fraction of ``size`` when it split the layers.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/ps")
            response.raise_for_status()
            loaded = response.json().get("models", [])
    except Exception as exc:  # noqa: BLE001 — an unreadable answer is "cannot confirm"
        return False, f"could not ask Ollama what it loaded ({exc!r})"
    for entry in loaded:
        name = str(entry.get("name", ""))
        if name not in (model, f"{model}:latest"):
            continue
        total = int(entry.get("size", 0) or 0)
        vram = int(entry.get("size_vram", 0) or 0)
        if vram <= 0:
            return False, f"{name} is loaded on the CPU (size_vram=0)"
        if total and vram < total * 0.95:
            share = 100.0 * vram / total
            return False, f"{name} is only {share:.0f}% on the GPU"
        return True, f"{name} fully on the GPU ({vram / 1e9:.2f} GB VRAM)"
    return False, f"{model} is not loaded"


def gpu_problems(stack: LocalStack) -> list[str]:
    """What is not running on the GPU that was asked to be. Empty means all of it."""
    problems: list[str] = []
    if stack.resolved_provider != "cuda":
        problems.append(
            f"the voice (Kokoro) resolved to '{stack.resolved_provider}', not cuda — "
            "install the sherpa-onnx CUDA wheel, and use the fp32 model "
            "(the int8 one has no CUDA kernels and is forced to the CPU)"
        )
    if stack.device != "cuda":
        problems.append(f"speech recognition is on '{stack.device}', not cuda")
    return problems


def offline_models(enabled: bool) -> None:
    """Keep the Hub client off the network. Must run before anything imports
    huggingface_hub (faster-whisper does, lazily, in ``LocalStack.load``).

    Assignment, not ``setdefault``: an environment that already carries
    ``HF_HUB_OFFLINE=""`` or ``0`` would otherwise keep it and go online while
    this process logged that it was offline. ``--online`` is the way to ask for
    the network, and it is the only way.
    """
    if enabled:
        os.environ["HF_HUB_OFFLINE"] = "1"


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def once(sig: int, _frame: object) -> None:
        """Ask for a graceful stop, then give the signal back to Python.

        Without restoring the default handler there is no way out of a shutdown
        that hangs: a second Ctrl+C would only set an event nobody is waiting on.
        """
        signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT else signal.SIG_DFL)
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows has no add_signal_handler; the C handler runs on its own
            # thread, so hop back to the loop before touching the event.
            signal.signal(sig, once)


async def serve(args: argparse.Namespace) -> int:
    if not args.no_qos:
        qos.foreground_qos(args.pcores)

    problem = await ensure_ollama(args.ollama_url, args.llm)
    if problem is not None:
        logger.error("%s", problem)
        return 2
    # The real value, not the intent: HF_HUB_OFFLINE may have been set already.
    offline = os.environ.get("HF_HUB_OFFLINE", "0")
    logger.info("models: %s", "online (downloads allowed)" if offline in ("", "0")
                else f"offline (HF_HUB_OFFLINE={offline}; pass --online to fetch)")

    stack = LocalStack(
        tts_dir=Path(args.tts_dir), whisper_size=args.whisper, device=args.device,
        compute_type=args.compute_type, num_threads=args.threads, provider=args.tts_provider,
        llm_model=args.llm, ollama_url=args.ollama_url, num_predict=args.num_predict,
        default_voice=args.voice,
        world_file=Path(args.world) if args.world else None,
        state_dir=None if args.no_memory else Path(args.state_dir),
        memory_messages=args.memory_messages,
        partial_stt=not args.no_partial_stt,
    )
    if args.forget:
        logger.info("forgot %d stored conversation(s)", stack.registry.wipe())
    try:
        stack.load()
    except OSError as exc:
        # Offline and the model is not in the cache. The traceback is true but
        # unreadable; the one thing the user has to do fits on a line.
        logger.error(
            "could not load the local models (%s: %s). If this is a fresh install, "
            "run the server once with --online to fetch them.", type(exc).__name__, exc,
        )
        return 2
    await stack.warmup()

    # Generation must happen on the local GPU. Everything below still *works* on
    # a CPU, several times slower, without saying so — which is the one outcome
    # worth refusing outright.
    problems = gpu_problems(stack)
    llm_ok, llm_note = await llm_on_gpu(args.ollama_url, args.llm)
    if not llm_ok:
        problems.append(f"the language model is not on the GPU: {llm_note}")
    if problems:
        for problem in problems:
            logger.error("%s", problem)
        if not args.allow_cpu:
            logger.error("refusing to start on the CPU; pass --allow-cpu to override")
            return 3
        logger.warning("--allow-cpu given: starting anyway, expect several times the latency")
    else:
        logger.info("all three stages on the GPU (%s)", llm_note)

    if not args.no_qos:
        # Ollama's runner process only exists once a model is loaded.
        logger.info("ollama after warm-up: %s", qos.boost_ollama())

    server = EngineBridgeServer(
        args.host, args.port, max_sessions=args.max_sessions,
        runtime_factory=stack.runtime_factory,
        vad=VADConfig(sensitivity=args.vad_sensitivity, hangover_ms=args.vad_hangover_ms),
    )
    await server.start()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _install_signal_handlers(loop, stop)
    # The launcher waits for this line, not for the port: a port that answers
    # before the models are warm would hand the first player a cold turn.
    print(
        f"VOICERT READY {args.host}:{server.port} rate={stack.sample_rate} "
        f"voices={len(stack.voices)} npcs={len(stack.registry.world.personas)} "
        f"memory={'off' if args.no_memory else args.state_dir} "
        f"tts={stack.resolved_provider} stt={stack.device} "
        f"llm={'gpu' if llm_ok else 'cpu'}",
        flush=True,
    )
    await stop.wait()
    logger.info("stopping (%d session(s) open)", server.sessions)
    await server.stop()
    # A session cancelled mid-turn never reaches its recorder's close(), and the
    # editor's launcher stops this process with Kill(). Write what is held.
    stack.registry.save_all()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767, help="8765/8766 are often taken on this machine")
    ap.add_argument("--tts-dir", default=str(DEFAULT_TTS_DIR), help="sherpa-onnx Kokoro directory (fp32)")
    ap.add_argument("--tts-provider", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--voice", default="default", help="fallback voice when HELLO does not name one")
    ap.add_argument("--whisper", default="small.en",
                    help="tiny.en/base.en/small.en — see examples/stt_bench.py for the "
                         "accuracy and latency each one costs")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu", "auto"])
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--llm", default="qwen3:8b",
                    help="a smaller model answers sooner and stops being the character: "
                         "qwen3:1.7b explained quantum entanglement to a night guard")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--num-predict", type=int, default=60, help="cap on reply length; an NPC does not monologue")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads per stage (never more than half the machine)")
    ap.add_argument("--pcores", type=int, default=16, help="pin to the first N logical CPUs; 0 = off")
    ap.add_argument("--no-qos", action="store_true", help="leave Windows scheduling alone")
    ap.add_argument("--max-sessions", type=int, default=8)
    ap.add_argument("--world", default=str(DEFAULT_WORLD),
                    help="authored characters and shared lore; empty string to take them from HELLO")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                    help="where each character's memory of the player is kept")
    ap.add_argument("--memory-messages", type=int, default=24,
                    help="messages replayed verbatim before older ones become facts")
    ap.add_argument("--no-memory", action="store_true",
                    help="never read or write memory: every conversation starts as the first")
    ap.add_argument("--forget", action="store_true",
                    help="wipe stored memory at startup, then run normally")
    ap.add_argument("--no-partial-stt", action="store_true",
                    help="do not transcribe an utterance while it is still being spoken")
    ap.add_argument("--vad-hangover-ms", type=int, default=450,
                    help="silence that ends an utterance. Costs this much latency on every "
                         "open-mic turn; push-to-talk sends ENDPOINT instead and skips it")
    ap.add_argument("--vad-sensitivity", type=float, default=0.7,
                    help="0..1, higher triggers on quieter speech")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="start even when a stage fell back to the CPU. Off by default: "
                         "the voice is meant to be generated on the local GPU, and a silent "
                         "fallback is slower without ever saying why")
    ap.add_argument("--online", action="store_true",
                    help="let the model loaders reach the network (first install only)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="", help="write all output here instead of stdout")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_file:
        # A launcher that reads our stdout through a pipe (the Unity editor, say)
        # can have that pipe closed under it — a domain reload disposes the Process
        # object — and the next line we print kills this server with a broken pipe.
        # Owning the file ourselves makes the server independent of who started it.
        stream = open(args.log_file, "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    logging.basicConfig(level=args.log_level.upper(), format="%(name)s  %(message)s")
    offline_models(not args.online)
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
