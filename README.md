# OMR service

Turns a PDF of sheet music into one MusicXML file. Each page is rendered with poppler's `pdftoppm`, read by [homr](https://github.com/liebharc/homr) 0.7.1 (on the CPU, in separate processes that stay loaded between pages), and the pages are joined into one score with music21.

Two programs share one Docker image:

- the **API** (`uvicorn omr_service.app:app`) stores each upload and answers status requests;
- the **worker** (`python -m omr_service.worker`) converts queued pages, then exits when the queue is empty. Several workers can run at once.

Jobs live in Azure Storage, not in memory: `jobs/<id>/input.pdf`, `status.json` and `result.musicxml` in a blob container, and one message per page on a queue, so the pages of one PDF are read at the same time by as many workers as are running. Each finished page is stored (`jobs/<id>/pages/`), and the worker that finishes the last page joins them. A job therefore carries on when the browser tab is closed, and survives restarts and scaling to zero. If a worker stops part-way, the page's message reappears on the queue and the page is retried, up to `MAX_ATTEMPTS` times, then skipped. Finished jobs are kept unless `RESULT_TTL_SECONDS` is set. Each job records an `owner`, empty until the website has accounts.

## API

| Request | Response |
|---|---|
| `POST /omr`, multipart form, field `file` = the PDF | `202` + job status (below), `Location: /omr/{id}` |
| `GET /omr/{id}` | `200` job status, always JSON |
| `GET /omr/{id}/musicxml` | `200` the MusicXML (`application/vnd.recordare.musicxml+xml`), once `status` is `done` |
| `POST /warm` | `202 {"warming": true}` when it starts a worker ahead of an upload; `false` when one is already starting or pages are being read |
| `GET /health` | `200 {"status": "ok"}` |

Job status:

```json
{"id": "b5f84d…", "status": "running", "pages_total": 2, "pages_done": 1, "error": null, "warnings": [], "check_bars": []}
```

Call `POST /warm` when the upload page opens. A worker takes about 25 s to start (pulling the image and starting the container), so it starts while the user chooses the PDF; it then waits `WARM_WAIT_SECONDS` for pages before stopping. At most one is started per `WARM_EVERY_SECONDS`, however many pages are opened. Without it, an upload waits for a worker to start.

`status` goes `queued` → `running` → `done` or `failed`. Pages homr can't read are skipped and listed in `warnings`. A job fails only if no page can be read. Once `done`, `check_bars` lists the bars (numbered from 1, as in the MusicXML) that hold more beats than their time signature: almost certainly misread, so the first places to correct. It does not list every error.

Errors are JSON `{"error": "…"}`:

| Code | When |
|---|---|
| 404 | unknown job, or its result has expired (`RESULT_TTL_SECONDS`) |
| 409 | MusicXML asked for before the job is `done` |
| 413 | file over `MAX_UPLOAD_MB`, or more than `MAX_PAGES` pages |
| 415 | not a PDF |
| 422 | a PDF that can't be read |
| 503 | `MAX_QUEUED_PAGES` pages already queued or being read; retry after `Retry-After` seconds |

Browsers may call it only from the origins in `ALLOWED_ORIGINS`. Expect about 15–19 s per page on 2 CPUs (homr 0.7.1, measured on a laptop under a CPU limit; not a limit), less with `PAGE_WORKERS` and several workers, so poll every few seconds.

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
| `HOMR_CMD` | not set: homr stays loaded between pages (`omr_service.homr_server`). Set, homr is started for every page with this command; `HOMR_SERVER=0` does the same with `omr_service.homr_runner` |
| `USE_GPU` | `0` (`1` lets homr use the worker's GPU) |
| `PAGE_WORKERS` | `1` (pages one worker converts at the same time; the Azure workers use `2` on 4 vCPU) |
| `HOMR_THREADS` | ONNX Runtime threads per homr process; default: the container's CPUs divided by `PAGE_WORKERS` |
| `MAX_UPLOAD_MB` / `MAX_PAGES` / `MAX_QUEUED_PAGES` | `100` / `200` / `400` |
| `RESULT_TTL_SECONDS` | `0` (keep finished jobs) |
| `PAGE_TIMEOUT_SECONDS` / `RETRY_AFTER_SECONDS` / `RENDER_DPI` | `900` / `60` / `300` |
| `MAX_ATTEMPTS` | `3` (deliveries of a page before it is skipped) |
| `STORAGE_ACCOUNT` | empty: jobs go in a local folder, `STORE_DIR` (default: in the system temp folder) |
| `BLOB_CONTAINER` / `QUEUE_NAME` | `jobs` / `omr-pages` |
| `AZURE_CLIENT_ID` | in Azure, the managed identity's client ID; locally, sign-in comes from `az login` |
| `WORK_DIR` | the system temp folder (temporary files while converting) |
| `WARM_WAIT_SECONDS` / `WARM_EVERY_SECONDS` | `60` / `60` (how long a worker started by `POST /warm` waits for pages; at most one such worker per this many seconds) |

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

`deploy/azure.sh` creates everything, step by step: resource group, container registry and image, storage account (blob container and queue; Entra ID sign-in only, no shared keys), a managed identity with its permissions, a Container Apps environment, the API as a container app that scales to zero, and the worker as a Container Apps job started by queue messages: one run for every `PAGE_WORKERS` pages waiting, up to `MAX_WORKERS` runs at once. Step 9 adds a second API for trying a new image beside the live one, step 10 switches the live API to it, and step 11 rolls back to the previous worker.

## Credits and licence

Copyright (C) 2026 University of Southampton.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License, version 3, as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE). If you run a modified version as a network service, the licence requires you to offer its source to the people who use it.

Built on homr (AGPL-3.0), music21 (BSD-3-Clause), FastAPI (MIT), the Azure SDK for Python (MIT) and poppler (GPL, run as a separate program).
