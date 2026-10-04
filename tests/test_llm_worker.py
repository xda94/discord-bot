import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from features.llm_mention import AskJob, LLMMentionFeature
from llm.capacity import CapacityBusy, action_context, busy, reserve
from llm.client import query_llm
from llm.worker import SingleSlotWorker, WorkerResult


def _feature():
    feature = object.__new__(LLMMentionFeature)
    feature._user_pending = set()
    feature._user_last_ask = {}
    feature._memory_pending = set()
    feature._reaction_pending_channels = set()
    feature._worker = SingleSlotWorker(
        feature._process_queued_job, feature._finish_queued_job,
    )
    feature._queue = feature._worker.queue
    feature._queue_sequence = 0
    feature._processing = False
    feature._worker_task = None
    return feature


def _job(user_id):
    return AskJob(
        user=SimpleNamespace(id=user_id, display_name="Alice"),
        question="hello", model="discord-bot",
    )


async def _stop(worker):
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.task


def test_worker_preserves_global_ticket_order_across_local_priorities():
    async def scenario():
        jobs = [SimpleNamespace(reservation=reserve()) for _ in range(3)]
        processed = []
        finished = []

        async def process(job):
            with action_context() as reservation:
                assert reservation is job.reservation
            processed.append(job)
            return WorkerResult()

        worker = SingleSlotWorker(process, lambda job, result: finished.append(job))
        try:
            await worker.submit(0, jobs[2])
            await worker.submit(2, jobs[0])
            await worker.submit(1, jobs[1])
            await asyncio.wait_for(worker.queue.join(), 1)
            assert processed == jobs
            assert finished == jobs
            assert not busy()
        finally:
            await _stop(worker)

    asyncio.run(scenario())


def test_bot_admission_counts_api_work_and_rejects_duplicate_user():
    async def scenario():
        api = reserve()
        feature = _feature()
        feature._process_job = AsyncMock()
        try:
            assert await feature._enqueue_job(_job(1))
            assert not await feature._enqueue_job(_job(1))
            assert await feature._enqueue_job(_job(2))
            assert not await feature._enqueue_job(_job(3))
            assert feature._user_pending == {1, 2}
            api.release()
            await asyncio.wait_for(feature._queue.join(), 1)
            assert feature._process_job.await_count == 2
            assert not feature._user_pending
            assert not busy()
        finally:
            api.release()
            await _stop(feature._worker)

    asyncio.run(scenario())


def test_worker_shutdown_releases_active_and_queued_pending_jobs():
    async def scenario():
        feature = _feature()
        started = asyncio.Event()

        async def process(job):
            started.set()
            await asyncio.Event().wait()

        feature._process_job = process
        for user_id in range(3):
            assert await feature._enqueue_job(_job(user_id))
        await asyncio.wait_for(started.wait(), 1)
        await _stop(feature._worker)
        await asyncio.wait_for(feature._queue.join(), 1)
        assert not feature._user_pending
        assert not feature._worker.processing
        assert not busy()

    asyncio.run(scenario())


def test_worker_cancelled_before_start_releases_queued_reservation():
    async def scenario():
        finished = MagicMock()
        worker = SingleSlotWorker(AsyncMock(), finished)
        job = SimpleNamespace(reservation=reserve())
        await worker.submit(0, job)
        await _stop(worker)
        await asyncio.wait_for(worker.queue.join(), 1)
        finished.assert_called_once_with(job, WorkerResult("cancelled"))
        assert not busy()

    asyncio.run(scenario())


def test_worker_error_releases_pending_and_continues():
    async def scenario():
        feature = _feature()
        feature._process_job = AsyncMock(side_effect=[RuntimeError("failed"), None])
        assert await feature._enqueue_job(_job(1))
        assert await feature._enqueue_job(_job(2))
        try:
            await asyncio.wait_for(feature._queue.join(), 1)
            assert feature._process_job.await_count == 2
            assert not feature._user_pending
            assert not busy()
            assert not feature._worker.task.done()
        finally:
            await _stop(feature._worker)

    asyncio.run(scenario())


def test_cancelled_worker_pins_capacity_until_http_thread_finishes(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def post(*args, **kwargs):
        started.set()
        try:
            assert release.wait(2)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": "done"}}]},
            )
        finally:
            finished.set()

    monkeypatch.setattr("llm.client.requests.post", post)

    async def scenario():
        feature = _feature()

        async def process(job):
            await asyncio.to_thread(query_llm, "hello", model="discord-bot")

        feature._process_job = process
        for user_id in range(3):
            assert await feature._enqueue_job(_job(user_id))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await _stop(feature._worker)
            assert not feature._user_pending
            assert busy()
            with pytest.raises(CapacityBusy):
                reserve(policy="background")
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            for _ in range(100):
                if not busy():
                    break
                await asyncio.sleep(0.01)
            assert not busy()
        finally:
            release.set()
            if feature._worker.task is not None and not feature._worker.task.done():
                await _stop(feature._worker)

    asyncio.run(scenario())


def test_expired_worker_reservation_cleans_pending_and_reports_retry():
    async def scenario():
        feature = _feature()
        feature._reply_job_error = AsyncMock()
        job = _job(1)
        job.reservation = reserve(queue_timeout=0)
        feature._user_pending.add(1)
        await feature._worker.submit(0, job)
        try:
            await asyncio.wait_for(feature._queue.join(), 1)
            await asyncio.sleep(0)
            assert not feature._user_pending
            assert not busy()
            feature._reply_job_error.assert_awaited_once()
            assert "try again" in feature._reply_job_error.await_args.args[1]
        finally:
            await _stop(feature._worker)

    asyncio.run(scenario())


def test_mention_retries_and_natural_action_share_job_reservation(monkeypatch):
    async def scenario():
        feature = _feature()
        job = _job(1)
        job.question = "show wishlist"
        job.reply_to = SimpleNamespace(reply=AsyncMock())
        seen = []
        outputs = iter([
            "invalid JSON",
            '{"text":"","reaction":null,"action":{"intent":"wishlist","slots":{}}}',
        ])

        def query(*args, **kwargs):
            with action_context() as reservation:
                seen.append(reservation)
            return next(outputs)

        async def execute(message, proposal):
            with action_context() as reservation:
                seen.append(reservation)

        feature.natural_commands = SimpleNamespace(execute_proposal=AsyncMock(side_effect=execute))
        monkeypatch.setattr("llm.client.query_llm", query)
        monkeypatch.setenv("NATURAL_LLM_ENABLED", "1")
        assert await feature._enqueue_job(job)
        try:
            await asyncio.wait_for(feature._queue.join(), 1)
            assert seen == [job.reservation] * 3
            feature.natural_commands.execute_proposal.assert_awaited_once()
            assert not busy()
        finally:
            await _stop(feature._worker)

    asyncio.run(scenario())


def test_stale_cancelled_worker_callback_does_not_drain_replacement():
    async def scenario():
        worker = SingleSlotWorker(AsyncMock(), MagicMock())
        old = asyncio.create_task(asyncio.sleep(0))
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
        replacement = asyncio.create_task(asyncio.Event().wait())
        worker.task = replacement
        job = SimpleNamespace(reservation=reserve())
        worker.queue.put_nowait((job.reservation.ticket, 1, job))
        try:
            worker._stopped(old)
            assert worker.queue.qsize() == 1
            assert busy()
        finally:
            worker._drain()
            replacement.cancel()
            with pytest.raises(asyncio.CancelledError):
                await replacement

    asyncio.run(scenario())
