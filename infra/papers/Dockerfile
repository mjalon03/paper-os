FROM rclone/rclone:1.75.0 AS rclone

FROM python:3.13.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system paper && useradd --system --gid paper --home-dir /app paper

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

COPY infra/papers/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt

COPY paper_service /app/paper_service

USER paper

EXPOSE 8000

CMD ["uvicorn", "paper_service.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
