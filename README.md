# OMR service

Turns a PDF of sheet music into one MusicXML file. Each page is rendered with poppler's `pdftoppm`, read by [homr](https://github.com/liebharc/homr) 0.7.0 (run as a separate program, on the CPU), and the pages are joined into one score with music21.

Two programs share one Docker image:

- the **API** (`uvicorn omr_service.app:app`) stores each upload and answers status requests;
- the **worker** (`python -m omr_service.worker`) converts queued jobs, then exits when the queue is empty.

Jobs live in Azure Storage, not in memory: `jobs/<id>/input.pdf`, `status.json` and `result.musicxml` in a blob container, and the job's id on a queue. A job therefore carries on when the browser tab is closed, and survives restarts and scaling to zero. If the worker stops part-way, the job's message reappears on the queue and the job is retried, up to `MAX_ATTEMPTS` times. Finished jobs are kept unless `RESULT_TTL_SECONDS` is set. Each job records an `owner`, empty until the website has accounts.

## API

| Request | Response |
|---|---|
| `POST /omr`, multipart form, field `file` = the PDF | `202` + job status (below), `Location: /omr/{id}` |
| `GET /omr/{id}` | `200` job status, always JSON |
| `GET /omr/{id}/musicxml` | `200` the MusicXML (`application/vnd.recordare.musicxml+xml`), once `status` is `done` |
| `GET /health` | `200 {"status": "ok"}` |

Job status:

```json
{"id": "b5f84d…", "status": "running", "pages_total": 2, "pages_done": 1, "error": null, "warnings": []}
```

`status` goes `queued` → `running` → `done` or `failed`. Pages homr can't read are skipped and listed in `warnings`. A job fails only if no page can be read.

Errors are JSON `{"error": "…"}`:

| Code | When |
|---|---|
| 404 | unknown job, or its result has expired (`RESULT_TTL_SECONDS`) |
| 409 | MusicXML asked for before the job is `done` |
| 413 | file over `MAX_UPLOAD_MB`, or more than `MAX_PAGES` pages |
| 415 | not a PDF |
| 422 | a PDF that can't be read |
| 503 | `MAX_QUEUED_JOBS` jobs already queued or running; retry after `Retry-After` seconds |

Browsers may call it only from the origins in `ALLOWED_ORIGINS`. Expect about 15 s per page on the CPU (measured with homr 0.7.0; not a limit), so poll every few seconds.

Example from the browser:

```js
const form = new FormData();
form.append("file", pdfFile);
const job = await (await fetch(`${OMR_URL}/omr`, { method: "POST", body: form })).json();
let status;
do {
  await new Promise(r => setTimeout(r, 3000));
  status = await (await fetch(`${OMR_URL}/omr/${job.id}`)).json();
} while (status.status === "queued" || status.status === "running");
if (status.status === "done") {
  const musicxml = await (await fetch(`${OMR_URL}/omr/${job.id}/musicxml`)).text();
}
```

## Configuration (environment variables)

Every limit is a setting. The defaults are deliberately generous while we test; tighten them later through the environment.

| Variable | Default |
|---|---|
| `ALLOWED_ORIGINS` | `https://kind-island-00d8a3f03.2.azurestaticapps.net` (comma-separated) |
| `HOMR_CMD` | `homr` |
| `MAX_UPLOAD_MB` / `MAX_PAGES` / `MAX_QUEUED_JOBS` | `100` / `200` / `100` |
| `RESULT_TTL_SECONDS` | `0` (keep finished jobs) |
| `PAGE_TIMEOUT_SECONDS` / `RETRY_AFTER_SECONDS` / `RENDER_DPI` | `900` / `60` / `300` |
| `MAX_ATTEMPTS` | `3` (deliveries of a job before the worker gives up) |
| `STORAGE_ACCOUNT` | empty: jobs go in a local folder, `STORE_DIR` (default: in the system temp folder) |
| `BLOB_CONTAINER` / `QUEUE_NAME` | `jobs` / `omr-jobs` |
| `AZURE_CLIENT_ID` | in Azure, the managed identity's client ID; locally, sign-in comes from `az login` |
| `WORK_DIR` | the system temp folder (temporary files while converting) |

## Run locally

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and poppler (`pdfinfo`, `pdftoppm`).

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest httpx
.venv/bin/homr --init                     # download homr's models once
.venv/bin/python -m pytest -q             # tests use a fake homr
ALLOWED_ORIGINS=http://localhost:3000 .venv/bin/uvicorn omr_service.app:app --port 8765   # API
.venv/bin/python -m omr_service.worker --watch                                            # worker, second terminal
```

Both use a local folder for jobs. To use the Azure storage instead, set `STORAGE_ACCOUNT` for both; your `az login` account needs the Storage Blob Data Contributor and Storage Queue Data Contributor roles on it.

Dependencies are pinned in `requirements.txt`; change `requirements.in` and run `uv pip compile requirements.in -o requirements.txt --python-version 3.12`.

## Azure

`deploy/azure.sh` creates everything, step by step: resource group, container registry and image, storage account (blob container and queue; Entra ID sign-in only, no shared keys), a managed identity with its permissions, a Container Apps environment, the API as a container app that scales to zero, and the worker as a Container Apps job started by queue messages.

## Credits and licence

Copyright (C) 2026 University of Southampton.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License, version 3, as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE). If you run a modified version as a network service, the licence requires you to offer its source to the people who use it.

Built on homr (AGPL-3.0), music21 (BSD-3-Clause), FastAPI (MIT), the Azure SDK for Python (MIT) and poppler (GPL, run as a separate program).
