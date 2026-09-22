"""Single-slot asynchronous worker used by all local LLM jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("discord_bot")


@dataclass(frozen=True)
class WorkerResult:
    outcome: str = "completed"
    value: Any = None


class SingleSlotWorker:
    """Admit work only while idle and execute it on one persistent task."""

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
            logger.info("LLM queue worker started previous_state=%s", previous_state)

    async def submit(self, priority: int, job: Any) -> bool:
        # There is deliberately no await before put_nowait. Callers share the
        # Discord event loop, making this check-and-admit operation atomic.
        if self.busy():
            logger.info(
                "LLM job rejected type=%s priority=%s processing=%s queue_size=%d",
                type(job).__name__,
                priority,
                self.processing,
                self.queue.qsize(),
            )
            return False
        self.sequence += 1
        self.queue.put_nowait((priority, self.sequence, job))
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
        while True:
            priority, sequence, job = await self.queue.get()
            started = time.monotonic()
            job_type = type(job).__name__
            result = WorkerResult()
            logger.info(
                "LLM job started sequence=%s priority=%s type=%s model=%s "
                "queue_remaining=%d",
                sequence,
                priority,
                job_type,
                getattr(job, "model", None),
                self.queue.qsize(),
            )
            try:
                self.processing = True
                result = await self._processor(job)
            except Exception:
                result = WorkerResult("failed")
                logger.exception(
                    "Unhandled error processing LLM job sequence=%s type=%s",
                    sequence,
                    job_type,
                )
            finally:
                logger.info(
                    "LLM job finished sequence=%s type=%s outcome=%s elapsed=%.2fs",
                    sequence,
                    job_type,
                    result.outcome,
                    time.monotonic() - started,
                )
                self.processing = False
                self.queue.task_done()
                self._finished(job, result)

