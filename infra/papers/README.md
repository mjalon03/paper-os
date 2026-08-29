# Paper server foundation

This directory defines the private server foundation and first Drive ingestion
slice for the paper pipeline:

- a FastAPI liveness/readiness service;
- a PostgreSQL-backed sync queue and paper hash registry;
- a single Worker that copies PDFs from `Paper Inbox` at 07:00 and 19:00 Hong
  Kong time by default;
- SHA-256 duplicate detection and immutable content-addressed raw storage;
- local PDF text extraction, validated DeepSeek metadata classification, and
  Drive organization under `Original/<category>/`;
- coordinate-aware two-column extraction and a private mobile reader with
  source-page images, a table of contents, typography controls, and local
  reading progress;
- a staged reader-v2 worker that prefers eligible PMC JATS and individual media,
  then falls back to resource-bounded Docling extraction and verified figure
  crops without modifying the v1 output;
- PostgreSQL with a persistent Docker volume;
- no host-published application or database ports;
- attachment to the existing external Docker network named `web`.

The raw paper source of truth remains Google Drive. The Worker uses rclone
`copy`, never `sync`, so this ingestion slice does not rename, move, or delete
Drive files during ingestion. Organization only moves a Drive file after the
raw server copy and validated metadata are available. The destination filename
contains a short content hash to prevent unrelated papers with the same title
from overwriting each other.

Per-paper metadata classification uses `deepseek-v4-flash`. A future taxonomy
audit that compares many papers and proposes folder splits or merges is reserved
for `deepseek-v4-pro`; the taxonomy audit itself is not scheduled in this slice.
Publication year remains in each filename and in PostgreSQL rather than creating
a year directory.

`legal-site/` contains the public OAuth application overview, privacy policy,
and terms. It contains no credentials or private paper data. Serve it from the
existing Caddy static volume with `Caddyfile.legal.snippet`; unlike the reader,
these policy pages must remain publicly reachable.

## Server preparation

From the repository root on the server:

1. Copy `infra/papers/.env.example` to `infra/papers/.env`.
2. Replace the placeholder with a long random database password.
3. Place the verified rclone configuration at `secrets/rclone/rclone.conf` with
   directory mode `700` and file mode `600`.
4. Put the DeepSeek API key in `secrets/deepseek/api_key`, owned by the Worker
   user and mode `600`. An empty file safely disables classification while
   leaving papers pending.
5. Confirm that the external Docker network `web` exists.
6. Validate with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml config`.
7. Start with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml up -d --build`.

Reader v2 initially processes only paper IDs `1,15,21`. Keep
`PAPER_READER_V2_ALLOWED_IDS` set to that review cohort until all three pass
manual review. Setting it to `all` enables conversion for the full organized
library. The v2 worker is limited to one CPU and 2.5 GB RAM and stores downloaded
Docling models in the `paper_reader_v2_models` volume.

The API is reachable only by containers attached to `web`; it is not published
on a host port. Add `Caddyfile.snippet` to the existing Caddy configuration only
after Cloudflare DNS and Access are ready.

## Verification

Inspect container health with:

```sh
docker compose --env-file infra/papers/.env -f infra/papers/compose.yml ps
```

From the `web` Docker network, `/healthz` reports process liveness and `/readyz`
reports whether the PostgreSQL TCP endpoint is reachable.

`POST /jobs/sync` requests an immediate run. If another run is queued or active,
it returns that job instead of creating overlapping work. `GET
/jobs/sync/latest` returns the most recent job and aggregate counts without
exposing paper names. These routes remain behind Cloudflare Access.

## Rollback

Stop the stack without deleting data:

```sh
docker compose --env-file infra/papers/.env -f infra/papers/compose.yml down
```

Do not add `--volumes` unless the PostgreSQL data is intentionally being deleted.
