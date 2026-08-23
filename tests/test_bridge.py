"""Engine bridge acceptance: the wire protocol a Unity/Unreal component speaks.

HELLO -> READY; TEXT_IN -> AUDIO_OUT + TEXT_OUT + TURN_END; INTERRUPT -> FLUSH;
a tool-triggering line -> TOOL; bad handshake -> ERROR; capacity -> ERROR.
"""

import asyncio
import json

import pytest

from voicert.game.bridge import (
    T_AUDIO_OUT,
    T_ERROR,
    T_FLUSH,
    T_HELLO,
    T_INTERRUPT,
    T_READY,
    T_TEXT_IN,
    T_TEXT_OUT,
    T_TOOL,
    T_TURN_END,
    EngineBridgeServer,
    encode_frame,
    encode_json,
    read_frame,
)


async def _recv_until(reader, stop_types, timeout=5.0):
    """Collect frames until one of stop_types arrives (inclusive)."""
    frames = []

    async def _go():
        while True:
            fr = await read_frame(reader)
            if fr is None:
                return
            frames.append(fr)
            if fr[0] in stop_types:
                return

    await asyncio.wait_for(_go(), timeout)
    return frames


@pytest.fixture
async def bridge():
    server = EngineBridgeServer("127.0.0.1", 0, max_sessions=2)
    await server.start()
    yield server
    await server.stop()


async def _connect(server, npc_id="yorick", **hello):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(encode_json(T_HELLO, {"proto": 1, "npc_id": npc_id, **hello}))
    await writer.drain()
    return reader, writer


# -- codec ---------------------------------------------------------------


def test_frame_codec_roundtrip_sync():
    data = encode_frame(T_TEXT_IN, "hello".encode())
    assert data[:1] == bytes([T_TEXT_IN])
    assert int.from_bytes(data[1:5], "big") == 5
    assert data[5:] == b"hello"


def test_empty_payload_frame_is_five_bytes():
    assert len(encode_frame(T_INTERRUPT)) == 5


# -- handshake -------------------------------------------------------------


async def test_hello_gets_ready(bridge):
    reader, writer = await _connect(bridge, character="Yorick", lore_scope="Velenhart")
    frames = await _recv_until(reader, {T_READY})
    assert frames[-1][0] == T_READY
    ready = json.loads(frames[-1][1])
    assert ready == {"npc_id": "yorick", "sample_rate": 16000, "channels": 1, "format": "pcm16"}
    writer.close()
    await writer.wait_closed()


async def test_non_hello_first_frame_is_an_error(bridge):
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    writer.write(encode_frame(T_TEXT_IN, b"hi"))
    await writer.drain()
    frames = await _recv_until(reader, {T_ERROR})
    assert frames[-1][0] == T_ERROR
    assert "HELLO" in json.loads(frames[-1][1])["message"]
    writer.close()
    await writer.wait_closed()


async def test_missing_npc_id_is_an_error(bridge):
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    writer.write(encode_json(T_HELLO, {"proto": 1}))
    await writer.drain()
    frames = await _recv_until(reader, {T_ERROR})
    assert "npc_id" in json.loads(frames[-1][1])["message"]
    writer.close()
    await writer.wait_closed()


# -- a full turn -----------------------------------------------------------


async def test_text_in_yields_audio_text_and_turn_end(bridge):
    reader, writer = await _connect(bridge)
    await _recv_until(reader, {T_READY})

    writer.write(encode_frame(T_TEXT_IN, "What is for sale today?".encode()))
    await writer.drain()
    frames = await _recv_until(reader, {T_TURN_END})

    types = [t for t, _ in frames]
    assert T_AUDIO_OUT in types, "NPC voice must stream to the engine"
    assert T_TEXT_OUT in types, "subtitle text must accompany the audio"
    assert types[-1] == T_TURN_END

    audio_bytes = sum(len(p) for t, p in frames if t == T_AUDIO_OUT)
    assert audio_bytes > 0 and audio_bytes % 2 == 0, "PCM16 payloads are whole samples"

    # text follows its own audio: the first TEXT_OUT cannot precede the first AUDIO_OUT
    assert types.index(T_AUDIO_OUT) < types.index(T_TEXT_OUT)

    end = json.loads(frames[-1][1])
    assert end["turn_id"] >= 1
    assert "tts_first_audio" in end["metrics"]
    assert end["metrics"]["profile"] == "npc"
    writer.close()
    await writer.wait_closed()


async def test_interrupt_produces_flush_and_stops_audio(bridge):
    reader, writer = await _connect(bridge)
    await _recv_until(reader, {T_READY})

    writer.write(encode_frame(T_TEXT_IN, "Tell me a long story about the harbor".encode()))
    await writer.drain()
    # wait for the first audio chunk, then cut
    await _recv_until(reader, {T_AUDIO_OUT})
    writer.write(encode_frame(T_INTERRUPT))
    await writer.drain()

    frames = await _recv_until(reader, {T_FLUSH})
    assert frames[-1][0] == T_FLUSH

    # settle: no audio may follow the flush
    async def _drain_quiet():
        try:
            while True:
                fr = await asyncio.wait_for(read_frame(reader), 0.3)
                if fr is None:
                    return []
                yield_frames.append(fr)
        except asyncio.TimeoutError:
            return

    yield_frames: list = []
    await _drain_quiet()
    assert all(t != T_AUDIO_OUT for t, _ in yield_frames), "no audio after FLUSH"
    writer.close()
    await writer.wait_closed()


async def test_tool_calls_are_forwarded_to_the_engine(bridge):
    reader, writer = await _connect(bridge)
    await _recv_until(reader, {T_READY})
    writer.write(encode_frame(T_TEXT_IN, "Use a tool and check the world state".encode()))
    await writer.drain()
    frames = await _recv_until(reader, {T_TURN_END})
    tools = [json.loads(p) for t, p in frames if t == T_TOOL]
    assert tools, "FunctionCallFrame must reach the engine as a TOOL frame"
    assert tools[0]["tool_name"] in {"emit_game_event", "query_world_state", "play_animation"}
    writer.close()
    await writer.wait_closed()


async def test_lore_from_hello_lands_in_the_system_prompt(bridge):
    seen = {}
    original = bridge._runtime_factory

    def spy(hello, transport):
        rt = original(hello, transport)
        seen["prompt"] = rt.ctx.system_prompt
        return rt

    bridge._runtime_factory = spy
    reader, writer = await _connect(bridge, character="Mira the Smith", lore_scope="the forge district")
    await _recv_until(reader, {T_READY})
    assert "Mira the Smith" in seen["prompt"]
    assert "forge district" in seen["prompt"]
    writer.close()
    await writer.wait_closed()


async def test_server_full_rejects_extra_sessions(bridge):
    a = await _connect(bridge, npc_id="a")
    await _recv_until(a[0], {T_READY})
    b = await _connect(bridge, npc_id="b")
    await _recv_until(b[0], {T_READY})

    reader, writer = await _connect(bridge, npc_id="c")
    frames = await _recv_until(reader, {T_ERROR, T_READY})
    assert frames[-1][0] == T_ERROR
    assert "full" in json.loads(frames[-1][1])["message"]

    for _, w in (a, b):
        w.close()
        await w.wait_closed()
    writer.close()
    await writer.wait_closed()
