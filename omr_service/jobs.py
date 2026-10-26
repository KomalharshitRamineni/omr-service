"""In-memory jobs, converted one at a time by a background thread (homr needs about 1 GB per page)."""

import logging
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field

from .pipeline import ConversionError, convert

log = logging.getLogger("omr")


class QueueFull(Exception):
    pass


@dataclass
class Job:
    id: str
    filename: str
    pages_total: int
    status: str = "queued"  # queued -> running -> done | failed
    pages_done: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    result: bytes | None = None
    finished: float | None = None

    def public(self):
        return {"id": self.id, "status": self.status, "pages_total": self.pages_total,
                "pages_done": self.pages_done, "error": self.error, "warnings": self.warnings}


class JobStore:
    def __init__(self, settings):
        self.settings = settings
        self._jobs = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._stop = threading.Event()

    def job_dir(self, job_id):
        return self.settings.work_dir / job_id

    def start(self):
        threading.Thread(target=self._run, name="omr-worker", daemon=True).start()

    def stop(self):
        self._stop.set()

    def submit(self, job_id, filename, pages_total):
        with self._lock:
            waiting = sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
            if waiting >= self.settings.max_queued_jobs:
                raise QueueFull
            job = self._jobs[job_id] = Job(job_id, filename, pages_total)
        self._queue.put(job_id)
        return job

    def get(self, job_id):
        self._purge()
        with self._lock:
            return self._jobs.get(job_id)

    def _purge(self):
        cutoff = time.time() - self.settings.result_ttl_s
        with self._lock:
            for job_id in [j.id for j in self._jobs.values() if j.finished and j.finished < cutoff]:
                del self._jobs[job_id]

    def _run(self):
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=30)
            except queue.Empty:
                self._purge()
                continue
            with self._lock:
                job = self._jobs.get(job_id)
            if job:
                self._process(job)

    def _process(self, job):
        job.status = "running"
        work = self.job_dir(job.id)

        def on_page(done, total):
            job.pages_done = done

        try:
            job.result, job.warnings = convert(work / "input.pdf", work, self.settings, on_page)
            job.status = "done"
        except ConversionError as ex:
            job.error, job.status = str(ex), "failed"
        except Exception:
            log.exception("job %s failed", job.id)
            job.error, job.status = "internal error", "failed"
        finally:
            job.finished = time.time()
            shutil.rmtree(work, ignore_errors=True)
