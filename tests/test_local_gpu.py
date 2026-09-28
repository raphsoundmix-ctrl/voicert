"""GPU plumbing in the local providers: CUDA DLL discovery and provider selection.

Pure functions only — nothing here loads a model or needs a GPU.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from voicert.processors import local


def test_nvidia_dll_dirs_lists_only_bin_folders_that_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "cudnn").mkdir()  # package without a bin folder is ignored
    monkeypatch.setattr(local.sysconfig, "get_paths", lambda: {"purelib": str(tmp_path), "platlib": str(tmp_path)})

    assert local.nvidia_dll_dirs() == [str(tmp_path / "nvidia" / "cublas" / "bin")]


@pytest.mark.skipif(sys.platform != "win32", reason="os.add_dll_directory is Windows-only")
def test_enable_nvidia_dlls_prepends_path_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "nvidia" / "cudart" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(local, "nvidia_dll_dirs", lambda: [str(bin_dir)])
    monkeypatch.setenv("PATH", r"C:\somewhere")

    assert local.enable_nvidia_dlls() == [str(bin_dir)]
    assert local.enable_nvidia_dlls() == [str(bin_dir)]
    assert os.environ["PATH"].split(os.pathsep) == [str(bin_dir), r"C:\somewhere"]


def test_enable_nvidia_dlls_is_a_no_op_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local.sys, "platform", "linux")
    monkeypatch.setenv("PATH", "/usr/bin")

    assert local.enable_nvidia_dlls() == []
    assert os.environ["PATH"] == "/usr/bin"


def test_sherpa_cuda_available_looks_for_the_cuda_provider_library(tmp_path: Path) -> None:
    assert local.sherpa_cuda_available(tmp_path) is False
    (tmp_path / "onnxruntime_providers_cuda.dll").write_bytes(b"")
    assert local.sherpa_cuda_available(tmp_path) is True


@pytest.mark.parametrize(
    ("requested", "cuda_build", "expected"),
    [("auto", True, "cuda"), ("auto", False, "cpu"), ("cpu", True, "cpu"), ("cuda", True, "cuda")],
)
def test_resolve_tts_provider(requested: str, cuda_build: bool, expected: str) -> None:
    assert local.resolve_tts_provider(requested, cuda_build) == expected


def test_resolve_tts_provider_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="provider must be"):
        local.resolve_tts_provider("tpu", True)


def test_resolve_tts_provider_refuses_cuda_on_a_cpu_build() -> None:
    # sherpa-onnx would silently fall back to the CPU and every later report would lie.
    with pytest.raises(RuntimeError, match="CPU build"):
        local.resolve_tts_provider("cuda", False)


def test_is_quantized_reads_the_graph_not_the_file_name(tmp_path: Path) -> None:
    quantized = tmp_path / "model.onnx"  # Kitten ships its quantized graph under this name
    quantized.write_bytes(b"\x08\x07onnx graph ... MatMulInteger ... more")
    plain = tmp_path / "fp32.onnx"
    plain.write_bytes(b"\x08\x07onnx graph ... MatMul ... Conv ...")

    assert local.is_quantized(str(quantized)) is True
    assert local.is_quantized(str(plain)) is False
    assert local.is_quantized(str(tmp_path / "missing.onnx")) is False
    assert local.is_quantized(r"D:\models\kokoro\model.int8.onnx") is True  # name is enough


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        (r"D:\models\kokoro\model.int8.onnx", "cuda", True),
        (r"D:\models\kokoro\model.onnx", "cuda", False),
        (r"D:\models\kokoro\model.int8.onnx", "cpu", False),
    ],
)
def test_int8_on_cuda_flags_only_quantized_models_on_the_gpu(model: str, provider: str, expected: bool) -> None:
    assert local.int8_on_cuda(model, provider) is expected


class TestSentenceBuffer:
    """Tokens in, whole clauses out — the difference between speech and stutter."""

    def test_releases_a_sentence_only_once_it_is_complete(self) -> None:
        buffer = local.SentenceBuffer()

        assert buffer.push("Aye") == []
        assert buffer.push(", one room") == []
        assert buffer.push(" left. ") == ["Aye, one room left."]
        assert buffer.push("Upstairs.") == []  # no trailing space yet
        assert buffer.flush() == "Upstairs."

    def test_splits_several_sentences_arriving_in_one_token(self) -> None:
        buffer = local.SentenceBuffer()

        assert buffer.push("Aye. Two rooms. ") == ["Aye.", "Two rooms."]

    def test_keeps_the_closing_quote_with_its_sentence(self) -> None:
        buffer = local.SentenceBuffer()

        assert buffer.push('He said "no." And left. ') == ['He said "no."', "And left."]

    def test_a_model_that_forgets_punctuation_still_speaks(self) -> None:
        buffer = local.SentenceBuffer(max_chars=20)

        released = buffer.push("one two three four five six seven")

        # Cut at the last space before the limit, never mid-word.
        assert released == ["one two three four"]
        assert buffer.flush() == "five six seven"

    def test_a_word_longer_than_the_limit_is_cut_rather_than_held_forever(self) -> None:
        buffer = local.SentenceBuffer(max_chars=8)

        released = buffer.push("supercalifragilistic")

        assert released == ["supercal", "ifragili"]
        assert buffer.flush() == "stic"


@pytest.mark.asyncio
async def test_llm_first_token_is_the_first_token_not_the_first_clause() -> None:
    """A provider that buffers into sentences must not report the buffering as model latency."""
    from voicert.config import ConfigFactory
    from voicert.frames import Frame, TextFrame
    from voicert.processors.base import LLMService

    class SlowClauseLLM(LLMService):
        async def generate(self, messages: list[dict[str, str]]) -> "AsyncIterator[Frame]":
            self.note_first_token()          # token arrived now
            await asyncio.sleep(0.05)        # ...clause completes only later
            yield TextFrame(text="Aye, one room left.", role="assistant", final=False)

    runtime = ConfigFactory.build("npc")
    llm = SlowClauseLLM(runtime.ctx)
    turn = runtime.state.add_user_final("any room?")
    runtime.metrics.turn_started(turn.turn_id)

    async for _ in llm.process_frame(
        TextFrame(text="any room?", role="user", final=True, turn_id=turn.turn_id)
    ):
        pass

    first_token_ms = runtime.metrics.report(turn.turn_id)["llm_first_token"]
    assert first_token_ms < 40, f"marked at the clause, not the token: {first_token_ms} ms"
