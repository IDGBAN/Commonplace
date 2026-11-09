"""What `journal browse` does in the background: answers and upkeep jobs.

An answer used to be written inside the request that asked for it, so
reloading the page, or closing it, stopped the model partway. Now a question
starts a Run that carries on whoever is listening, and a page can pick its
events up again from the start.

Upkeep jobs (digests, the profile, themes) run one at a time on a worker
thread, the only place the browser writes to the cache from. Jobs and answers
share one model through a Lane: a job gives way between its steps, so a
question waits for the step in hand and no longer than that.

A lock file next to the cache says which process is doing upkeep, so the
browser can tell when `journal watch` or `journal index` is at it in a
terminal and leave it alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config

Json = dict[str, Any]

# finished answers and jobs kept for a page that comes back to them
RUNS_KEPT = 30
JOBS_KEPT = 20
# a page following a quiet answer hears this often that it's still alive
KEEPALIVE_SECONDS = 15.0


class Cancelled(Exception):
    """Raised inside an answer or a job when it's been told to stop."""


# The lock file


def lock_path(config: Config) -> Path:
    cache = config.db.path
    return cache.with_name(f"{cache.stem}.lock")


# far past anything written to the file, so the note in it stays readable
_LOCK_AT = 1 << 30


def _try_lock(handle: Any) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(_LOCK_AT)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            handle.seek(_LOCK_AT)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def hold(config: Config, what: str) -> Iterator[bool]:
    """Says this process is doing `what` to the cache until the block ends.

    Yields False when another process already holds it. The operating system
    lets go of the lock when a process dies, so a crash can't leave it stuck.
    """
    path = lock_path(config)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError:
        yield True  # nowhere to say so, which is no reason not to work
        return
    try:
        if not _try_lock(handle):
            yield False
            return
        try:
            note = {"pid": os.getpid(), "what": what, "since": datetime.now(UTC).isoformat()}
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(note).encode())
            handle.flush()
            yield True
        finally:
            _unlock(handle)
    finally:
        handle.close()


def holder(config: Config) -> Json | None:
    """Who holds the lock, as {pid, what, since}, or None when nobody does."""
    path = lock_path(config)
    if not path.is_file():
        return None
    try:
        with path.open("r+b") as handle:
            if _try_lock(handle):
                _unlock(handle)
                return None
            handle.seek(0)
            note = json.loads(handle.read(4096) or b"{}")
    except (OSError, ValueError):
        return None
    return note if isinstance(note, dict) and "pid" in note else {"pid": 0, "what": "something", "since": None}


# One model, shared


class Lane:
    """Lets one thing use the models at a time, and questions go first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._asking = 0

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @contextlib.contextmanager
    def asking(self) -> Iterator[None]:
        with self._cond:
            self._asking += 1
        try:
            with self._lock:
                yield
        finally:
            with self._cond:
                self._asking -= 1
                self._cond.notify_all()

    @contextlib.contextmanager
    def working(self) -> Iterator[None]:
        with self._lock:
            yield

    def give_way(self) -> float:
        """Called by a job between steps, while it's `working`. Lets any
        questions through and returns how many seconds that took."""
        with self._cond:
            if not self._asking:
                return 0.0
        started = time.monotonic()
        self._lock.release()
        try:
            with self._cond:
                self._cond.wait_for(lambda: self._asking == 0)
        finally:
            self._lock.acquire()
        return time.monotonic() - started


# Answers


class Run:
    """One answer being written, and everything it has said so far."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.events: list[tuple[str, Json]] = []
        self.done = False
        self._cond = threading.Condition()
        self._cancel = threading.Event()

    def send(self, event: str, data: Json) -> None:
        """Adds an event. Raises Cancelled once the answer has been stopped,
        which is what ends the model call that was feeding it."""
        if self._cancel.is_set():
            raise Cancelled
        self._add(event, data)

    def _add(self, event: str, data: Json) -> None:
        with self._cond:
            self.events.append((event, data))
            self._cond.notify_all()

    def cancel(self) -> None:
        self._cancel.set()

    def _finish(self) -> None:
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def follow(self, start: int = 0) -> Iterator[tuple[str, Json] | None]:
        """Events from `start` on, as they come, until the answer is finished.
        None now and then while nothing is happening."""
        at = max(start, 0)
        while True:
            with self._cond:
                if at >= len(self.events) and not self.done:
                    self._cond.wait(KEEPALIVE_SECONDS)
                batch = self.events[at:]
                finished = self.done
            if not batch and not finished:
                yield None
            yield from batch
            at += len(batch)
            if finished and at >= len(self.events):
                return


class Answers:
    def __init__(self, lane: Lane) -> None:
        self._lane = lane
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._guard = threading.Lock()

    def start(self, work: Callable[[Run], None], waiting: str | None = None) -> Run:
        """Runs `work` on a thread of its own. `waiting` is said first when an
        upkeep step has the model and the question has to wait for it."""
        run = Run()
        with self._guard:
            self._runs[run.id] = run
            while len(self._runs) > RUNS_KEPT:
                oldest = next(iter(self._runs))
                if not self._runs[oldest].done:
                    break
                del self._runs[oldest]

        def go() -> None:
            try:
                if waiting and self._lane.busy:
                    run.send("stage", {"text": waiting})
                with self._lane.asking():
                    work(run)
            except Cancelled:
                run._add("cancelled", {})
            except Exception as exc:  # noqa: BLE001 - otherwise the page waits on nothing
                run._add("problem", {"error": f"Commonplace hit a bug: {type(exc).__name__}: {exc}"})
            finally:
                run._finish()

        threading.Thread(target=go, name=f"answer-{run.id[:8]}", daemon=True).start()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._guard:
            return self._runs.get(run_id)


# Upkeep jobs


@dataclass
class Job:
    id: int
    kind: str
    label: str
    params: Json = field(default_factory=dict)
    # queued, running, done, failed or stopped
    status: str = "queued"
    done: int = 0
    total: int = 0
    message: str = ""
    result: str = ""
    error: str | None = None
    queued_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    # seconds spent on steps, and waiting for questions, since it started
    worked: float = 0.0
    paused: float = 0.0
    stopping: bool = False

    def as_json(self) -> Json:
        data = asdict(self)
        data["seconds_left"] = (
            round(self.worked / self.done * (self.total - self.done))
            if self.status == "running" and self.done and self.total > self.done else None
        )
        return data


class Step:
    """What a running job is handed, to say how far it is and to give way."""

    def __init__(self, job: Job, jobs: Jobs) -> None:
        self._job = job
        self._jobs = jobs
        self._since = time.monotonic()

    def __call__(self, done: int, total: int, message: str) -> None:
        """Call before each step. Raises Cancelled if the job was told to stop."""
        now = time.monotonic()
        waited = self._jobs.lane.give_way()
        with self._jobs.guard:
            job = self._job
            if done > job.done:
                job.worked += now - self._since
            job.paused += waited
            job.done, job.total, job.message = done, total, message
            stopping = job.stopping
        self._since = time.monotonic()
        if stopping:
            raise Cancelled

    def say(self, message: str) -> None:
        with self._jobs.guard:
            self._job.message = message


Runner = Callable[[Job, Step], str]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Jobs:
    """A line of upkeep jobs and the one thread that works through it."""

    def __init__(self, config: Config, lane: Lane, runners: dict[str, Runner]) -> None:
        self.config = config
        self.lane = lane
        self.guard = threading.Lock()
        self._runners = runners
        self._jobs: list[Job] = []
        self._next = 1
        self._wake = threading.Condition(self.guard)
        self._thread: threading.Thread | None = None

    def add(self, kind: str, label: str, params: Json | None = None) -> Job:
        """Puts a job in line. The same job already waiting or running is returned as it is."""
        params = params or {}
        with self._wake:
            for job in self._jobs:
                if job.status in ("queued", "running") and (job.kind, job.params) == (kind, params):
                    return job
            job = Job(self._next, kind, label, params, queued_at=_now())
            self._next += 1
            self._jobs.append(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="upkeep", daemon=True)
                self._thread.start()
            self._wake.notify_all()
            return job

    def stop(self) -> bool:
        """Asks the running job to stop after the step it's on."""
        with self.guard:
            for job in self._jobs:
                if job.status == "running":
                    job.stopping = True
                    return True
        return False

    def remove(self, job_id: int) -> bool:
        with self.guard:
            for job in self._jobs:
                if job.id == job_id and job.status == "queued":
                    self._jobs.remove(job)
                    return True
        return False

    def snapshot(self) -> Json:
        with self.guard:
            jobs = [job.as_json() for job in self._jobs]
        return {
            "running": next((j for j in jobs if j["status"] == "running"), None),
            "queued": [j for j in jobs if j["status"] == "queued"],
            "recent": [j for j in reversed(jobs) if j["status"] in ("done", "failed", "stopped")],
        }

    def idle(self) -> bool:
        with self.guard:
            return not any(job.status in ("queued", "running") for job in self._jobs)

    def _take(self) -> Job:
        with self._wake:
            while True:
                job = next((j for j in self._jobs if j.status == "queued"), None)
                if job is not None:
                    job.status, job.started_at = "running", _now()
                    return job
                self._wake.wait()

    def _work(self) -> None:
        while True:
            job = self._take()
            status, result, error = "done", "", None
            try:
                with hold(self.config, job.label) as ours:
                    if not ours:
                        other = holder(self.config) or {}
                        raise RuntimeError(
                            f"{other.get('what', 'Something else')} is already working on the cache. "
                            "Try again once it's finished."
                        )
                    with self.lane.working():
                        result = self._runners[job.kind](job, Step(job, self))
            except Cancelled:
                status, result = "stopped", "Stopped. What was done is kept, and the rest is still waiting."
            except Exception as exc:  # noqa: BLE001 - a job that fails mustn't take the worker with it
                status, error = "failed", str(exc) or type(exc).__name__
            with self.guard:
                job.status, job.result, job.error, job.finished_at = status, result, error, _now()
                finished = [j for j in self._jobs if j.status in ("done", "failed", "stopped")]
                for old in finished[:-JOBS_KEPT]:
                    self._jobs.remove(old)
