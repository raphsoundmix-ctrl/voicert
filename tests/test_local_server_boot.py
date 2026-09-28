"""The server's own start-up promises: it starts Ollama, and it stays offline."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from voicert.game import local_server


class Alive:
    """A spawned `ollama serve` that is still running."""

    def poll(self) -> int | None:
        return None


def test_ollama_executable_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_server.shutil, "which", lambda _name: r"C:\\tools\\ollama.exe")
    assert local_server.ollama_executable() == r"C:\\tools\\ollama.exe"


def test_ollama_executable_falls_back_to_user_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(local_server.shutil, "which", lambda _name: None)
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert local_server.ollama_executable() == str(exe)


def test_ollama_executable_none_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_server.shutil, "which", lambda _name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert local_server.ollama_executable() is None


@pytest.mark.asyncio
async def test_ensure_ollama_starts_it_when_nothing_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["no Ollama at x (refused)", "no Ollama at x (refused)", None])

    async def fake_is_up(_url: str, _model: str, timeout_s: float = 3.0) -> str | None:
        return next(answers)

    started: list[str] = []

    def fake_start(exe: str) -> Alive:
        started.append(exe)
        return Alive()

    monkeypatch.setattr(local_server, "ollama_is_up", fake_is_up)
    monkeypatch.setattr(local_server, "ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(local_server, "start_ollama", fake_start)

    assert await local_server.ensure_ollama("x", "m", wait_s=5.0) is None
    assert started == ["ollama.exe"]


@pytest.mark.asyncio
async def test_ensure_ollama_does_not_start_for_a_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_is_up(_url: str, _model: str, timeout_s: float = 3.0) -> str | None:
        return "Ollama has no model 'm'; pull it with `ollama pull m`"

    started: list[str] = []
    monkeypatch.setattr(local_server, "ollama_is_up", fake_is_up)
    monkeypatch.setattr(local_server, "start_ollama", lambda exe: started.append(exe))

    problem = await local_server.ensure_ollama("x", "m", wait_s=1.0)
    assert problem is not None and "no model" in problem
    assert started == []          # a missing model is not fixed by another Ollama


@pytest.mark.asyncio
async def test_ensure_ollama_reports_when_it_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_is_up(_url: str, _model: str, timeout_s: float = 3.0) -> str | None:
        return "no Ollama at x (refused)"

    monkeypatch.setattr(local_server, "ollama_is_up", fake_is_up)
    monkeypatch.setattr(local_server, "ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(local_server, "start_ollama", lambda exe: Alive())

    problem = await local_server.ensure_ollama("x", "m", wait_s=0.6)
    assert problem is not None and "did not answer" in problem


def test_offline_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    local_server.offline_models(True)
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_online_flag_leaves_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    local_server.offline_models(False)
    assert "HF_HUB_OFFLINE" not in os.environ


def test_start_ollama_is_detached_and_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every handle goes to the void, and on Windows no console window appears.

    A console window in front of a full-screen game is the whole point of these
    flags, so assert them by name rather than by repeating the expression.
    """
    calls: list[dict] = []
    sentinel = object()

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"args": args, **kwargs})
        return sentinel

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert local_server.start_ollama("ollama.exe") is sentinel   # the caller needs the handle
    call = calls[0]
    assert call["args"] == ["ollama.exe", "serve"]
    assert call["stdin"] is subprocess.DEVNULL
    assert call["stdout"] is subprocess.DEVNULL
    assert call["stderr"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert call["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert call["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP


@pytest.mark.asyncio
async def test_ensure_ollama_reports_an_immediate_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A serve that dies at once (the port is held by an Ollama we cannot see)
    must be reported with its exit code, not waited out."""

    async def fake_is_up(_url: str, _model: str, timeout_s: float = 3.0) -> str | None:
        return "no Ollama at x (refused)"

    class Dead:
        def poll(self) -> int:
            return 1

    monkeypatch.setattr(local_server, "ollama_is_up", fake_is_up)
    monkeypatch.setattr(local_server, "ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(local_server, "start_ollama", lambda exe: Dead())

    problem = await local_server.ensure_ollama("x", "m", wait_s=30.0)
    assert problem is not None and "exited immediately with code 1" in problem


@pytest.mark.asyncio
async def test_ensure_ollama_survives_a_failed_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_is_up(_url: str, _model: str, timeout_s: float = 3.0) -> str | None:
        return "no Ollama at x (refused)"

    def boom(_exe: str) -> None:
        raise OSError("access is denied")

    monkeypatch.setattr(local_server, "ollama_is_up", fake_is_up)
    monkeypatch.setattr(local_server, "ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(local_server, "start_ollama", boom)

    problem = await local_server.ensure_ollama("x", "m", wait_s=1.0)
    assert problem is not None and "starting it failed too" in problem


def test_offline_is_set_before_the_hub_is_imported() -> None:
    """The contract is an ordering, and it cannot be checked in this process.

    ``huggingface_hub`` reads HF_HUB_OFFLINE once, at import. Setting it after
    faster-whisper has pulled the hub in would be a no-op that still logs
    "offline" — so run the real entry point in a fresh interpreter and look.
    """
    source = (
        "import sys, os\n"
        "sys.argv = ['local_server', '--help']\n"
        "from voicert.game import local_server\n"
        "assert 'huggingface_hub' not in sys.modules, 'the hub was imported at module scope'\n"
        "local_server.offline_models(True)\n"
        "import huggingface_hub as h\n"
        "from huggingface_hub import constants\n"
        "assert constants.HF_HUB_OFFLINE, 'the hub came up online'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True, text=True,
        env={**os.environ, "HF_HUB_OFFLINE": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# --------------------------------------------------------------- on the GPU


class _Stack:
    """Just the two fields the check reads."""

    def __init__(self, provider: str, device: str) -> None:
        self.resolved_provider = provider
        self.device = device


def test_gpu_problems_silent_when_everything_is_on_the_gpu() -> None:
    assert local_server.gpu_problems(_Stack("cuda", "cuda")) == []


def test_gpu_problems_names_a_cpu_voice() -> None:
    problems = local_server.gpu_problems(_Stack("cpu", "cuda"))
    assert len(problems) == 1
    assert "Kokoro" in problems[0] and "cpu" in problems[0]
    # The int8 trap is the reason this happens most often, so it must be named.
    assert "int8" in problems[0]


def test_gpu_problems_names_a_cpu_recogniser() -> None:
    problems = local_server.gpu_problems(_Stack("cuda", "cpu"))
    assert len(problems) == 1 and "recognition" in problems[0]


@pytest.mark.asyncio
async def test_llm_on_gpu_accepts_a_fully_resident_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ps(monkeypatch, [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 6_000_000_000}])
    ok, note = await local_server.llm_on_gpu("http://x", "qwen3:8b")
    assert ok and "GPU" in note


@pytest.mark.asyncio
async def test_llm_on_gpu_rejects_a_cpu_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ps(monkeypatch, [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 0}])
    ok, note = await local_server.llm_on_gpu("http://x", "qwen3:8b")
    assert not ok and "CPU" in note


@pytest.mark.asyncio
async def test_llm_on_gpu_rejects_a_split_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half on the card is not on the card: it is the slow path with a good name."""
    _fake_ps(monkeypatch, [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 3_000_000_000}])
    ok, note = await local_server.llm_on_gpu("http://x", "qwen3:8b")
    assert not ok and "50%" in note


@pytest.mark.asyncio
async def test_llm_on_gpu_reports_a_model_that_is_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ps(monkeypatch, [])
    ok, note = await local_server.llm_on_gpu("http://x", "qwen3:8b")
    assert not ok and "not loaded" in note


def _fake_ps(monkeypatch: pytest.MonkeyPatch, models: list[dict]) -> None:
    """Stand in for httpx: GET /api/ps returns these models."""

    class Response:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"models": models}

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, _url: str) -> Response:
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
