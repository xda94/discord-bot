from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from llm.capacity import CapacityError, async_action_context

logger = logging.getLogger("discord_bot")


@dataclass(frozen=True)
class WorkerResult:
    outcome: str = "completed"
    value: Any = None


class SingleSlotWorker:
    def __init__(
        self,
        processor: Callable[[Any], Awaitable[WorkerResult]],
        finished: Callable[[Any, WorkerResult], None],
    ) -> None:
        self._processor = processor
        self._finished = finished
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.sequence = 0
        self.processing = False
        self.task: asyncio.Task | None = None

    def busy(self) -> bool:
        return self.processing or self.queue.qsize() > 0

    def start(self) -> None:
        if self.task is None or self.task.done():
            previous_state = "missing" if self.task is None else "finished"
            self.task = asyncio.create_task(self.run())
            self.task.add_done_callback(self._stopped)
            logger.info("LLM queue worker started previous_state=%s", previous_state)

    async def submit(self, priority: int, job: Any) -> bool:
        self.sequence += 1
        self.queue.put_nowait((job.reservation.ticket, self.sequence, job))
        logger.info(
            "LLM job admitted sequence=%d type=%s priority=%s queue_size=%d",
            self.sequence,
            type(job).__name__,
            priority,
            self.queue.qsize(),
        )
        self.start()
        return True

    async def run(self) -> None:
        try:
            while True:
                ticket, sequence, job = await self.queue.get()
                started = time.monotonic()
                job_type = type(job).__name__
                result = WorkerResult("cancelled")
                logger.info(
                    "LLM job started sequence=%s ticket=%s type=%s model=%s "
                    "queue_remaining=%d",
                    sequence, ticket, job_type, getattr(job, "model", None),
                    self.queue.qsize(),
                )
                try:
                    self.processing = True
                    async with async_action_context(job.reservation):
                        result = await self._processor(job)
                except CapacityError:
                    result = WorkerResult("skipped")
                    logger.info("LLM job skipped sequence=%s reason=capacity", sequence)
                except Exception:
                    result = WorkerResult("failed")
                    logger.exception(
                        "Unhandled error processing LLM job sequence=%s type=%s",
                        sequence, job_type,
                    )
                finally:
                    logger.info(
                        "LLM job finished sequence=%s type=%s outcome=%s elapsed=%.2fs",
                        sequence, job_type, result.outcome,
                        time.monotonic() - started,
                    )
                    self.processing = False
                    self._finish(job, result)
        finally:
            self._drain()

    def _stopped(self, task: asyncio.Task) -> None:
        if task is self.task and task.cancelled():
            self._drain()

    def _drain(self) -> None:
        while not self.queue.empty():
            _, _, job = self.queue.get_nowait()
            self._finish(job, WorkerResult("cancelled"))

    def _finish(self, job: Any, result: WorkerResult) -> None:
        try:
            job.reservation.release()
            self._finished(job, result)
        except Exception:
            logger.exception("LLM job cleanup failed type=%s", type(job).__name__)
        finally:
            self.queue.task_done()
