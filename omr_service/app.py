"""HTTP API: POST /omr (a PDF) -> job; GET /omr/{id} -> status; GET /omr/{id}/musicxml -> result."""

import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from .config import Settings
from .jobs import JobStore, QueueFull
from .pipeline import ConversionError, page_count

MUSICXML_TYPE = "application/vnd.recordare.musicxml+xml"


def error(status, message, headers=None):
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def safe_stem(filename):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "").stem)[:80] or "score"


def create_app(settings=None):
    settings = settings or Settings()
    store = JobStore(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        store.start()
        yield
        store.stop()

    app = FastAPI(title="OMR service", summary="PDF sheet music to MusicXML with homr", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST"],
                       allow_headers=["*"], expose_headers=["Location", "Content-Disposition"])

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

        job_id = uuid.uuid4().hex
        job_dir = store.job_dir(job_id)
        job_dir.mkdir(parents=True)
        pdf = job_dir / "input.pdf"
        pdf.write_bytes(data)
        try:
            pages = page_count(pdf)
            if pages < 1:
                raise ConversionError("the PDF has no pages")
            if pages > settings.max_pages:
                shutil.rmtree(job_dir, ignore_errors=True)
                return error(413, f"the PDF has {pages} pages; the limit is {settings.max_pages}")
            job = store.submit(job_id, safe_stem(file.filename), pages)
        except ConversionError as ex:
            shutil.rmtree(job_dir, ignore_errors=True)
            return error(422, str(ex))
        except QueueFull:
            shutil.rmtree(job_dir, ignore_errors=True)
            return error(503, "the service is busy; try again in a few minutes", {"Retry-After": "60"})
        return JSONResponse(job.public(), status_code=202, headers={"Location": f"/omr/{job.id}"})

    @app.get("/omr/{job_id}")
    def status(job_id: str):
        job = store.get(job_id)
        if not job:
            return error(404, "unknown or expired job")
        return job.public()

    @app.get("/omr/{job_id}/musicxml")
    def result(job_id: str):
        job = store.get(job_id)
        if not job:
            return error(404, "unknown or expired job")
        if job.status != "done":
            return error(409, f"the job is {job.status}")
        return Response(job.result, media_type=MUSICXML_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{job.filename}.musicxml"'})

    return app


app = create_app()
