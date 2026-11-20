"""Converts queued pages: `python -m omr_service.worker` takes pages off the queue until it is empty, then exits
(in Azure, a Container Apps job starts it when messages arrive, as many runs as the queue needs). `--watch`
keeps polling, for local runs.

Each page of a PDF is its own message, so the pages of one PDF are read at the same time by as many workers as
are running, PAGE_WORKERS pages per worker. homr stays loaded in each worker between pages (homr_server). The
worker that finishes a job's last page joins the pages into one score.

A page's message stays on the queue, hidden, while the page is read, and is deleted only once the page's result
(or the reason it was skipped) is stored. If the worker stops part-way, the message reappears and another run
retries the page, up to MAX_ATTEMPTS deliveries; after that the page is skipped. If the worker stops while
joining, the job stays "running" (the join is not retried)."""

import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import jobs
from .config import Settings
from .pipeline import ConversionError, Homr, convert_page, join_pages
from .storage import make_store

log = logging.getLogger("omr.worker")


def finish(store, status, error=None, warnings=(), check_bars=()):
    status.update(status="failed" if error else "done", error=error, warnings=list(warnings),
                  check_bars=list(check_bars), finished=time.time())
    jobs.save(store, status)


def _work_dir(settings):
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=settings.work_dir))


def join_if_complete(store, settings, job_id):
    """Joins the job's pages if all are finished and no other worker has started joining them."""
    status = jobs.load(store, job_id)
    if not status or status["status"] in ("done", "failed"):
        return
    files = jobs.page_files(store, job_id)
    finished = jobs.finished_pages(files)
    if len(finished) < status["pages_total"]:
        return
    if not store.create(jobs.blob(job_id, "join.lock")):
        return  # another worker finished a page at the same moment and is joining
    status["pages_done"] = len(finished)
    work = _work_dir(settings)
    try:
        results, failures = {}, {}
        for page in sorted(finished):
            if "musicxml" in files[page]:
                path = work / f"p{page:02d}" / f"p{page:02d}.musicxml"  # as the page was read
                path.parent.mkdir()
                path.write_bytes(store.get(jobs.page_blob(job_id, page, "musicxml")))
                results[page] = path
            else:
                failures[page] = store.get(jobs.page_blob(job_id, page, "error")).decode()
        try:
            result, warnings, check_bars = join_pages(results, failures, work / "result.musicxml")
        except ConversionError as ex:
            skipped = [f"page {page} skipped: {failures[page]}" for page in sorted(failures)]
            finish(store, status, error=str(ex), warnings=skipped)
            return
        store.put(jobs.blob(job_id, "result.musicxml"), result, jobs.MUSICXML_TYPE)
        finish(store, status, warnings=warnings, check_bars=check_bars)
    except Exception:
        log.exception("job %s: joining failed", job_id)
        finish(store, status, error="internal error")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def convert_one_page(store, settings, homr, message, job_id, page):
    done = jobs.finished_pages(jobs.page_files(store, job_id))
    if page in done:  # delivered twice
        store.delete(message)
        return
    if message.dequeue_count > settings.max_attempts:
        store.put(jobs.page_blob(job_id, page, "error"), f"gave up after {settings.max_attempts} attempts".encode())
        store.delete(message)
        return
    store.put(jobs.page_blob(job_id, page, "start"), b"")
    work = _work_dir(settings)
    try:
        pdf = work / "input.pdf"
        pdf.write_bytes(store.get(jobs.blob(job_id, "input.pdf")))
        try:
            xml = convert_page(pdf, page, work, settings, homr)
            store.put(jobs.page_blob(job_id, page, "musicxml"), xml.read_bytes(), jobs.MUSICXML_TYPE)
        except ConversionError as ex:
            store.put(jobs.page_blob(job_id, page, "error"), str(ex).encode())
    except Exception:
        log.exception("job %s page %d failed on attempt %d", job_id, page, message.dequeue_count)
        if message.dequeue_count < settings.max_attempts:
            return  # the message reappears after its lease and is retried
        store.put(jobs.page_blob(job_id, page, "error"), b"internal error")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    store.delete(message)


def process_next(store, settings, homr=None):
    """Converts one queued page (or splits a whole-job message into pages). Returns False when the queue is
    empty."""
    message = store.receive(settings.page_timeout_s + 300)  # hidden long enough for one page
    if not message:
        return False
    try:
        job_id, page = jobs.read_message(message.text)
    except (ValueError, KeyError, TypeError):
        log.warning("dropping a message that is not a page: %.80r", message.text)
        store.delete(message)
        return True
    status = jobs.load(store, job_id)
    if not status or status["status"] in ("done", "failed"):
        store.delete(message)
        return True
    if page is None:  # one message for the whole job: queue its pages instead
        jobs.send_pages(store, job_id, status["pages_total"])
        store.delete(message)
        return True
    own = homr is None
    homr = homr or Homr(settings, slots=1)
    try:
        convert_one_page(store, settings, homr, message, job_id, page)
    finally:
        if own:
            homr.close()
    join_if_complete(store, settings, job_id)
    return True


def drain(store, settings, homr, max_errors=3):
    """Converts pages until the queue is empty. An unexpected error (storage, network) is logged and the loop
    goes on, so one slot is not lost; a page's message reappears after its lease. It stops after max_errors
    errors in a row."""
    errors = 0
    while errors < max_errors:
        try:
            if not process_next(store, settings, homr):
                return
            errors = 0
        except Exception:
            errors += 1
            log.exception("error %d of %d in a row", errors, max_errors)
            time.sleep(2 * errors)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # the SDK logs every request at INFO
    settings = Settings()
    store = make_store(settings)
    homr = Homr(settings)
    watch = "--watch" in sys.argv[1:]
    try:
        while True:
            # PAGE_WORKERS pages at once, each with its own homr process from the pool
            threads = [threading.Thread(target=drain, args=(store, settings, homr))
                       for _ in range(settings.page_workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if not watch:
                return
            time.sleep(2)
    finally:
        homr.close()


if __name__ == "__main__":
    main()
