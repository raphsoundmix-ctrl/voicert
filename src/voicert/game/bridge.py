"""Engine bridge — how Unity and Unreal talk to a VoiceRT NPC.

The engine side is deliberately thin: a component on an actor opens one
TCP connection per live NPC, sends what the player said, and receives a
stream of 16-bit PCM plus the text of what the NPC is saying. Everything
heavy (VAD, LLM, TTS, the voice pool) stays in this process. A thin client
is what keeps the game's frame budget untouched and makes the engine
plugin a few hundred lines of C# or C++ instead of a model runtime.

Wire format
-----------
TCP, little ceremony, no dependencies on either side. Every frame is::

    type (1 byte) | length (4 bytes, big-endian, unsigned) | payload

Client -> server
  0x01 HELLO      JSON  {"proto":1,"npc_id","character","lore_scope","voice"}
  0x02 TEXT_IN    UTF-8 text the player said or typed
  0x03 AUDIO_IN   PCM16 mono 16 kHz microphone audio (runs VAD -> barge-in)
  0x04 EVENT      JSON  {"event","payload"}   in-game event for the NPC
  0x05 INTERRUPT  empty  the engine detected the player talking over the NPC
  0x06 LOD        JSON  {"tier","distance_m","priority"}

Server -> client
  0x81 READY      JSON  {"npc_id","sample_rate":16000,"channels":1,"format":"pcm16"}
  0x82 AUDIO_OUT  PCM16 mono 16 kHz chunk — queue it into the engine's audio source
  0x83 TEXT_OUT   JSON  {"text","turn_id"}   partial words, for subtitles / visemes
  0x84 TURN_END   JSON  {"turn_id","metrics"}
  0x85 FLUSH      empty  barge-in happened: drop every queued audio sample NOW
  0x86 TOOL       JSON  {"tool_name","arguments","call_id"}  e.g. play_animation
  0x8F ERROR      JSON  {"message"}

One connection is one NPC. The engine's dialogue LOD decides which NPCs
hold a connection; ``max_sessions`` caps it server-side as a backstop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from voicert.config import AgentRuntime, ConfigFactory
from voicert.frames import AudioFrame, Frame, FunctionCallFrame, InterruptionFrame, TextFrame
from voicert.processors.stubs import make_user_audio
from voicert.transport import BaseTransport, EnergyVAD, VADConfig

logger = logging.getLogger("voicert.game.bridge")

PROTO_VERSION = 1
HEADER = struct.Struct("!BI")  # type, length (big-endian)
MAX_FRAME = 4 * 1024 * 1024

# client -> server
T_HELLO, T_TEXT_IN, T_AUDIO_IN, T_EVENT, T_INTERRUPT, T_LOD = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
# server -> client
T_READY, T_AUDIO_OUT, T_TEXT_OUT, T_TURN_END, T_FLUSH, T_TOOL, T_ERROR = (
    0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x8F,
)


def encode_frame(ftype: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_FRAME:
        raise ValueError(f"frame payload too large: {len(payload)}")
    return HEADER.pack(ftype, len(payload)) + payload


def encode_json(ftype: int, obj: Any) -> bytes:
    return encode_frame(ftype, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """Read one frame; None on clean EOF."""
    try:
        head = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    ftype, length = HEADER.unpack(head)
    if length > MAX_FRAME:
        raise ValueError(f"incoming frame too large: {length}")
    payload = await reader.readexactly(length) if length else b""
    return ftype, payload


@dataclass(frozen=True, slots=True)
class Hello:
    npc_id: str
    character: str = ""
    lore_scope: str = ""
    voice: str = "default"

    @classmethod
    def parse(cls, payload: bytes) -> "Hello":
        obj = json.loads(payload.decode("utf-8") or "{}")
        if obj.get("proto", PROTO_VERSION) != PROTO_VERSION:
            raise ValueError(f"unsupported proto {obj.get('proto')}")
        npc_id = str(obj.get("npc_id") or "").strip()
        if not npc_id:
            raise ValueError("HELLO requires npc_id")
        return cls(
            npc_id=npc_id,
            character=str(obj.get("character") or ""),
            lore_scope=str(obj.get("lore_scope") or ""),
            voice=str(obj.get("voice") or "default"),
        )


class EngineBridgeTransport(BaseTransport):
    """Pipeline sink that writes server->client frames to the socket.

    Audio goes out as it is synthesized; text follows its audio; an
    InterruptionFrame becomes FLUSH, which the engine must honour before
    it plays another sample.
    """

    name = "engine-bridge"

    def __init__(self, send: Callable[[bytes], None], runtime_getter: Callable[[], AgentRuntime | None]) -> None:
        super().__init__(EnergyVAD(VADConfig(sensitivity=0.7, hangover_ms=150)))
        self._send = send
        self._runtime = runtime_getter

    async def sink(self, frame: Frame) -> None:
        if isinstance(frame, AudioFrame):
            if frame.source == "agent":
                self._send(encode_frame(T_AUDIO_OUT, frame.pcm))
        elif isinstance(frame, TextFrame):
            if frame.role != "assistant":
                return
            if frame.final:
                metrics: dict[str, object] = {}
                runtime = self._runtime()
                if runtime is not None:
                    # turn ids are sequential: the user turn precedes this one
                    metrics = runtime.metrics.report(max(frame.turn_id - 1, 1))
                self._send(encode_json(T_TURN_END, {"turn_id": frame.turn_id, "metrics": metrics}))
            elif frame.text:
                self._send(encode_json(T_TEXT_OUT, {"text": frame.text, "turn_id": frame.turn_id}))
        elif isinstance(frame, InterruptionFrame):
            self._send(encode_frame(T_FLUSH))
        elif isinstance(frame, FunctionCallFrame):
            self._send(
                encode_json(
                    T_TOOL,
                    {
                        "tool_name": frame.tool_name,
                        "arguments": frame.arguments,
                        "call_id": frame.call_id,
                        "result": frame.result,
                    },
                )
            )


class EngineBridgeServer:
    """asyncio TCP server; one connection = one NPC session.

    ``runtime_factory`` lets tests and games inject their own runtime (a
    different profile, real providers, a shared voice pool). The default
    builds the stock NPC profile with stub providers, so the whole bridge
    is exercisable with no API keys.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        max_sessions: int = 8,
        runtime_factory: Callable[[Hello, BaseTransport], AgentRuntime] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sessions = max_sessions
        self._runtime_factory = runtime_factory or self._default_runtime
        self._server: asyncio.AbstractServer | None = None
        self.sessions = 0

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock is not None:
            self.port = sock.getsockname()[1]
        logger.info("engine bridge listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # -- per-connection ----------------------------------------------------

    @staticmethod
    def _default_runtime(hello: Hello, transport: BaseTransport) -> AgentRuntime:
        runtime = ConfigFactory.build("npc", transport=transport)
        if hello.character or hello.lore_scope:
            prompt = runtime.config.system_prompt.format(
                character=hello.character or "a character of this world",
                lore_scope=hello.lore_scope or "this game world",
            )
            runtime.ctx.system_prompt = prompt
        return runtime

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self.sessions >= self.max_sessions:
            # Consume the client's HELLO first so the peer never sees a
            # reset while it is still sending; then reply and close cleanly.
            try:
                await asyncio.wait_for(read_frame(reader), timeout=2.0)
            except (asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError, ConnectionError):
                pass
            writer.write(encode_json(T_ERROR, {"message": "server full"}))
            try:
                await writer.drain()
                writer.write_eof()
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        self.sessions += 1
        runtime: AgentRuntime | None = None
        out_q: asyncio.Queue[bytes] = asyncio.Queue()

        def send(data: bytes) -> None:
            out_q.put_nowait(data)

        async def pump_out() -> None:
            while True:
                data = await out_q.get()
                writer.write(data)
                await writer.drain()

        pump = asyncio.create_task(pump_out())
        try:
            first = await read_frame(reader)
            if first is None or first[0] != T_HELLO:
                send(encode_json(T_ERROR, {"message": "expected HELLO"}))
                await asyncio.sleep(0)
                return
            hello = Hello.parse(first[1])
            transport = EngineBridgeTransport(send, lambda: runtime)
            runtime = self._runtime_factory(hello, transport)
            await runtime.start()
            send(encode_json(
                T_READY,
                {"npc_id": hello.npc_id, "sample_rate": 16000, "channels": 1, "format": "pcm16"},
            ))
            logger.info("npc %s connected from %s", hello.npc_id, peer)

            while True:
                frame = await read_frame(reader)
                if frame is None:
                    break
                ftype, payload = frame
                if ftype == T_TEXT_IN:
                    text = payload.decode("utf-8", errors="replace").strip()
                    if text:
                        await runtime.pipeline.push(make_user_audio(text))
                elif ftype == T_AUDIO_IN:
                    await transport.feed_input(payload, 16000)
                elif ftype == T_EVENT:
                    obj = json.loads(payload.decode("utf-8") or "{}")
                    event = str(obj.get("event") or "event")
                    detail = json.dumps(obj.get("payload", {}), ensure_ascii=False)
                    await runtime.pipeline.push(make_user_audio(f"[game event: {event} {detail}]"))
                elif ftype == T_INTERRUPT:
                    await runtime.interruption.interrupt()
                elif ftype == T_LOD:
                    obj = json.loads(payload.decode("utf-8") or "{}")
                    logger.debug("npc %s lod %s", hello.npc_id, obj)
                    if str(obj.get("tier", "")).upper() == "OFF":
                        break
                else:
                    send(encode_json(T_ERROR, {"message": f"unknown frame type {ftype:#x}"}))
        except (ValueError, json.JSONDecodeError) as exc:
            send(encode_json(T_ERROR, {"message": str(exc)}))
            await asyncio.sleep(0)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if runtime is not None:
                try:
                    await runtime.stop()
                except Exception:  # noqa: BLE001 — shutdown must not mask the session error
                    logger.exception("runtime stop failed")
            # let queued frames flush before closing
            await asyncio.sleep(0)
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.sessions -= 1
            logger.info("session closed (%s)", peer)


async def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    """``python -m voicert.game.bridge`` — run the bridge on localhost."""
    logging.basicConfig(level=logging.INFO, format="%(name)s  %(message)s")
    server = EngineBridgeServer(host, port)
    await server.serve_forever()


if __name__ == "__main__":
    import sys

    _host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    _port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    asyncio.run(main(_host, _port))
