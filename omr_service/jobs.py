"""Jobs are kept in storage, not in memory, so they survive restarts and scaling to zero.
Each job has jobs/<id>/input.pdf, jobs/<id>/status.json and, once done, jobs/<id>/result.musicxml;
its id goes on the queue, and a worker (worker.py) converts it."""

import json
import re
import time

MUSICXML_TYPE = "application/vnd.recordare.musicxml+xml"
PUBLIC_FIELDS = ("id", "status", "pages_total", "pages_done", "error", "warnings")


class QueueFull(Exception):
    pass


def valid_id(job_id):
    return re.fullmatch(r"[0-9a-f]{32}", job_id) is not None


def blob(job_id, name):
    return f"jobs/{job_id}/{name}"


def submit(store, settings, job_id, filename, pdf, pages_total, owner=None):
    if store.queue_length() >= settings.max_queued_jobs:
        raise QueueFull
    store.put(blob(job_id, "input.pdf"), pdf, "application/pdf")
    status = {
        "id": job_id, "status": "queued",  # queued -> running -> done | failed
        "pages_total": pages_total, "pages_done": 0, "error": None, "warnings": [],
        "filename": filename,
        "owner": owner,  # the signed-in user, once the website has accounts
        "created": time.time(), "finished": None,
    }
    save(store, status)
    store.send(job_id)
    return status


def load(store, job_id):
    data = store.get(blob(job_id, "status.json"))
    return json.loads(data) if data else None


def save(store, status):
    store.put(blob(status["id"], "status.json"), json.dumps(status).encode(), "application/json")


def public(status):
    return {key: status[key] for key in PUBLIC_FIELDS}


def expired(status, settings):
    ttl = settings.result_ttl_s
    return ttl > 0 and status["finished"] is not None and status["finished"] < time.time() - ttl
