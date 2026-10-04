import asyncio
import fcntl
import multiprocessing
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from llm.capacity import (
    CapacityBusy,
    CapacityError,
    CapacityTimeout,
    action_context,
    async_action_context,
    busy,
    reserve,
)
from llm.client import query_llm


def _process_request(connection, directory, policy, queue_timeout):
    os.environ["LLM_CAPACITY_DIR"] = directory
    os.environ["LLAMA_CPP_ALLOWED_MODELS"] = "discord-bot"
    os.environ["LLAMA_CPP_DEFAULT_MODEL"] = "discord-bot"
    from llm import client

    reservation = None

    def post(*args, **kwargs):
        connection.send(("http", reservation.ticket))
        assert connection.recv() == "finish"
        return SimpleNamespace(
            ok=True,
            json=lambda: {"choices": [{"message": {"content": "ok"}}]},
        )

    client.requests.post = post
    try:
        reservation = reserve(policy=policy, queue_timeout=queue_timeout)
        connection.send(("reserved", reservation.ticket))
        assert connection.recv() == "run"
        with action_context(reservation):
            result = client.query_llm("test")
        connection.send(("done", result))
    except CapacityError as exc:
        connection.send((type(exc).__name__, str(exc)))
    finally:
        if reservation is not None:
            reservation.release()
        connection.close()


@contextmanager
def _process(directory, *, policy="interactive", queue_timeout=10):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_process_request,
        args=(child, directory, policy, queue_timeout),
    )
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()


def _receive(connection, expected):
    assert connection.poll(5), expected
    message = connection.recv()
    assert message[0] == expected, message
    return message[1]


def test_process_requests_have_two_waiters_and_fifo_http(monkeypatch):
    directory = os.environ["LLM_CAPACITY_DIR"]
    with _process(directory) as (_, first):
        assert _receive(first, "reserved") == 1
        first.send("run")
        _receive(first, "http")
        with _process(directory) as (_, second):
            assert _receive(second, "reserved") == 2
            second.send("run")
            with _process(directory) as (_, third):
                assert _receive(third, "reserved") == 3
                third.send("run")
                with _process(directory) as (_, fourth):
                    _receive(fourth, "CapacityBusy")
                with _process(directory, policy="background") as (_, background):
                    _receive(background, "CapacityBusy")
                assert not second.poll(0.05)
                assert not third.poll(0.05)
                first.send("finish")
                _receive(first, "done")
                _receive(second, "http")
                assert not third.poll(0.05)
                second.send("finish")
                _receive(second, "done")
                _receive(third, "http")
                third.send("finish")
                _receive(third, "done")
    assert busy() is False


@pytest.mark.parametrize("kill_active", [True, False])
def test_process_death_recovers_locked_position(kill_active):
    directory = os.environ["LLM_CAPACITY_DIR"]
    with _process(directory) as (owner, first):
        _receive(first, "reserved")
        if kill_active:
            first.send("run")
            _receive(first, "http")
        with _process(directory) as (_, waiting):
            _receive(waiting, "reserved")
            waiting.send("run")
            assert not waiting.poll(0.05)
            owner.kill()
            owner.join(5)
            _receive(waiting, "http")
            waiting.send("finish")
            _receive(waiting, "done")
    with action_context(policy="background"):
        assert busy() is True
    assert busy() is False


def test_waiting_process_deadline_releases_its_position():
    owner = reserve()
    owner.activate()
    try:
        with _process(os.environ["LLM_CAPACITY_DIR"], queue_timeout=0.1) as (_, waiter):
            _receive(waiter, "reserved")
            waiter.send("run")
            _receive(waiter, "CapacityTimeout")
        second = reserve()
        third = reserve()
        try:
            with pytest.raises(CapacityBusy):
                reserve()
        finally:
            second.release()
            third.release()
    finally:
        owner.release()
    assert not busy()


def test_waiting_async_cancellation_releases_its_position():
    async def scenario():
        owner = reserve()
        owner.activate()
        waiter = reserve()

        async def waiting():
            async with async_action_context(waiter):
                raise AssertionError("Unexpected activation")

        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = reserve()
        third = reserve()
        second.release()
        third.release()
        owner.release()
        assert not busy()

    asyncio.run(scenario())


def test_activation_does_not_block_on_admission_lock():
    reservation = reserve()
    gate = os.open(str(reservation.directory / "admission"), os.O_RDWR)
    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert reservation.try_activate() is False
        reservation.release()
    finally:
        os.close(gate)
    assert not busy()


@pytest.mark.parametrize("outer_action", [True, False])
def test_cancelled_to_thread_keeps_execution_until_http_finishes(monkeypatch, outer_action):
    started = threading.Event()
    finish = threading.Event()
    ended = threading.Event()

    def post(*args, **kwargs):
        started.set()
        assert finish.wait(10)
        return SimpleNamespace(
            ok=True,
            json=lambda: {"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setattr("llm.client.requests.post", post)

    def inference():
        try:
            return query_llm("test")
        finally:
            ended.set()

    async def scenario():
        async def request():
            if outer_action:
                async with async_action_context():
                    return await asyncio.to_thread(inference)
            return await asyncio.to_thread(inference)

        task = asyncio.create_task(request())
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert busy()
        with pytest.raises(CapacityBusy):
            reserve(policy="background")
        with _process(os.environ["LLM_CAPACITY_DIR"]) as (_, waiting):
            _receive(waiting, "reserved")
            waiting.send("run")
            assert not waiting.poll(0.1)
            finish.set()
            assert await asyncio.to_thread(ended.wait, 5)
            _receive(waiting, "http")
            waiting.send("finish")
            _receive(waiting, "done")
        assert not busy()

    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_storage_failure_fails_closed(tmp_path, monkeypatch):
    unavailable = tmp_path / "file"
    unavailable.write_text("not a directory")
    monkeypatch.setenv("LLM_CAPACITY_DIR", str(unavailable))
    with pytest.raises(CapacityError, match="unavailable"):
        reserve()


def test_default_storage_is_beside_configured_database(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_CAPACITY_DIR")
    monkeypatch.setattr("db.connection.DB_FILE", str(tmp_path / "data" / "test.db"))
    reservation = reserve()
    try:
        assert reservation.directory == tmp_path / "data" / ".llm-capacity"
    finally:
        reservation.release()
