# OMR service

Turns a PDF of sheet music into one MusicXML file. Each page is rendered with poppler's `pdftoppm`, read by [homr](https://github.com/liebharc/homr) 0.7.0 (run as a separate program, on the CPU), and the pages are joined into one score with music21.

Jobs are processed one at a time and kept in memory for an hour after they finish.

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
| 404 | unknown job, or finished more than an hour ago |
| 409 | MusicXML asked for before the job is `done` |
| 413 | file over 20 MB, or more than 20 pages |
| 415 | not a PDF |
| 422 | a PDF that can't be read |
| 503 | queue full (10 jobs); retry after `Retry-After` seconds |

Browsers may call it only from the origins in `ALLOWED_ORIGINS`. A page takes about 15 s.

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

| Variable | Default |
|---|---|
| `ALLOWED_ORIGINS` | `https://kind-island-00d8a3f03.2.azurestaticapps.net` (comma-separated) |
| `HOMR_CMD` | `homr` |
| `MAX_UPLOAD_MB` / `MAX_PAGES` / `MAX_QUEUED_JOBS` | `20` / `20` / `10` |
| `RESULT_TTL_SECONDS` / `PAGE_TIMEOUT_SECONDS` / `RENDER_DPI` | `3600` / `300` / `300` |
| `WORK_DIR` | the system temp folder |

## Run locally

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and poppler (`pdfinfo`, `pdftoppm`).

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest httpx
.venv/bin/homr --init                     # download homr's models once
.venv/bin/python -m pytest -q             # tests use a fake homr
ALLOWED_ORIGINS=http://localhost:3000 .venv/bin/uvicorn omr_service.app:app --port 8765
```

Dependencies are pinned in `requirements.txt`; change `requirements.in` and run `uv pip compile requirements.in -o requirements.txt --python-version 3.12`.

## Credits and licence

Copyright (C) 2026 University of Southampton.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License, version 3, as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE). If you run a modified version as a network service, the licence requires you to offer its source to the people who use it.

Built on homr (AGPL-3.0), music21 (BSD-3-Clause), FastAPI (MIT) and poppler (GPL, run as a separate program).
