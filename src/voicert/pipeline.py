"""Pipeline & FrameProcessor — Pipecat-inspired async frame conveyor.

Topology::

    push(frame) -> [q0] -> proc0 -> [q1] -> proc1 -> ... -> [qN] -> sink

Each processor runs its own pump task: it takes a frame from its inbox,
spawns a *child task* to run ``process_frame`` (an async generator), and
forwards yielded frames to the next queue. The child-task indirection is
what makes barge-in possible: ``Pipeline.interrupt()`` cancels the child
tasks without killing the pumps, so the pipeline survives the interruption
and keeps serving the next turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from voicert.frames import EndFrame, Frame, InterruptionFrame, InterruptionReason

logger = logging.getLogger("voicert.pipeline")

SinkFn = Callable[[Frame], Awaitable[None]]


class FrameProcessor:
    """Base processor. Subclasses override ``process_frame``.

    Contract:
      * ``process_frame`` is an async generator: consume one frame, yield
        zero or more frames. It MUST tolerate ``asyncio.CancelledError``
        at any await point — that is how barge-in reaches it.
      * ``on_interrupt`` runs *after* the in-flight task is cancelled;
        use it to drop internal buffers so stale partials never leak
        into the next turn.
    """

    name: str = "processor"

    async def process_frame(self, frame: Frame) -> AsyncIterator[Frame]:
        yield frame

    async def on_interrupt(self, frame: InterruptionFrame) -> None:  # noqa: B027
        """Flush internal state after cancellation. Default: nothing."""

    async def on_start(self) -> None:  # noqa: B027
        """Called once when the pipeline starts."""

    async def on_stop(self) -> None:  # noqa: B027
        """Called once when the pipeline stops."""


class Pipeline:
    """Chains processors with asyncio queues and owns the barge-in machinery.

    ``interrupt()`` sequencing (order matters):
      1. cancel every in-flight ``process_frame`` child task and await
         the cancellations — stops token/audio *production* first;
      2. drain every inter-processor queue — discards frames produced
         before the cut so they cannot play after it;
      3. call ``on_interrupt`` on every processor — flushes buffers;
      4. deliver the InterruptionFrame to the sink — lets the transport
         flush its playback buffer.

    Concurrent ``interrupt()`` calls are serialized by a lock; a second
    call that arrives while one is in progress becomes a no-op (checked
    via a generation counter), which is what makes rapid double barge-in
    safe.
    """

    def __init__(self, processors: Sequence[FrameProcessor], *, sink: SinkFn | None = None) -> None:
        if not processors:
            raise ValueError("Pipeline needs at least one processor")
        self._processors: list[FrameProcessor] = list(processors)
        self._sink: SinkFn | None = sink
        self._queues: list[asyncio.Queue[Frame]] = []
        self._pumps: list[asyncio.Task[None]] = []
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._interrupt_lock = asyncio.Lock()
        self._interrupt_gen = 0
        self._started = False
        self._stopped = asyncio.Event()

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Pipeline already started")
        self._started = True
        n = len(self._processors)
        self._queues = [asyncio.Queue() for _ in range(n + 1)]
        for proc in self._processors:
            await proc.on_start()
        for i, proc in enumerate(self._processors):
            self._pumps.append(
                asyncio.create_task(
                    self._pump(proc, self._queues[i], self._queues[i + 1]),
                    name=f"voicert-pump-{proc.name}",
                )
            )
        self._pumps.append(asyncio.create_task(self._drain_to_sink(), name="voicert-sink"))

    async def stop(self) -> None:
        if not self._started:
            return
        await self.push(EndFrame())
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("pipeline stop timed out; force-cancelling pumps")
        for task in self._pumps:
            task.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)
        for proc in self._processors:
            await proc.on_stop()
        self._started = False

    # -- data path -----------------------------------------------------

    async def push(self, frame: Frame) -> None:
        if not self._started:
            raise RuntimeError("Pipeline not started")
        await self._queues[0].put(frame)

    async def _pump(
        self,
        proc: FrameProcessor,
        inbox: asyncio.Queue[Frame],
        outbox: asyncio.Queue[Frame],
    ) -> None:
        while True:
            frame = await inbox.get()
            if isinstance(frame, EndFrame):
                await outbox.put(frame)
                return
            if isinstance(frame, InterruptionFrame):
                # Interruption frames travel out-of-band via interrupt();
                # one arriving through a queue is already-handled residue.
                continue
            gen = self._interrupt_gen
            task = asyncio.create_task(
                self._run_one(proc, frame, outbox), name=f"voicert-work-{proc.name}"
            )
            self._inflight[proc.name] = task
            try:
                await task
            except asyncio.CancelledError:
                if task.cancelled() and self._interrupt_gen > gen:
                    # Cancelled by interrupt(): swallow and keep pumping.
                    continue
                raise
            except Exception:
                logger.exception("processor %s failed on frame %r", proc.name, frame)
            finally:
                if self._inflight.get(proc.name) is task:
                    del self._inflight[proc.name]

    async def _run_one(self, proc: FrameProcessor, frame: Frame, outbox: asyncio.Queue[Frame]) -> None:
        async for out in proc.process_frame(frame):
            await outbox.put(out)

    async def _drain_to_sink(self) -> None:
        final_q = self._queues[-1]
        while True:
            frame = await final_q.get()
            if isinstance(frame, EndFrame):
                self._stopped.set()
                return
            if self._sink is not None:
                await self._sink(frame)

    # -- barge-in --------------------------------------------------------

    async def interrupt(
        self,
        reason: InterruptionReason = InterruptionReason.USER_BARGE_IN,
        turn_id: int = 0,
    ) -> bool:
        """Cancel all in-flight work and notify downstream. Returns True if
        this call performed the interruption, False if it was coalesced
        into one already in progress (rapid double barge-in)."""
        if self._interrupt_lock.locked():
            # A concurrent interrupt is mid-flight; coalesce.
            async with self._interrupt_lock:
                return False
        async with self._interrupt_lock:
            self._interrupt_gen += 1
            iframe = InterruptionFrame(reason=reason, turn_id=turn_id)

            tasks = [t for t in self._inflight.values() if not t.done()]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            for q in self._queues:
                self._drain_queue(q)

            for proc in self._processors:
                await proc.on_interrupt(iframe)

            if self._sink is not None:
                await self._sink(iframe)
            logger.info("pipeline interrupted: %s (turn %d)", reason.value, turn_id)
            return True

    @staticmethod
    def _drain_queue(q: asyncio.Queue[Frame]) -> None:
        while True:
            try:
                frame = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(frame, EndFrame):
                # Never discard shutdown signals during a barge-in.
                q.put_nowait(frame)
                return
