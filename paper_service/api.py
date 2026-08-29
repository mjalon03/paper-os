"""Private API and mobile reader entrypoint for the paper pipeline."""

import os
import re
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .database import (
    ensure_schema,
    get_reader_paper,
    latest_sync,
    list_reader_papers,
    queue_sync,
)
from .reader import ReaderFailure, read_manifest, render_library, render_reader
from .reader_v2 import read_article, render_reader_v2, v2_asset_path
from .status import readiness_status, service_status


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_schema()
    yield


app = FastAPI(
    title="Personal OS Paper API",
    version="0.5.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def private_response_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "private, no-store"
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
        )
    return response


def _data_dir() -> Path:
    return Path(os.environ.get("PAPER_DATA_DIR", "/data"))


def _reader_asset(paper_id: int, asset_name: str) -> Path:
    if not re.fullmatch(r"page-\d{3}\.jpg", asset_name):
        raise HTTPException(status_code=404)
    paper = get_reader_paper(paper_id)
    if not paper or paper.get("reader_status") != "ready":
        raise HTTPException(status_code=404)
    manifest_path = PurePosixPath(str(paper["reader_manifest_path"]))
    root = (_data_dir() / "library" / "mobile").resolve()
    path = (root / manifest_path.parent / "assets" / asset_name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return path


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/library", status_code=307)


@app.get("/library", response_class=HTMLResponse)
def library() -> HTMLResponse:
    return HTMLResponse(render_library(list_reader_papers()))


@app.get("/read/{paper_id}", response_class=HTMLResponse)
def read_paper(paper_id: int) -> HTMLResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    if paper.get("reader_status") != "ready" or not paper.get(
        "reader_manifest_path"
    ):
        return HTMLResponse(render_library([paper]), status_code=202)
    try:
        manifest = read_manifest(_data_dir(), str(paper["reader_manifest_path"]))
    except ReaderFailure:
        raise HTTPException(status_code=503) from None
    return HTMLResponse(render_reader(paper, manifest))


@app.get("/read/{paper_id}/v2", response_class=HTMLResponse)
def read_paper_v2(paper_id: int) -> HTMLResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    try:
        article = read_article(_data_dir(), paper_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    except (ValueError, ReaderFailure):
        raise HTTPException(status_code=503) from None
    return HTMLResponse(render_reader_v2(paper, article))


@app.get("/read/{paper_id}/v2/assets/{asset_name}")
def reader_v2_asset(paper_id: int, asset_name: str) -> FileResponse:
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    try:
        path = v2_asset_path(_data_dir(), paper_id, asset_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/read/{paper_id}/assets/{asset_name}")
def reader_asset(paper_id: int, asset_name: str) -> FileResponse:
    return FileResponse(_reader_asset(paper_id, asset_name), media_type="image/jpeg")


@app.get("/read/{paper_id}/source.pdf")
def reader_source(paper_id: int) -> FileResponse:
    paper = get_reader_paper(paper_id)
    if not paper or paper.get("reader_status") != "ready":
        raise HTTPException(status_code=404)
    relative = PurePosixPath(str(paper["stored_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=404)
    root = (_data_dir() / "library" / "raw").resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="paper.pdf",
        content_disposition_type="inline",
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return service_status()


@app.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    payload, status_code = readiness_status()
    response.status_code = status_code
    return payload


@app.post("/jobs/sync", status_code=status.HTTP_202_ACCEPTED)
def request_sync() -> dict[str, object]:
    return {"job": queue_sync("manual")}


@app.get("/jobs/sync/latest")
def get_latest_sync() -> dict[str, object]:
    return {"job": latest_sync()}
