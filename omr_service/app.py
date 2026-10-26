"""HTTP API: POST /omr (a PDF) -> job; GET /omr/{id} -> status; GET /omr/{id}/musicxml -> result.
The API only stores jobs and reads them back; worker.py converts them."""

import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import jobs
from .config import Settings
from .pipeline import ConversionError, page_count
from .storage import make_store


def error(status, message, headers=None):
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def safe_stem(filename):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "").stem)[:80] or "score"


def create_app(settings=None, store=None):
    settings = settings or Settings()
    store = store or make_store(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="OMR service", summary="PDF sheet music to MusicXML with homr", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST"],
                       allow_headers=["*"], expose_headers=["Location", "Content-Disposition"])

    def find(job_id):
        status = jobs.load(store, job_id) if jobs.valid_id(job_id) else None
        return None if status is None or jobs.expired(status, settings) else status

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/omr", status_code=202)
    def submit(file: UploadFile = File(...)):
        limit = settings.max_upload_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            return error(413, f"the file is larger than {settings.max_upload_mb} MB")
        if not data.startswith(b"%PDF-"):
            return error(415, "upload a PDF file")
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=settings.work_dir) as tmp:
                tmp.write(data)
                tmp.flush()
                pages = page_count(tmp.name)
            if pages < 1:
                raise ConversionError("the PDF has no pages")
            if pages > settings.max_pages:
                return error(413, f"the PDF has {pages} pages; the limit is {settings.max_pages}")
            status = jobs.submit(store, settings, uuid.uuid4().hex, safe_stem(file.filename), data, pages)
        except ConversionError as ex:
            return error(422, str(ex))
        except jobs.QueueFull:
            return error(503, "the service is busy; try again in a few minutes",
                         {"Retry-After": str(settings.retry_after_s)})
        return JSONResponse(jobs.public(status), status_code=202, headers={"Location": f"/omr/{status['id']}"})

    @app.get("/omr/{job_id}")
    def status(job_id: str):
        status = find(job_id)
        if not status:
            return error(404, "unknown or expired job")
        return jobs.public(status)

    @app.get("/omr/{job_id}/musicxml")
    def result(job_id: str):
        status = find(job_id)
        if not status:
            return error(404, "unknown or expired job")
        if status["status"] != "done":
            return error(409, f"the job is {status['status']}")
        return Response(store.get(jobs.blob(job_id, "result.musicxml")), media_type=jobs.MUSICXML_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{status["filename"]}.musicxml"'})

    return app


app = create_app()
