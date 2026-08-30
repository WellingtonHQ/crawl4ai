# nodriver_worker — AGPL-3.0 license notice

This directory contains a worker process built on **nodriver**, which is
licensed under the **GNU AGPL v3.0** (https://www.gnu.org/licenses/agpl-3.0.txt).

## Process isolation

- `nodriver` is installed **only** in the worker's own virtualenv at
  `/opt/nodriver-worker` (see `requirements.txt`).
- It is imported **only** by `worker.py`, which runs as the separate
  supervisord program `nodriver-worker`.
- The `crawl4ai` Python package and the main API (`server.py`, gunicorn
  workers) **never** import nodriver. The only contact point between the
  rest of the image and this worker is an HTTP socket on port 8001
  (`POST /md` / `GET /health`) — AGPL isolation by process + network
  boundary (separate venv, separate process, separate port).

## Consequences for the built image

Because the **image** contains an AGPL-3.0 component, treat the built
Docker image as a product that carries AGPL obligations:

- Keep the image **private / internal**. Do not publish it to public
  registries (Docker Hub, GHCR public, etc.) or redistribute it as a
  standalone artifact.
- If you ever redistribute the image (or run it for third parties over a
  network in a way that constitutes offering), AGPL-3.0 requires you to
  make available the complete corresponding source, including nodriver and
  your `worker.py` modifications.
- Removing this worker (the apt `chromium xvfb xdotool openbox fontconfig`
  + fonts layer, the `/opt/nodriver-worker` venv, the two supervisord
  programs, and this directory) restores an image with no AGPL component.

## Dependencies in this venv

| Package | License | Note |
|---|---|---|
| nodriver 0.50.3 | AGPL-3.0 | the AGPL component |
| fastapi, uvicorn | BSD-3 / BSD-3 | |
| trafilatura | Apache-2.0 | |
| readability-lxml, markdownify | BSD-3 / BSD-3 | |
| opencv-python-headless, numpy | MIT / BSD-3 | opencv-python-headless (not the GUI build) — same `cv2` API `verify_cf` needs, no libGL dependency |
