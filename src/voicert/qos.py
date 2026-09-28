"""Scheduling a local inference process needs on Windows to keep the GPU fed.

Windows 11 power-throttles background processes (EcoQoS) and prefers the
efficiency cores for them. A realtime voice pipeline is *launch-bound* —
thousands of small CUDA kernels per sentence — so a throttled CPU side cannot
keep the GPU's queue full, the driver reads the idle queue as "nothing to do"
and drops the card to its lowest power state **while it is working**. Every
kernel then takes ten times longer.

Measured on this machine (i9-12900K, 8 P-cores + 8 E-cores, RTX 4080) on
2026-09-09, one NPC turn end to end:

    default background scheduling      738 - 1215 ms   GPU at 210-780 MHz
    + above-normal priority            unchanged
    + a side process holding P2 clocks  577 -  679 ms  (contention, not a fix)
    pinned to the P-cores               348 -  415 ms  GPU at 1100-1260 MHz

A shipped game is a foreground process and never sees the worst of this, but
its inference threads still deserve the same treatment; a headless server
started from a console or a service must ask for it explicitly.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any

logger = logging.getLogger("voicert.qos")

PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_INFO = 4  # PROCESS_INFORMATION_CLASS
PROCESS_SET_INFORMATION = 0x0200


def _win32_qos() -> tuple[Any, Any]:
    """A privately typed ``kernel32`` plus the "throttling off" payload.

    The prototypes are not optional: ``GetCurrentProcess`` returns the
    pseudo-handle ``(HANDLE)-1``, and without ``restype`` ctypes hands the
    callee a zero-extended ``0x00000000FFFFFFFF``, so the call fails with
    ERROR_INVALID_HANDLE and the throttling stays on.
    """
    import ctypes.wintypes as wt

    class PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]

    # A private handle: never set argtypes on the shared ctypes.windll.kernel32.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k32.SetProcessInformation.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    return k32, PowerThrottling(
        PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
        0,  # StateMask 0 = throttling off
    )


def foreground_qos(pcores: int = 16) -> None:
    """Ask Windows to schedule this process like a foreground game.

    Turns EcoQoS off, raises the priority class, and pins the process to the
    first ``pcores`` logical CPUs — the performance cores come first on
    Alder/Raptor Lake. ``pcores=0`` leaves the affinity alone; a machine with
    no more logical CPUs than that is left alone too. A no-op off Windows.
    """
    if sys.platform != "win32":
        return
    k32, off = _win32_qos()
    ok = bool(
        k32.SetProcessInformation(
            k32.GetCurrentProcess(), PROCESS_POWER_THROTTLING_INFO, ctypes.byref(off), ctypes.sizeof(off)
        )
    )
    if not ok:
        logger.warning(
            "could not disable power throttling for this process: WinError %d", ctypes.get_last_error()
        )
    try:
        import psutil
    except ImportError:
        logger.warning(
            "psutil is missing: priority and affinity untouched, so this process keeps "
            'background scheduling (install the "bench" extra)'
        )
        return
    psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    pinned = bool(pcores) and (psutil.cpu_count(logical=True) or 0) > pcores
    if pinned:
        psutil.Process().cpu_affinity(list(range(pcores)))
    logger.info(
        "foreground QoS: power throttling off=%s, priority above normal, pinned to %s cores, ollama %s",
        ok, pcores if pinned else "all", boost_ollama(),
    )


def boost_ollama() -> list[str]:
    """Take the Ollama process *tree* out of EcoQoS and raise its priority.

    The model does not run in ``ollama.exe`` — that is the HTTP front. Inference
    happens in its child ``llama-server.exe``, which the obvious name filter
    misses, and neither the priority class nor the throttling state is inherited
    by children. The runner only exists once a model is loaded, so call this
    again after the first warm-up. Returns the processes actually changed.
    """
    if sys.platform != "win32":
        return []
    try:
        import psutil
    except ImportError:
        return []
    k32, off = _win32_qos()
    boosted: list[str] = []
    seen: set[int] = set()
    for proc in psutil.process_iter(["name"]):
        if not (proc.info["name"] or "").lower().startswith("ollama"):
            continue
        try:
            tree = [proc, *proc.children(recursive=True)]
        except psutil.Error:
            continue
        for p in tree:
            # The tray app and the server are each other's relatives, so the
            # walk revisits them; conhost is the console window, not compute.
            if p.pid in seen:
                continue
            seen.add(p.pid)
            try:
                if p.name().lower() == "conhost.exe":
                    continue
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
                handle = k32.OpenProcess(PROCESS_SET_INFORMATION, False, p.pid)
                if not handle:
                    logger.info(
                        "ollama %s (%d): priority raised, cannot open for power throttling (WinError %d)",
                        p.name(), p.pid, ctypes.get_last_error(),
                    )
                    continue
                applied = bool(
                    k32.SetProcessInformation(
                        handle, PROCESS_POWER_THROTTLING_INFO, ctypes.byref(off), ctypes.sizeof(off)
                    )
                )
                k32.CloseHandle(handle)
                if applied:
                    boosted.append(f"{p.name()}:{p.pid}")
                else:
                    logger.info(
                        "ollama %s (%d): power throttling not disabled (WinError %d)",
                        p.name(), p.pid, ctypes.get_last_error(),
                    )
            except psutil.Error as exc:
                logger.info("skipping ollama pid %d: %s", p.pid, exc)
    return boosted
