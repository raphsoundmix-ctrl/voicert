"""Run a command and sample GPU utilisation, VRAM and CPU load while it runs.

The MVP's resource rule is "never more than half the machine": keep the
RTX 4080 under 50 % utilisation and 8 GB of VRAM, and the CPU under 50 %.
This wraps any command (normally ``run_local_npc.py``) and prints the
percentiles that decide whether the rule held::

    python examples/resmon.py -- python examples/run_local_npc.py --tts kitten

Samples every ~100 ms through ``nvidia-smi`` and psutil; the summary is one
JSON line so it can be pasted into VERSIONS.md.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time

import psutil

GPU_QUERY = ["nvidia-smi", "--id=0",
             "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,clocks.sm",
             "--format=csv,noheader,nounits"]


def _num(field: str) -> float:
    """nvidia-smi prints '[N/A]' or '[Not Supported]' for fields a card lacks."""
    field = field.strip()
    return float("nan") if field.startswith("[") else float(field)


def gpu() -> tuple[float, float, float, float, float]:
    out = subprocess.run(GPU_QUERY, capture_output=True, text=True, check=True).stdout.strip()
    util, used, total, power, sm_mhz = (_num(x) for x in out.splitlines()[0].split(","))
    return util, used, total, power, sm_mhz


def pct(values: list[float], p: float) -> float:
    values = [v for v in values if v == v]  # drop NaN from unsupported fields
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def _finite(values: list[float], default: float = 0.0) -> list[float]:
    return [v for v in values if v == v] or [default]


def main(cmd: list[str]) -> int:
    _, base_vram, total_vram, _, _ = gpu()
    psutil.cpu_percent(None)
    logical = psutil.cpu_count(logical=True) or 1
    # A bare "python" would start the base interpreter: the venv's python.exe is a
    # launcher, and CreateProcess searches the *running* interpreter's directory
    # before PATH. Run the child under the same interpreter as this sampler.
    if os.path.basename(cmd[0]).lower() in ("python", "python.exe", "python3", "python3.exe"):
        cmd = [sys.executable, *cmd[1:]]
    elif os.path.isfile(cmd[0]):
        cmd = [os.path.abspath(cmd[0]), *cmd[1:]]
    child = subprocess.Popen(cmd)
    util, vram, cpu, power, sm = [], [], [], [], []
    sample_errors = 0
    t0 = time.time()
    try:
        while child.poll() is None:
            try:
                u, m, _, w, mhz = gpu()
            except (subprocess.CalledProcessError, ValueError, IndexError):
                # A driver hiccup must not orphan the benchmark or lose the summary.
                sample_errors += 1
            else:
                util.append(u)
                sm.append(mhz)
                vram.append(m)
                power.append(w)
            cpu.append(psutil.cpu_percent(None))
            time.sleep(0.1)
    except BaseException:
        child.kill()
        raise
    finally:
        child.wait()
    summary = {
        "event": "resource_use",
        "seconds": round(time.time() - t0, 1),
        "samples": len(util),
        "gpu_util_p50": pct(util, 50), "gpu_util_p95": pct(util, 95), "gpu_util_max": max(_finite(util)),
        "gpu_util_share_over_50pct": round(sum(1 for u in util if u > 50) / max(1, len(util)), 3),
        "vram_base_mib": base_vram, "vram_max_mib": max(_finite(vram, base_vram)),
        "vram_delta_mib": round(max(_finite(vram, base_vram)) - base_vram),
        "vram_max_share": round(max(_finite(vram)) / total_vram, 3),
        "gpu_sample_errors": sample_errors,
        "gpu_power_w_p95": pct(power, 95),
        # SM clock while working: P8 (~210 MHz) under load means the GPU is being starved, not saved
        "gpu_sm_mhz_p50": pct(sm, 50), "gpu_sm_mhz_min": min(_finite(sm)),
        "cpu_p50": pct(cpu, 50), "cpu_p95": pct(cpu, 95), "cpu_max": max(_finite(cpu)),
        "cpu_logical": logical,
        "cpu_mean": round(statistics.fmean(cpu), 1) if cpu else 0,
    }
    print(json.dumps(summary), flush=True)
    return child.returncode


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        raise SystemExit("usage: resmon.py -- <command ...>")
    raise SystemExit(main(args))
