"""Private API entrypoint for the paper pipeline."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from .database import ensure_schema, latest_sync, queue_sync
from .status import readiness_status, service_status


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_schema()
    yield


app = FastAPI(
    title="Personal OS Paper API",
    version="0.3.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
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
