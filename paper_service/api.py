"""Minimal API entrypoint for the paper pipeline."""

from fastapi import FastAPI, Response

from .status import readiness_status, service_status

app = FastAPI(
    title="Personal OS Paper API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return service_status()


@app.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    payload, status_code = readiness_status()
    response.status_code = status_code
    return payload
