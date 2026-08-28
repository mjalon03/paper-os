# Paper server foundation

This directory defines the first private server slice for the paper pipeline:

- a FastAPI liveness/readiness service;
- PostgreSQL with a persistent Docker volume;
- no host-published application or database ports;
- attachment to the existing external Docker network named `web`.

The raw paper source of truth remains Google Drive. This stack currently stores
no papers and contains no Google, DeepSeek, Cloudflare, or database credentials.

## Server preparation

From the repository root on the server:

1. Copy `infra/papers/.env.example` to `infra/papers/.env`.
2. Replace the placeholder with a long random database password.
3. Confirm that the external Docker network `web` exists.
4. Validate with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml config`.
5. Start with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml up -d --build`.

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

## Rollback

Stop the stack without deleting data:

```sh
docker compose --env-file infra/papers/.env -f infra/papers/compose.yml down
```

Do not add `--volumes` unless the PostgreSQL data is intentionally being deleted.
