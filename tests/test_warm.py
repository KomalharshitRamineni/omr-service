"""POST /warm: the upload page starts a worker ahead of an upload; the worker waits for pages, then exits."""

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from helpers import make_pdf, upload

from omr_service import jobs
from omr_service.pipeline import Homr
from omr_service.worker import Stay, process_next, run

FAKE_SERVER = [sys.executable, str(Path(__file__).with_name("fake_homr_server.py"))]


def queued(store):
    texts = []
    while (message := store.receive(60)):
        texts.append(message.text)
    return texts


def test_warm_queues_one_message_per_window(client, store):
    first = client.post("/warm")
    assert first.status_code == 202 and first.json() == {"warming": True}
    assert queued(store) == [jobs.WARM_MESSAGE]
    assert client.post("/warm").json() == {"warming": False}  # same window: no second worker
    assert queued(store) == []


def test_no_warm_while_pages_are_queued(client, store):
    assert upload(client, make_pdf(1)).status_code == 202
    assert client.post("/warm").json() == {"warming": False}
    assert not any(jobs.is_warm(text) for text in queued(store))


def test_warm_message_keeps_the_worker_waiting(store, settings):
    store.send(jobs.WARM_MESSAGE)
    stay = Stay()
    assert process_next(store, settings, stay=stay)  # taken, not dropped as "not a page"
    assert stay.active()
    assert store.receive(60) is None  # deleted from the queue
    assert not Stay().active()


def test_page_messages_are_not_warm():
    assert not jobs.is_warm(jobs.page_message("a" * 32, 1))
    assert not jobs.is_warm("a" * 32)
    assert not jobs.is_warm("not json")


def test_warm_worker_reads_a_pdf_uploaded_while_it_waits(client, store, settings):
    settings = replace(settings, warm_wait_s=3)
    assert client.post("/warm").json() == {"warming": True}
    homr = Homr(settings)
    started = time.time()
    worker = threading.Thread(target=run, args=(store, settings, homr))
    worker.start()
    time.sleep(1)  # the queue is empty now; without the warm-up the worker would have exited
    assert worker.is_alive()
    job_id = upload(client, make_pdf(2)).json()["id"]
    worker.join(timeout=30)
    homr.close()
    assert not worker.is_alive()
    assert client.get(f"/omr/{job_id}").json()["status"] == "done"
    assert time.time() - started >= 3  # it stayed for the whole wait


def test_worker_without_warm_up_exits_on_an_empty_queue(store, settings):
    started = time.time()
    run(store, settings, Homr(settings))
    assert time.time() - started < 1


def test_warm_up_starts_homr_processes_before_the_first_page(store, settings, tmp_path):
    settings = replace(settings, homr_server_cmd=FAKE_SERVER, page_workers=2)
    homr = Homr(settings)
    try:
        store.send(jobs.WARM_MESSAGE)
        assert process_next(store, settings, homr, Stay())
        servers = list(homr._idle.queue)
        assert len(servers) == 2 and all(s.proc is not None and s.proc.poll() is None for s in servers)
    finally:
        homr.close()
