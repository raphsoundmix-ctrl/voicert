#!/usr/bin/env python3
"""Container entrypoint for the VoiceRT engine bridge.

Why this exists instead of ``python -m voicert.game.bridge`` as the CMD:

1. Signals. ``voicert.game.bridge.main`` awaits ``serve_forever()`` with
   no signal handling. As PID 1 in a container that is actively wrong --
   the kernel does not deliver a signal to PID 1 when that signal still
   carries its *default* disposition, so SIGTERM from ``docker stop`` is
   dropped on the floor and Docker falls through to SIGKILL after the
   grace period. Installing an explicit handler makes PID 1 signalable
   and lets asyncio unwind: stop accepting, let live NPC turns drain,
   close the listener, exit 0.

2. Binding. Inside the container the listener must bind 0.0.0.0 -- the
   published-port proxy dials the container's own interface address, not
   its loopback. The security boundary is the *publish* spec in
   docker-compose.yml (``127.0.0.1:8765:8765``), never this bind.

3. A seam for real providers. ``EngineBridgeServer`` takes a
   ``runtime_factory``; its default builds the stub NPC profile. Set
   ``VOICERT_RUNTIME_FACTORY=module:callable`` to point at your own.

Environment
-----------
  VOICERT_BIND_HOST        default 0.0.0.0   (see note 2)
  VOICERT_PORT             default 8765
  VOICERT_MAX_SESSIONS     default 8         one live NPC per session
  VOICERT_LOG_LEVEL        default INFO
  VOICERT_RUNTIME_FACTORY  e.g. local_factory:build   (empty = stubs)
  VOICERT_SHUTDOWN_GRACE   default 5.0 seconds to let live turns finish
  VOICERT_WARMUP           default 1; "0"/"false"/"no" skips provider warmup
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import sys

from voicert.game.bridge import EngineBridgeServer

LOG = logging.getLogger("voicert.docker")


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def _load_runtime_factory():  # type: ignore[no-untyped-def]
    """Import ``VOICERT_RUNTIME_FACTORY`` as ``module:callable``.

    Returns ``(factory, module)``; ``(None, None)`` when unset. The module
    comes back too so an optional ``warmup()`` on it can be awaited at boot.
    """
    spec = _env_str("VOICERT_RUNTIME_FACTORY", "")
    if not spec:
        return None, None
    if ":" not in spec:
        raise SystemExit(
            f"VOICERT_RUNTIME_FACTORY must look like 'module:callable', got {spec!r}"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import runtime factory module {module_name!r}: {exc}") from exc
    try:
        factory = getattr(module, attr)
    except AttributeError:
        raise SystemExit(f"module {module_name!r} has no attribute {attr!r}") from None
    if not callable(factory):
        raise SystemExit(f"{spec} is not callable")
    LOG.info("runtime factory: %s", spec)
    return factory, module


async def _warmup(module) -> None:  # type: ignore[no-untyped-def]
    """Await an optional ``warmup()`` on the factory module, best-effort.

    Measured, not guessed: the first NPC line against a cold Ollama blew
    past OllamaLLM's 60 s httpx timeout and the whole turn was lost. The
    model load is not something a container can make faster, but it is
    something the container can pay for at boot instead of in front of the
    player. A failure here is logged and ignored -- a warmup backend being
    down must not stop the bridge from serving the stub path.
    """
    if module is None or not hasattr(module, "warmup"):
        return
    try:
        LOG.info("warming up providers")
        await module.warmup()
        LOG.info("warmup done")
    except Exception as exc:  # noqa: BLE001 - warmup is advisory, never fatal
        LOG.warning("warmup failed (serving anyway): %r", exc)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: no add_signal_handler. Keeps this script runnable
            # natively for parity testing; in the container the Unix path
            # above is the one that runs.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _drain(server: EngineBridgeServer, grace: float) -> None:
    """Give live NPC sessions a bounded chance to finish their turn.

    Closing the listener does not close established connections, and the
    server keeps no handle on its per-connection tasks, so this is a
    best-effort wait: sessions still open when the deadline passes are
    dropped when the process exits.
    """
    if grace <= 0:
        return
    deadline = asyncio.get_running_loop().time() + grace
    while server.sessions > 0 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    if server.sessions > 0:
        LOG.warning("shutdown grace expired with %d session(s) still open", server.sessions)


async def _amain() -> int:
    host = _env_str("VOICERT_BIND_HOST", "0.0.0.0")
    port = _env_int("VOICERT_PORT", 8765)
    max_sessions = _env_int("VOICERT_MAX_SESSIONS", 8)
    grace = _env_float("VOICERT_SHUTDOWN_GRACE", 5.0)

    factory, factory_module = _load_runtime_factory()
    server = EngineBridgeServer(
        host, port, max_sessions=max_sessions, runtime_factory=factory
    )
    await server.start()
    LOG.info("bridge up on %s:%d (max_sessions=%d)", host, server.port, max_sessions)

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    # Backgrounded, not awaited. asyncio.start_server already accepts
    # connections once start() returns, so the healthcheck stays green and
    # the stub path stays usable while a slow model loads -- and a SIGTERM
    # arriving mid-warmup is not stuck behind a 60 s httpx timeout.
    warm: asyncio.Task[None] | None = None
    if _env_str("VOICERT_WARMUP", "1").lower() not in ("0", "false", "no"):
        warm = asyncio.create_task(_warmup(factory_module), name="bridge-warmup")

    serve = asyncio.create_task(server.serve_forever(), name="bridge-serve")
    waiter = asyncio.create_task(stop.wait(), name="bridge-stop")
    done, _pending = await asyncio.wait({serve, waiter}, return_when=asyncio.FIRST_COMPLETED)

    async def _cancel(task: "asyncio.Task[None] | None") -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if serve in done:
        waiter.cancel()
        await _cancel(warm)
        exc = serve.exception()
        if exc is not None:
            LOG.error("bridge stopped on error: %r", exc)
            return 1
        LOG.info("bridge stopped on its own")
        return 0

    LOG.info("signal received; refusing new NPC sessions")
    await _cancel(serve)
    await _cancel(warm)
    await _drain(server, grace)
    await server.stop()
    LOG.info("bridge closed cleanly")
    return 0


def main() -> int:
    logging.basicConfig(
        level=_env_str("VOICERT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        stream=sys.stdout,
    )
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        # Only reachable if a SIGINT lands before the handler is installed.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
