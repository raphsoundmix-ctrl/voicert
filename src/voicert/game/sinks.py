"""Generated-audio sinks — pushing synthesized PCM into game audio middleware.

The design rule: **an AI voice must be an ordinary voice.** It should travel
the same path as a footstep — through the same busses, attenuation curves,
occlusion, reverb sends, ducking, and voice limiting the sound designer
already authored. Anything else means maintaining a second mix, and a second
mix is where AI dialogue starts sounding pasted-on.

Both major middlewares expose the same shape of hook: register a source whose
buffers you fill from a callback, then treat it as a normal event.

**Wwise** — the Audio Input source plugin. You post an event backed by an
Audio Input source and supply two callbacks: one that fills a buffer of
samples on demand, one that reports the format. From that point the sound is
a Wwise voice: it obeys attenuation, obstruction/occlusion, bus routing,
RTPCs, and playback-limit virtualization.

**FMOD** — a programmer sound (or a user-created sound with a PCM read
callback). The programmer-sound callback hands you the sound instance to fill
at play time; the event instance then behaves like any other event in the
project, including 3D attenuation and bus effects.

**Unity** without middleware — ``OnAudioFilterRead`` on an AudioSource.
**Unreal** without middleware — ``USoundWaveProcedural``.

The Python side of this repo is the *producer*: it hands over interleaved
16-bit PCM chunks as they are synthesized. The consumer is a small native
shim in the game process. The classes below define that contract precisely so
the shim is mechanical to write; they are deliberately transport-agnostic
(shared memory, a local socket, or an embedded interpreter all satisfy it).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from voicert.frames import AudioFrame, Frame, InterruptionFrame

logger = logging.getLogger("voicert.game.sinks")


@runtime_checkable
class AudioSink(Protocol):
    """What the game side must implement.

    ``push`` must be non-blocking and must never allocate on the audio
    thread — buffer into a lock-free ring and let the middleware callback
    drain it. ``flush`` is the barge-in path: it has to discard queued audio
    *immediately*, or the NPC keeps talking after the player cut them off.
    """

    def push(self, pcm: bytes, sample_rate: int) -> None: ...

    def flush(self) -> None: ...


class _BaseSink:
    """Shared frame handling: route AudioFrames to push, interruptions to flush.

    Wire it as the pipeline sink::

        runtime = ConfigFactory.build("npc", transport=...)
        sink = WwiseAudioInputSink(npc_id="yorick", game_object_id=1042)
    """

    npc_id: str

    async def __call__(self, frame: Frame) -> None:
        if isinstance(frame, AudioFrame) and frame.source == "agent":
            self.push(frame.pcm, frame.sample_rate)  # type: ignore[attr-defined]
        elif isinstance(frame, InterruptionFrame):
            self.flush()  # type: ignore[attr-defined]


class WwiseAudioInputSink(_BaseSink):
    """Wwise Audio Input source plug-in.

    Native shim contract (C++ side). Note the callbacks are **global**, not
    per-voice: they are registered once via ``SetAudioInputCallbacks()`` and
    dispatch on the playing ID they receive, so the shim keeps a
    playingID -> ring-buffer map.

    1. ``AK::SoundEngine::RegisterGameObj(gameObjectId)`` for the NPC.
    2. ``SetAudioInputCallbacks()`` once at startup. The sound engine calls
       the *format* callback once at the beginning of playback, and the
       *execute* callback every audio frame until you stop it or return
       ``AK_NoMoreData``.
    3. ``AK::SoundEngine::PostEvent("Play_NPC_Voice", gameObjectId)`` — the
       event must be backed by an Audio Input source.

    In Unreal the wrapper is ``UAkAudioInputComponent``: subclass it, override
    ``FillSamplesBuffer`` and ``GetChannelConfig``, and start playback with
    **Post Associated Audio Input Event** — an ordinary PostEvent does not
    drive the Audio Input plug-in.

    From there the voice is fully authored content: attenuation curves,
    obstruction/occlusion, the dialogue bus, and any RTPC (e.g. "emotion")
    all apply, and Wwise's playback limits virtualize it when too many NPCs
    speak at once.

    The execute callback runs on the Wwise audio thread, so ``push`` writes
    into a ring buffer and the callback only ever reads — no locks, no
    allocation, nothing that can stall a frame.

    Prior art for this exact transport: ReadSpeaker's speechEngine plug-in
    (runtime TTS), 4Players ODIN voice chat, and Unreal AudioLink all feed
    Wwise through Audio Input.
    """

    def __init__(self, npc_id: str, game_object_id: int, event_name: str = "Play_NPC_Voice") -> None:
        self.npc_id = npc_id
        self.game_object_id = game_object_id
        self.event_name = event_name
        self._queued_bytes = 0

    def push(self, pcm: bytes, sample_rate: int) -> None:
        self._queued_bytes += len(pcm)
        logger.debug(
            "wwise[%s obj=%d] +%d bytes @%d Hz", self.npc_id, self.game_object_id, len(pcm), sample_rate
        )
        raise NotImplementedError(
            "WwiseAudioInputSink is the documented contract for the native shim: "
            "copy pcm into the AkAudioBuffer ring consumed by the Audio Input execute callback."
        )

    def flush(self) -> None:
        self._queued_bytes = 0
        logger.info("wwise[%s] flush on barge-in", self.npc_id)


class FMODProgrammerSink(_BaseSink):
    """FMOD Studio programmer sound / PCM read callback.

    Native shim contract (C++ side):

    1. Create the event instance (its timeline must contain a *programmer
       instrument*) and set the callback mask for
       ``FMOD_STUDIO_EVENT_CALLBACK_CREATE_PROGRAMMER_SOUND``.
    2. In that callback, build a user-created sound —
       ``System::createSound`` with ``FMOD_OPENUSER`` plus an
       ``FMOD_CREATESOUNDEXINFO`` carrying ``defaultfrequency``,
       ``numchannels``, ``format`` and ``pcmreadcallback`` — and hand it back
       in the properties struct.
    3. FMOD then pulls PCM from your read callback in blocks of
       ``decodebuffersize``; release the sound in the matching
       ``DESTROY_PROGRAMMER_SOUND`` callback.

    The event instance keeps its authored 3D attenuation, bus routing, and
    effects — as far as the mix is concerned the generated voice is
    indistinguishable from a shipped asset.

    Concurrency is already handled for you: ``System::init`` sets
    ``maxchannels`` (virtual voices — FMOD suggests 256-1024 for most games)
    while ``System::setSoftwareChannels`` sets the real, mixed voices
    (default 64). The dialogue pool should be sized well below those limits.
    """

    def __init__(self, npc_id: str, event_path: str = "event:/NPC/Voice") -> None:
        self.npc_id = npc_id
        self.event_path = event_path
        self._queued_bytes = 0

    def push(self, pcm: bytes, sample_rate: int) -> None:
        self._queued_bytes += len(pcm)
        logger.debug("fmod[%s %s] +%d bytes @%d Hz", self.npc_id, self.event_path, len(pcm), sample_rate)
        raise NotImplementedError(
            "FMODProgrammerSink is the documented contract for the native shim: "
            "copy pcm into the ring drained by FMOD_CREATESOUNDEXINFO::pcmreadcallback."
        )

    def flush(self) -> None:
        self._queued_bytes = 0
        logger.info("fmod[%s] flush on barge-in", self.npc_id)


class NullSink(_BaseSink):
    """Records what would have been played. Used by tests and the offline demo
    so the whole game path is exercisable without an engine attached."""

    def __init__(self, npc_id: str = "test-npc") -> None:
        self.npc_id = npc_id
        self.chunks: list[bytes] = []
        self.flushes = 0

    def push(self, pcm: bytes, sample_rate: int) -> None:
        self.chunks.append(pcm)

    def flush(self) -> None:
        self.chunks.clear()
        self.flushes += 1

    @property
    def total_bytes(self) -> int:
        return sum(len(c) for c in self.chunks)
