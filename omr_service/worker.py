"""Converts queued jobs: `python -m omr_service.worker` takes jobs off the queue until it is empty, then exits
(in Azure, a Container Apps job starts it when messages arrive). `--watch` keeps polling, for local runs.

A job's message stays on the queue, hidden, while it is converted, and is deleted only at the end. If the worker
stops part-way (a crash, or the container is stopped), the message reappears and another run retries the job,
up to MAX_ATTEMPTS deliveries."""

import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import jobs
from .config import Settings
from .pipeline import ConversionError, convert
from .storage import make_store

log = logging.getLogger("omr.worker")


def finish(store, status, error=None, warnings=()):
    status.update(status="failed" if error else "done", error=error, warnings=list(warnings), finished=time.time())
    jobs.save(store, status)


def process_next(store, settings):
    """Converts one queued job. Returns False when the queue is empty."""
    lease = settings.page_timeout_s + 300  # hidden long enough for one page; renewed after each page
    message = store.receive(lease)
    if not message:
        return False
    status = jobs.load(store, message.text)
    if not status or status["status"] in ("done", "failed"):
        store.delete(message)
        return True
    if message.dequeue_count > settings.max_attempts:
        finish(store, status, error=f"gave up after {settings.max_attempts} attempts")
        store.delete(message)
        return True

    status.update(status="running", pages_done=0)
    jobs.save(store, status)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=settings.work_dir))
    try:
        pdf = work / "input.pdf"
        pdf.write_bytes(store.get(jobs.blob(status["id"], "input.pdf")))

        def on_page(done, total):
            status["pages_done"] = done
            jobs.save(store, status)
            store.extend(message, lease)

        result, warnings = convert(pdf, work, settings, on_page)
        store.put(jobs.blob(status["id"], "result.musicxml"), result, jobs.MUSICXML_TYPE)
        finish(store, status, warnings=warnings)
    except ConversionError as ex:
        finish(store, status, error=str(ex))
    except Exception:
        log.exception("job %s failed on attempt %d", status["id"], message.dequeue_count)
        if message.dequeue_count < settings.max_attempts:
            status.update(status="queued")  # the message reappears after the lease and is retried
            jobs.save(store, status)
            return True
        finish(store, status, error="internal error")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    store.delete(message)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the SDK logs every request at INFO
    settings = Settings()
    store = make_store(settings)
    watch = "--watch" in sys.argv[1:]
    while True:
        if not process_next(store, settings):
            if not watch:
                return
            time.sleep(2)


if __name__ == "__main__":
    main()
