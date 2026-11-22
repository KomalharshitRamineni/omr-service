"""Jobs are kept in storage, not in memory, so they survive restarts and scaling to zero.

Each job has jobs/<id>/input.pdf, jobs/<id>/status.json and, once done, jobs/<id>/result.musicxml. Each page
is its own message on the queue, so several workers can read the pages of one PDF at the same time. A worker
writes pages/pNN.start when it starts a page and pages/pNN.musicxml (or pages/pNN.error, the reason it was
skipped) when it finishes; whoever finishes the last page joins them (worker.py). Only the API (on upload) and
the joining worker write status.json; while a job runs, its progress is read from the page files."""

import json
import re
import time

MUSICXML_TYPE = "application/vnd.recordare.musicxml+xml"
PUBLIC_FIELDS = ("id", "status", "pages_total", "pages_done", "error", "warnings", "check_bars")


class QueueFull(Exception):
    pass


def valid_id(job_id):
    return re.fullmatch(r"[0-9a-f]{32}", job_id) is not None


def blob(job_id, name):
    return f"jobs/{job_id}/{name}"


def page_blob(job_id, page, kind):
    """kind: "start", "musicxml" (the page as homr read it) or "error" (why it was skipped)."""
    return blob(job_id, f"pages/p{page:03d}.{kind}")


def page_message(job_id, page):
    return json.dumps({"job": job_id, "page": page})


def read_message(text):
    """(job id, page) for a page message; (job id, None) for a whole-job message (an id on its own, as
    sent before pages were queued separately)."""
    if valid_id(text):
        return text, None
    data = json.loads(text)
    if not valid_id(data["job"]) or not isinstance(data["page"], int) or data["page"] < 1:
        raise ValueError("not a page message")
    return data["job"], data["page"]


WARM_MESSAGE = json.dumps({"warm": True})


def is_warm(text):
    try:
        return json.loads(text) == {"warm": True}
    except ValueError:
        return False


def warm(store, settings):
    """Queues a warm-up message, which starts a worker that then waits for pages (worker.py). Not sent while
    pages are queued or being read (workers are starting anyway), nor more than once per WARM_EVERY_SECONDS.
    Returns True if sent."""
    if store.queue_length() > 0:
        return False
    window = int(time.time() // settings.warm_every_s)
    if not store.create(f"warm/{window}"):  # another caller warmed in this window
        return False
    store.send(WARM_MESSAGE)
    return True


def send_pages(store, job_id, pages_total):
    for page in range(1, pages_total + 1):
        store.send(page_message(job_id, page))


def submit(store, settings, job_id, filename, pdf, pages_total, owner=None):
    if store.queue_length() + pages_total > settings.max_queued_pages:
        raise QueueFull
    store.put(blob(job_id, "input.pdf"), pdf, "application/pdf")
    status = {
        "id": job_id, "status": "queued",  # queued -> running -> done | failed
        "pages_total": pages_total, "pages_done": 0, "error": None, "warnings": [], "check_bars": [],
        "filename": filename,
        "owner": owner,  # the signed-in user, once the website has accounts
        "created": time.time(), "finished": None,
    }
    save(store, status)
    send_pages(store, job_id, pages_total)
    return status


def load(store, job_id):
    data = store.get(blob(job_id, "status.json"))
    return json.loads(data) if data else None


def save(store, status):
    store.put(blob(status["id"], "status.json"), json.dumps(status).encode(), "application/json")


def page_files(store, job_id):
    """{page: set of kinds written so far}."""
    found = {}
    for name in store.list(blob(job_id, "pages/")):
        match = re.fullmatch(r".*/pages/p(\d+)\.(start|musicxml|error)", name)
        if match:
            found.setdefault(int(match[1]), set()).add(match[2])
    return found


def finished_pages(files):
    return {page for page, kinds in files.items() if kinds & {"musicxml", "error"}}


def with_progress(store, status):
    """A queued or running job's status with its progress read from the page files."""
    if status["status"] not in ("queued", "running"):
        return status
    files = page_files(store, status["id"])
    if not files:
        return status
    return dict(status, status="running", pages_done=len(finished_pages(files)))


def public(status):
    return {key: status.get(key, [] if key == "check_bars" else None) for key in PUBLIC_FIELDS}  # older jobs lack check_bars


def expired(status, settings):
    ttl = settings.result_ttl_s
    return ttl > 0 and status["finished"] is not None and status["finished"] < time.time() - ttl
