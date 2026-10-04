from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path


class CapacityError(RuntimeError):
    pass


class CapacityBusy(CapacityError):
    pass


class CapacityTimeout(CapacityBusy):
    pass


_current_action = ContextVar("llm_action", default=None)


def _directory():
    configured = os.getenv("LLM_CAPACITY_DIR")
    if configured:
        return Path(configured)
    from db import connection

    return Path(connection.DB_FILE).resolve().parent / ".llm-capacity"


def _open(path):
    return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)


@contextmanager
def _admission(directory, deadline, *, wait=True):
    fd = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = _open(directory / "admission")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    yield False
                    return
                if time.monotonic() >= deadline:
                    raise CapacityTimeout("LLM admission timed out.")
                time.sleep(0.01)
        yield True
    except OSError as exc:
        raise CapacityError("LLM capacity storage is unavailable.") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _positions(directory):
    live = []
    free = []
    for index in range(3):
        path = directory / f"position-{index}"
        fd = _open(path)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(path)
            except BlockingIOError:
                try:
                    ticket = int(os.pread(fd, 64, 0).decode("ascii"))
                    if ticket < 1:
                        raise ValueError
                except (UnicodeError, ValueError) as exc:
                    raise CapacityError("LLM capacity state is invalid.") from exc
                live.append((ticket, path))
        finally:
            os.close(fd)
    return live, free


def reserve(*, policy="interactive", queue_timeout=None):
    if policy not in {"interactive", "background"}:
        raise ValueError("Unknown LLM capacity policy.")
    if queue_timeout is None:
        queue_timeout = float(os.getenv("LLAMA_CPP_TIMEOUT", "180"))
    deadline = time.monotonic() + max(0.0, queue_timeout)
    directory = _directory()
    with _admission(directory, deadline):
        live, free = _positions(directory)
        if not free or (policy == "background" and live):
            raise CapacityBusy("LLM capacity is busy; try again in a moment.")
        ticket = max((entry[0] for entry in live), default=0) + 1
        fd = _open(free[0])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(ticket).encode("ascii"), 0)
        except BaseException:
            os.close(fd)
            raise
    return Reservation(directory, free[0], fd, ticket, deadline)


def busy():
    directory = _directory()
    with _admission(directory, time.monotonic() + 1):
        live, _ = _positions(directory)
        return bool(live)


class Reservation:
    def __init__(self, directory, path, fd, ticket, deadline):
        self.directory = directory
        self.path = path
        self.ticket = ticket
        self.deadline = deadline
        self._fd = fd
        self._execution_fd = None
        self._released = False
        self._users = 0
        self._lock = threading.RLock()
        self._request_lock = threading.RLock()

    def try_activate(self):
        with self._lock:
            if self._released:
                raise CapacityError("LLM action has been released.")
            if self._execution_fd is not None:
                return True
            if time.monotonic() >= self.deadline:
                self.release()
                raise CapacityTimeout("LLM capacity queue timed out.")
            with _admission(self.directory, self.deadline, wait=False) as admitted:
                if not admitted:
                    return False
                live, _ = _positions(self.directory)
                if not live or min(live)[1] != self.path:
                    return False
                fd = _open(self.directory / "execution")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
                except BaseException:
                    os.close(fd)
                    raise
                self._execution_fd = fd
                return True

    def activate(self):
        try:
            while not self.try_activate():
                time.sleep(0.01)
        except BaseException:
            self.release()
            raise

    def _close(self):
        if self._execution_fd is not None:
            os.close(self._execution_fd)
            self._execution_fd = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def release(self):
        with self._lock:
            self._released = True
            if not self._users:
                self._close()

    @contextmanager
    def execution(self):
        with self._request_lock:
            with self._lock:
                if self._released or self._execution_fd is None:
                    raise CapacityError("LLM action is no longer active.")
                self._users += 1
            try:
                yield
            finally:
                with self._lock:
                    self._users -= 1
                    if self._released and not self._users:
                        self._close()


@contextmanager
def action_context(reservation=None, *, policy="interactive", queue_timeout=None):
    current = _current_action.get()
    if current is not None:
        if reservation is not None and reservation is not current:
            raise CapacityError("A different LLM action is already active.")
        yield current
        return
    if reservation is None:
        reservation = reserve(policy=policy, queue_timeout=queue_timeout)
    token = None
    try:
        reservation.activate()
        token = _current_action.set(reservation)
        yield reservation
    finally:
        if token is not None:
            _current_action.reset(token)
        reservation.release()


@asynccontextmanager
async def async_action_context(reservation=None, *, policy="interactive", queue_timeout=None):
    current = _current_action.get()
    if current is not None:
        if reservation is not None and reservation is not current:
            raise CapacityError("A different LLM action is already active.")
        yield current
        return
    if reservation is None:
        reservation = reserve(policy=policy, queue_timeout=queue_timeout)
    token = None
    try:
        while not await asyncio.to_thread(reservation.try_activate):
            await asyncio.sleep(0.01)
        token = _current_action.set(reservation)
        yield reservation
    finally:
        if token is not None:
            _current_action.reset(token)
        reservation.release()


def admitted_request(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with action_context(
            policy=kwargs.get("capacity_policy", "interactive"),
            queue_timeout=kwargs.get("queue_timeout"),
        ) as reservation:
            with reservation.execution():
                return function(*args, **kwargs)

    return wrapped
