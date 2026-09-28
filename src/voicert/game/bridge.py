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
  0x01 HELLO      JSON  {"proto":1,"npc_id","character","lore_scope","voice","player_id"}
  0x02 TEXT_IN    UTF-8 text the player said or typed
  0x03 AUDIO_IN   PCM16 mono 16 kHz microphone audio (runs VAD -> barge-in)
  0x04 EVENT      JSON  {"event","payload"}   in-game event for the NPC
  0x05 INTERRUPT  empty  the engine detected the player talking over the NPC
  0x06 LOD        JSON  {"tier","distance_m","priority"}
  0x07 ENDPOINT   empty  the player released push-to-talk: end the utterance NOW
                        instead of waiting out the VAD's hangover

Server -> client
  0x81 READY      JSON  {"npc_id","sample_rate","channels":1,"format":"pcm16"}
                        sample_rate is whatever the TTS stage emits (16000 on stubs,
                        24000 for Kokoro/Kitten); the engine resamples from it.
  0x82 AUDIO_OUT  PCM16 mono chunk at that rate — queue it into the engine's audio source
  0x83 TEXT_OUT   JSON  {"text","turn_id"}   partial words, for subtitles / visemes
  0x84 TURN_END   JSON  {"turn_id","metrics"}
  0x85 FLUSH      empty  barge-in happened: drop every queued audio sample NOW
  0x86 TOOL       JSON  {"tool_name","arguments","call_id"}  e.g. play_animation
  0x87 STATE      JSON  {"state"}  idle|listening|processing|speaking|interrupted|error
  0x88 STT        JSON  {"text","final"}  what the player was heard to say
  0x8F ERROR      JSON  {"message"}

One connection is one NPC. The engine's dialogue LOD decides which NPCs
hold a connection; ``max_sessions`` caps it server-side as a backstop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from voicert.config import AgentRuntime, ConfigFactory
from voicert.frames import (
    AudioFrame,
    ErrorFrame,
    Frame,
    FunctionCallFrame,
    InterruptionFrame,
    TextFrame,
)
from voicert.game.session import SessionState, SessionStateMachine
from voicert.processors.base import TTSService
from voicert.transport import BaseTransport, EnergyVAD, VADConfig

logger = logging.getLogger("voicert.game.bridge")

PROTO_VERSION = 1
HEADER = struct.Struct("!BI")  # type, length (big-endian)
MAX_FRAME = 4 * 1024 * 1024

# client -> server
T_HELLO, T_TEXT_IN, T_AUDIO_IN, T_EVENT, T_INTERRUPT, T_LOD, T_ENDPOINT = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
)
# server -> client
T_READY, T_AUDIO_OUT, T_TEXT_OUT, T_TURN_END, T_FLUSH, T_TOOL, T_STATE, T_STT, T_ERROR = (
    0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x8F,
)

#: How often a partial transcript may be produced while the player is still
#: talking. Each one is a whole Whisper pass over the utterance so far: cheap
#: in wall-clock only because the LLM and the TTS are idle at that moment.
PARTIAL_INTERVAL_S = 0.55

#: Below this there is not enough audio for a transcript worth showing.
PARTIAL_MIN_MS = 400

#: A preview never looks further back than this. The snapshot grows with the
#: utterance, and the preview holds the same Whisper handle the real
#: transcription needs — an unbounded preview started just before the player
#: releases the key would sit in front of the answer, and cancelling the task
#: cannot stop the thread that already holds the lock. Three seconds is enough
#: to show what is being said and costs a constant amount.
PARTIAL_WINDOW_MS = 3_000


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
    #: Whose memory this session reads and writes. One player in this demo; the
    #: field exists so a shared world does not need a schema change to get more.
    player_id: str = "player"

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
            player_id=str(obj.get("player_id") or "player"),
        )


def tts_output_rate(runtime: AgentRuntime, default: int = 16_000) -> int:
    """The sample rate the runtime's TTS stage will emit, for READY.

    Falls back to ``default`` when the pipeline has no TTS stage (a text-only
    test runtime), so the handshake never depends on the provider wiring.
    """
    for proc in runtime.pipeline.processors:
        if isinstance(proc, TTSService):
            return int(proc.output_sample_rate)
    return default


class EngineBridgeTransport(BaseTransport):
    """Pipeline sink that writes server->client frames to the socket.

    Audio goes out as it is synthesized; text follows its audio; an
    InterruptionFrame becomes FLUSH, which the engine must honour before it
    plays another sample. The session state machine is driven from here because
    this is where a turn's real edges are: the NPC starts speaking when the
    first sample goes out, not when the model was asked for one.
    """

    name = "engine-bridge"

    def __init__(
        self,
        send: Callable[[bytes], None],
        runtime_getter: Callable[[], AgentRuntime | None],
        *,
        vad: VADConfig | None = None,
        session: SessionStateMachine | None = None,
    ) -> None:
        super().__init__(EnergyVAD(vad or VADConfig(sensitivity=0.7, hangover_ms=450)))
        self._send = send
        self._runtime = runtime_getter
        self.session = session
        #: The rate READY promised the engine. AUDIO_OUT carries no rate on the
        #: wire, so a frame at any other rate would play at the wrong pitch;
        #: that is a provider bug and gets one loud warning, not silence.
        self.announced_rate: int | None = None
        self._rate_warned = False
        #: Audio put on the wire since the last FLUSH. The engine may still be
        #: playing it long after this process considers the turn over.
        self._audio_since_flush = False

    async def sink(self, frame: Frame) -> None:
        if isinstance(frame, AudioFrame):
            self._send_audio(frame)
        elif isinstance(frame, TextFrame):
            self._send_text(frame)
        elif isinstance(frame, ErrorFrame):
            # A stage failed. Tell the engine, and close the turn it was
            # answering so the game stops waiting for a line that is not coming.
            if self.session is not None:
                self.session.failed()
            runtime = self._runtime()
            if runtime is not None:
                # The turn is over, and the failure path has to say so too —
                # otherwise the conversation is never written down.
                runtime.turn_finished()
            self._send(encode_json(T_ERROR, {"message": f"{frame.stage}: {frame.message}"}))
            self._send(encode_json(T_TURN_END, {"turn_id": frame.turn_id, "metrics": {}}))
        elif isinstance(frame, InterruptionFrame):
            if self.session is not None:
                self.session.interrupted(still_listening=self.vad_speaking)
            self._audio_since_flush = False
            self._send(encode_frame(T_FLUSH))
        elif isinstance(frame, FunctionCallFrame):
            self._send(encode_json(T_TOOL, {
                "tool_name": frame.tool_name,
                "arguments": frame.arguments,
                "call_id": frame.call_id,
                "result": frame.result,
            }))

    def _send_audio(self, frame: AudioFrame) -> None:
        if frame.source != "agent":
            return
        if (
            self.announced_rate is not None
            and frame.sample_rate != self.announced_rate
            and not self._rate_warned
        ):
            self._rate_warned = True
            logger.warning(
                "TTS emitted %d Hz but READY announced %d Hz; the engine will play it "
                "at the wrong pitch", frame.sample_rate, self.announced_rate,
            )
        if self.session is not None:
            self.session.voice_started()
        self._audio_since_flush = True
        self._send(encode_frame(T_AUDIO_OUT, frame.pcm))

    def flush_playback(self) -> bool:
        """Drop whatever the engine still has queued. Returns whether it was needed.

        Called when the player starts speaking. The pipeline may have nothing in
        flight — a GPU voice finishes generating long before it finishes being
        spoken — and then the InterruptionManager has nothing to cancel, while
        the player is still being talked over.
        """
        if not self._audio_since_flush:
            return False
        self._audio_since_flush = False
        self._send(encode_frame(T_FLUSH))
        return True

    def _send_text(self, frame: TextFrame) -> None:
        if frame.role != "assistant":
            return
        if not frame.final:
            if frame.text:
                self._send(encode_json(T_TEXT_OUT, {"text": frame.text, "turn_id": frame.turn_id}))
            return
        metrics: dict[str, object] = {}
        runtime = self._runtime()
        if runtime is not None:
            metrics = runtime.metrics.report(frame.reply_to or max(frame.turn_id - 1, 1))
            runtime.turn_finished()
        if self.session is not None:
            self.session.turn_ended()
        self._send(encode_json(T_TURN_END, {"turn_id": frame.turn_id, "metrics": metrics}))


class NpcSession:
    """One connection: one NPC, one conversation, one state machine.

    Split out of the server so the per-connection wiring — which callback drives
    which state, what a partial transcript costs, when the conversation is
    written down — reads in one place instead of inside a hundred-line handler.
    """

    def __init__(
        self,
        hello: Hello,
        *,
        send: Callable[[bytes], None],
        runtime_factory: Callable[[Hello, BaseTransport], AgentRuntime],
        vad: VADConfig | None = None,
    ) -> None:
        self.hello = hello
        self.send = send
        self.state = SessionStateMachine(self._announce_state, label=f"npc:{hello.npc_id}")
        self.transport = EngineBridgeTransport(
            send, lambda: self.runtime, vad=vad, session=self.state
        )
        self.runtime: AgentRuntime | None = runtime_factory(hello, self.transport)
        self._partial_task: asyncio.Task[None] | None = None
        self._partial_at = 0.0
        self._partial_failed = False
        self._wire_callbacks()

    # -- wiring ----------------------------------------------------------

    def _wire_callbacks(self) -> None:
        """Chain onto what ConfigFactory already wired, never replace it.

        The interruption manager owns ``on_speech_start``; overwriting it here
        would silently disable barge-in — the kind of break that only shows up
        in a live test with a microphone.
        """
        transport = self.transport
        barge_in = transport.on_speech_start
        push_audio = transport.on_user_audio

        def on_speech_start() -> None:
            self.state.heard_speech()
            # Cut the tail the player is talking over, whether or not the
            # pipeline still has anything left to cancel.
            if transport.flush_playback():
                self.state.interrupted(still_listening=True)
            if barge_in is not None:
                barge_in()

        async def on_user_audio(frame: AudioFrame) -> None:
            # An utterance reached the pipeline: it survived the VAD and the
            # length guards, so this is where a turn actually begins.
            self.state.utterance_captured()
            self._cancel_partial()
            if push_audio is not None:
                await push_audio(frame)

        transport.on_speech_start = on_speech_start
        transport.on_user_audio = on_user_audio
        if self.runtime is not None:
            self.runtime.ctx.on_user_text = self._on_user_text

    def _announce_state(self, state: SessionState) -> None:
        self.send(encode_json(T_STATE, {"state": state.value}))

    def _on_user_text(self, text: str, final: bool) -> None:
        self.send(encode_json(T_STT, {"text": text, "final": final}))

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> int:
        """Start the pipeline; returns the rate READY should announce."""
        runtime = self.runtime
        assert runtime is not None
        await runtime.start()
        rate = tts_output_rate(runtime)
        self.transport.announced_rate = rate
        return rate

    async def close(self) -> None:
        self._cancel_partial()
        self.state.closed()
        runtime, self.runtime = self.runtime, None
        if runtime is None:
            return
        try:
            await runtime.stop()
        except Exception:  # noqa: BLE001 — shutdown must not mask the session error
            logger.exception("runtime stop failed")
        runtime.session_closed()

    # -- incoming --------------------------------------------------------

    async def on_text(self, text: str) -> None:
        """A typed line: skip STT entirely (see AgentRuntime.say_text)."""
        runtime = self.runtime
        if runtime is None or not text:
            return
        self.state.utterance_captured()
        self.send(encode_json(T_STT, {"text": text, "final": True}))
        await runtime.say_text(text)

    async def on_audio(self, pcm: bytes) -> None:
        await self.transport.feed_input(pcm, 16_000)
        if self.state.state is SessionState.LISTENING and not self.transport.vad_speaking:
            # The VAD opened and closed without producing an utterance: a cough,
            # a chair, a door. Say so, rather than showing "listening" to a
            # player who stopped talking a second ago.
            self.state.speech_dropped()
        self._maybe_partial()

    async def on_endpoint(self) -> None:
        """Push-to-talk released: end the utterance now.

        Waiting out the VAD's hangover would cost the player that hangover on
        every single turn, for nothing — they have just said they are finished
        by letting go of the key.

        A press that captured nothing — a mistaken tap, a word below the VAD's
        threshold — has to be reported. Silence here leaves the game showing
        "listening" to a player waiting for an answer that was never requested.
        """
        if await self.transport.end_utterance():
            return
        self._cancel_partial()
        if not self.state.speech_dropped():
            self.state.to(SessionState.IDLE)
        self.send(encode_json(T_STT, {"text": "", "final": True}))

    async def on_interrupt(self) -> bool:
        runtime = self.runtime
        if runtime is None:
            return False
        cut = await runtime.interruption.interrupt()
        if not cut:
            # Nothing was speaking, so no InterruptionFrame reaches the sink and
            # no FLUSH would be sent. Answer anyway: the engine suppresses its own
            # audio the moment it asks for a barge-in and only a FLUSH lifts that,
            # so an unanswered INTERRUPT mutes the NPC for every later turn.
            self.send(encode_frame(T_FLUSH))
        return cut

    async def on_event(self, obj: dict[str, Any]) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        event = str(obj.get("event") or "event")
        detail = json.dumps(obj.get("payload", {}), ensure_ascii=False)
        self.state.utterance_captured()
        await runtime.say_text(f"[game event: {event} {detail}]")

    # -- partial transcript ------------------------------------------------

    def _maybe_partial(self) -> None:
        """Transcribe what has been captured so far, at most every interval.

        Whisper has no streaming mode, so a "partial" is a whole pass over the
        utterance so far. It is affordable only because it costs the turn
        nothing: while the player is talking, the LLM and the TTS are idle. One
        at a time, and never after the real transcription has started.
        """
        runtime = self.runtime
        if runtime is None or runtime.partial_transcriber is None:
            return
        if self.state.state is not SessionState.LISTENING:
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        now = time.monotonic()
        if now - self._partial_at < PARTIAL_INTERVAL_S:
            return
        segmenter = self.transport.segmenter
        pcm = segmenter.snapshot() if segmenter is not None else b""
        if len(pcm) < 2 * 16 * PARTIAL_MIN_MS:   # 16 samples per ms at 16 kHz, 2 bytes each
            return
        pcm = pcm[-2 * 16 * PARTIAL_WINDOW_MS :]   # bound the cost, and the lock
        self._partial_at = now
        self._partial_task = asyncio.create_task(self._partial(pcm))

    async def _partial(self, pcm: bytes) -> None:
        runtime = self.runtime
        if runtime is None or runtime.partial_transcriber is None:
            return
        try:
            text = await runtime.partial_transcriber(pcm)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a preview must never break a turn
            if not self._partial_failed:
                self._partial_failed = True
                logger.warning("partial transcription failed; previews are off for this "
                               "session", exc_info=True)
            else:
                logger.debug("partial transcription failed", exc_info=True)
            return
        # By the time this lands the utterance may have ended, and a partial
        # arriving after the final one would overwrite the real transcript.
        if text and self.state.state is SessionState.LISTENING:
            self.send(encode_json(T_STT, {"text": text, "final": False}))

    def _cancel_partial(self) -> None:
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_task.cancel()
        self._partial_task = None


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
        vad: VADConfig | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sessions = max_sessions
        self.vad = vad
        self._runtime_factory = runtime_factory or self._default_runtime
        self._server: asyncio.AbstractServer | None = None
        #: Live per-connection handlers, so ``stop`` can cancel them.
        self._handlers: set[asyncio.Task[None]] = set()
        self.sessions = 0

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock is not None:
            self.port = sock.getsockname()[1]
        logger.info("engine bridge listening on %s:%d", self.host, self.port)

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Close the listener and let the live sessions go.

        ``wait_closed`` waits for the handler tasks, and a handler sits in
        ``read_frame`` for as long as the game holds its socket — so without
        cancelling them first, stopping a server with a connected NPC never
        returns and Ctrl+C cannot get the GPU models back.
        """
        if self._server is None:
            return
        self._server.close()
        for task in list(self._handlers):
            task.cancel()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._server.wait_closed(), timeout_s)
        self._handlers.clear()
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
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        peer = writer.get_extra_info("peername")
        if self.sessions >= self.max_sessions:
            await self._refuse(reader, writer)
            return

        self.sessions += 1
        session: NpcSession | None = None
        out_q: asyncio.Queue[bytes] = asyncio.Queue()
        pump = asyncio.create_task(self._pump_out(out_q, writer))
        try:
            session = await self._handshake(reader, out_q.put_nowait, peer)
            if session is not None:
                await self._serve(reader, session)
        except (ValueError, json.JSONDecodeError) as exc:
            out_q.put_nowait(encode_json(T_ERROR, {"message": str(exc)}))
            await asyncio.sleep(0)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if session is not None:
                await session.close()
            try:
                await self._drain_and_close(pump, writer)
            finally:
                self.sessions -= 1
                logger.info("session closed (%s)", peer)

    async def _handshake(
        self, reader: asyncio.StreamReader, send: Callable[[bytes], None], peer: Any
    ) -> NpcSession | None:
        first = await read_frame(reader)
        if first is None or first[0] != T_HELLO:
            send(encode_json(T_ERROR, {"message": "expected HELLO"}))
            await asyncio.sleep(0)
            return None
        hello = Hello.parse(first[1])
        session = NpcSession(hello, send=send, runtime_factory=self._runtime_factory, vad=self.vad)
        try:
            rate = await session.start()
        except BaseException:
            # The session already holds a character (and its memory). Give it
            # back before the failure leaves the building.
            await session.close()
            raise
        send(encode_json(
            T_READY,
            {"npc_id": hello.npc_id, "sample_rate": rate, "channels": 1, "format": "pcm16"},
        ))
        send(encode_json(T_STATE, {"state": session.state.state.value}))
        logger.info("npc %s connected from %s", hello.npc_id, peer)
        return session

    @staticmethod
    async def _serve(reader: asyncio.StreamReader, session: NpcSession) -> None:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                break
            ftype, payload = frame
            if ftype == T_AUDIO_IN:
                await session.on_audio(payload)
            elif ftype == T_TEXT_IN:
                await session.on_text(payload.decode("utf-8", errors="replace").strip())
            elif ftype == T_ENDPOINT:
                await session.on_endpoint()
            elif ftype == T_INTERRUPT:
                await session.on_interrupt()
            elif ftype in (T_EVENT, T_LOD):
                # A malformed payload is one bad frame, not the end of a
                # conversation — parse it where the failure can be answered.
                try:
                    obj = json.loads(payload.decode("utf-8") or "{}")
                except json.JSONDecodeError as exc:
                    session.send(encode_json(T_ERROR, {"message": f"bad {ftype:#x} payload: {exc}"}))
                    continue
                if ftype == T_EVENT:
                    await session.on_event(obj)
                else:
                    logger.debug("npc %s lod %s", session.hello.npc_id, obj)
                    if str(obj.get("tier", "")).upper() == "OFF":
                        break
            else:
                session.send(encode_json(T_ERROR, {"message": f"unknown frame type {ftype:#x}"}))

    @staticmethod
    async def _refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Over capacity. Consume the client's HELLO first so the peer never sees
        a reset while it is still sending; then reply and close cleanly."""
        with contextlib.suppress(
            asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError, ConnectionError
        ):
            await asyncio.wait_for(read_frame(reader), timeout=2.0)
        writer.write(encode_json(T_ERROR, {"message": "server full"}))
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()
            writer.write_eof()
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()

    @staticmethod
    async def _pump_out(out_q: asyncio.Queue[bytes], writer: asyncio.StreamWriter) -> None:
        while True:
            data = await out_q.get()
            writer.write(data)
            await writer.drain()

    @staticmethod
    async def _drain_and_close(pump: asyncio.Task[None], writer: asyncio.StreamWriter) -> None:
        # let queued frames flush before closing
        await asyncio.sleep(0)
        pump.cancel()
        # The pump may already have died on its own — the peer vanished mid-reply
        # and drain() raised — and awaiting it re-raises that. It must not be able
        # to skip the rest of this teardown: a leaked session slot is permanent,
        # and max_sessions of them turn the server into one that only ever
        # answers "server full".
        with contextlib.suppress(Exception):
            await pump
        writer.close()
        with contextlib.suppress(ConnectionError, OSError, asyncio.CancelledError):
            await writer.wait_closed()


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
