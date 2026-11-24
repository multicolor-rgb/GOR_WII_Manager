"""Background job queue used by the GUI for long running transfers."""

from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .util import CancelledError


class Job:
    _ids = itertools.count(1)

    def __init__(self, title: str, runner, detail: str = ""):
        self.id = next(Job._ids)
        self.title = title
        self.detail = detail
        self.status = "queued"  # queued | running | done | error | cancelled
        self.total = 0
        self.done = 0
        self.error = ""
        self.result = None
        self.volumes: set[str] = set()
        self.cancel_event = threading.Event()
        self.started_at = 0.0
        self.finished_at = 0.0
        self.speed = 0.0
        self._runner = runner
        self._last_done = 0
        self._last_time = 0.0
        self._queue: "JobQueue | None" = None

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}

    @property
    def fraction(self) -> float:
        if not self.total:
            return 0.0
        return min(1.0, max(0.0, self.done / self.total))

    @property
    def eta(self) -> float | None:
        if not self.active or self.speed <= 0 or not self.total:
            return None
        remaining = max(0, self.total - self.done)
        return remaining / self.speed

    def progress(self, done: int, total: int = 0) -> None:
        now = time.monotonic()
        if total:
            self.total = total
        self.done = done
        if now - self._last_time >= 0.4:
            elapsed = max(now - self._last_time, 1e-6)
            self.speed = max(0.0, (done - self._last_done) / elapsed)
            self._last_done = done
            self._last_time = now
        if self._queue is not None:
            self._queue.notify(self)

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._queue is not None:
            self._queue.notify(self)

    @property
    def cancellable(self) -> bool:
        return self.active


class JobQueue:
    def __init__(self, on_change=None):
        self.jobs: list[Job] = []
        self._on_change = on_change
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wbfs-job")
        self._lock = threading.Lock()

    def set_callback(self, callback) -> None:
        self._on_change = callback

    def notify(self, job: Job | None = None) -> None:
        callback = self._on_change
        if callback is not None:
            try:
                callback(job)
            except Exception:
                pass

    def add(self, title: str, runner, detail: str = "") -> Job:
        job = Job(title, runner, detail)
        job._queue = self
        with self._lock:
            self.jobs.append(job)
        self.notify(job)
        self._executor.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.monotonic()
            self.notify(job)
            return
        job.status = "running"
        job.started_at = time.monotonic()
        job._last_time = job.started_at
        self.notify(job)
        try:
            job.result = job._runner(job)
            if job.cancel_event.is_set():
                job.status = "cancelled"
            else:
                job.status = "done"
                if job.total and job.done < job.total:
                    job.done = job.total
        except CancelledError as error:
            job.status = "cancelled"
            job.error = str(error)
        except Exception as error:  # noqa: BLE001 - shown to the user
            job.status = "error"
            job.error = str(error) or error.__class__.__name__
        finally:
            job.finished_at = time.monotonic()
            job.speed = 0.0
            self.notify(job)

    def active_jobs(self) -> list[Job]:
        return [job for job in self.jobs if job.active]

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [job for job in self.jobs if job.active]
        self.notify(None)

    def shutdown(self, wait: bool = False) -> None:
        for job in self.jobs:
            if job.active:
                job.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)
