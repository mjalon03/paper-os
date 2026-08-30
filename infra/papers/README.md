# Private Paper OS

This directory deploys the private paper pipeline behind
`papers.minjae-jo.com`. It is designed for one user, a small OVH host, Google
Drive as the PDF source of truth, PostgreSQL as the workflow-state source of
truth, and immutable server-side reader artifacts.

The normal flow is:

1. The user puts PDFs in Google Drive `Paper Inbox`.
2. `paper-worker` checks at 07:00 and 19:00 Hong Kong time. The reader also has
   an explicit “Google Drive 지금 확인” action for an immediate queued run.
3. The worker copies, hashes, de-duplicates, and stores an immutable raw copy.
   A credential-free `paper-parser` service extracts bounded metadata from an
   exact-SHA private copy; only the validated text result returns to the worker
   for DeepSeek Flash classification and Drive organization. The worker never
   parses a PDF and never uses `rclone sync`.
4. A separate `paper-reader-v1-worker` preserves the legacy fallback queue but
   sends conversion to the same isolated parser boundary. It validates a
   bounded artifact archive and installs it atomically.
5. `paper-reader-v2-worker` coordinates a versioned mobile article without
   egress or model access. It first asks a DOI-only PMC broker for eligible JATS
   and media, then falls back to a credential-free Docling converter that sees
   only one exact-SHA PDF job. The coordinator re-hashes every returned file,
   and a checksum-bound current pointer changes only after a complete version
   is ready.
6. Reading state, extraction issues, highlights, notes, and questions use stable
   article anchors and optimistic revisions.
7. Search uses immutable local index generations. Optional DeepSeek Flash Q&A
   is restricted to the exact cited evidence snapshot; personal annotations are
   excluded unless the user opts in for that request.
8. Optional DeepSeek Pro knowledge drafts require an explicit user action and a
   separate human review. Only an approved revision enters the immutable
   create-only Obsidian outbox.

## Approval boundaries

Expensive or destructive operations are intentionally split into stages:

- A local taxonomy audit runs every 12 hours. It considers category size,
  growth, age, crowding, and topic heterogeneity without sending paper text.
- Scheduled audits cannot create provider jobs. An eligible audit is sent to
  DeepSeek Pro only through the authenticated human admin action, with a cost
  and data-disclosure acknowledgement stored immutably on the job.
- DeepSeek receives only the category, opaque paper ID, bounded title, and
  bounded title-derived topic terms for taxonomy—not PDF text, abstracts,
  annotations, credentials, filenames, paths, or content hashes.
- A generated folder proposal cannot move a Drive file. A human must approve
  the exact revision first. The mover binds and rechecks the Google Drive file
  ID, path, and SHA-256, rejects ambiguous names and destination collisions,
  moves by ID, verifies the destination ID/hash, and records a journal.
- Reader reprocessing and rollback use separate review requests. The worker
  activates a staged artifact only with an exact compare-and-swap against the
  reviewed source/version.
- Knowledge and Q&A model calls are also explicit first-use actions. AI drafts
  never become reviewed knowledge automatically.

Publication year stays in metadata and filenames rather than becoming a folder.
Subthemes are proposed only when a category has enough evidence to benefit from
another level.

## Services

- `postgres`: durable state and migration history.
- `paper-api`: private mobile reader, search, knowledge review, and admin UI.
- `paper-parser`: internal-only, credential-free PDF parsing with read-only raw
  data, exact-SHA private copies, scrubbed child environments, resource/time
  limits, and bounded outputs. It has no database, Drive, model secret, or
  egress network.
- `paper-worker`: Drive ingestion, isolated-parser metadata classification, and
  twice-daily scheduling.
- `paper-reader-v1-worker`: legacy reader queue orchestration and validated
  atomic artifact installation, without Drive/model secrets or egress.
- `paper-reader-v2-worker`: database-owning, no-egress coordinator for normal
  conversion plus approved reprocess and rollback jobs.
- `paper-reader-v2-converter`: internal-only Docling service with one projected
  PDF job, read-only models, scrubbed child environment, and no database, paper
  library, Drive/model secret, or egress network.
- `paper-pmc-broker`: DOI-only, egress-enabled PMC fetcher with no database,
  paper library, converter jobs, model cache, or secret mount. Its bounded ZIP
  is treated as untrusted by the coordinator.
- `paper-knowledge-worker`: reviewed-request DeepSeek Pro drafts.
- `paper-qa-worker`: evidence-bound DeepSeek Flash answers.
- `paper-search-index-worker`: periodic immutable local index maintenance.
- `paper-taxonomy-worker`: local audits, explicit Pro jobs, and approved Drive
  moves.
- `paper-ops-monitor`: worker heartbeat evaluation and private incident state.

Every application container is read-only apart from its explicit volumes and
temporary filesystem. Capabilities are dropped, privilege escalation is
disabled, logs rotate, PostgreSQL and the API have no host-published ports, and
only the required workers receive an egress network. Untrusted metadata and v1
PDF parsing runs only in `paper-parser`; its child receives an allowlisted
environment, CPU/address-space/file-descriptor limits, a process-group timeout,
and no secret mount. Reader archives are capped, structurally validated, and
installed atomically. A shared conversion lock prevents the parser and Docling
converter from consuming peak memory together on the 3.7 GB OVH host. Reader
v2 permits one converter request at a time inside a 2.5 GB memory-and-swap
ceiling; both heavy services hold the same cross-container lock for their
complete child lifetime. Do not bypass that lock or scale a replacement
converter that does not use it.

Reader v2 artifacts are bounded to 512 files, 64 MiB per file, and 256 MiB per
immutable version. Hashing and staging copy in 1 MiB chunks, so the artifact is
never duplicated in Python memory. Coordinator output and `TMPDIR` use the
disk-backed `paper_reader_v2_scratch` volume rather than the 128 MiB `/tmp`
tmpfs. Exact PDF projections and converter results use the separate
`paper_reader_v2_jobs` volume; PMC uses its own `paper_pmc_scratch` volume and
transfers only a bounded archive over its internal control network. Each
coordinator build is marked, cleaned on normal exit, and stale marked builds are
cleaned after restart; the logical scratch ceiling is 1 GiB with a 512 MiB
free-space reserve. Docker named volumes have no portable hard quota, so
production must also monitor the Docker data filesystem or place it on a
quota-backed filesystem. Only precisely named, validated job or Reader
directories are automatically removed.

The coordinator never invokes Docling or performs PMC HTTPS requests in its
production path. The converter cannot reach the database, full paper library,
PMC broker, or internet. The PMC broker accepts a canonical DOI and bounded
metadata rather than a caller URL, pins HTTPS to the exact supported public
hosts, and cannot read any PDF. The coordinator independently validates request
binding, ZIP paths and types, file counts and sizes, SHA-256 manifests, Article
schema, source kind, and the unchanged raw PDF before publishing.

`legal-site/` contains only the public OAuth overview, privacy policy, and
terms. Serve it with `Caddyfile.legal.snippet`; those pages remain public while
the paper application remains behind Cloudflare Access.

## Server preparation

From the repository root on the server:

1. Copy `infra/papers/.env.example` to `infra/papers/.env`.
2. Replace the placeholder with a long random database password.
3. Place the verified rclone configuration at `secrets/rclone/rclone.conf` with
   directory mode `700` and file mode `600`.
4. Put the DeepSeek API key in `secrets/deepseek/api_key`, owned by the service
   operator and mode `600`. An empty file disables model-backed jobs safely.
5. Confirm that the external Docker network `web` exists.
6. Validate with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml config`.
7. Verify that heartbeat worker IDs exactly match the sorted IDs in
   `PAPER_OPS_RUNTIME_SERVICES_JSON`; scheduled taxonomy audits remain local-only.
8. Confirm the Reader coordinator has no `paper_egress` network, the converter
   has only `paper_reader_v2_internal`, and the PMC broker is the only Reader v2
   service with `paper_egress`.
9. Start with `docker compose --env-file infra/papers/.env -f infra/papers/compose.yml up -d --build`.

Set `PAPER_READER_V2_ALLOWED_IDS` to a comma-separated review cohort for a staged
release or to `all` after that cohort passes manual review. Downloaded Docling
models remain in `paper_reader_v2_models`. Taxonomy move verification uses a
separate private temporary volume so large PDFs do not consume the 128 MB
container tmpfs. Reader v2 scratch defaults are intentionally expressed in
bytes in `.env`; lowering them is safe only if the 256 MiB artifact bound still
fits, and raising them does not create a real host-disk quota.

The API is reachable only by containers attached to `web`; it is not published
on a host port. `Caddyfile.snippet` also rejects requests that bypass Cloudflare.
Cloudflare Access is still the human identity boundary, and browser mutations
add same-origin plus `X-Paper-Action: 1` checks. `/healthz` and `/readyz` are the
only public application paths. The Obsidian bridge uses its own exact service
identity and cannot call human endpoints.

Do not enable an operational webhook by adding only a URL. It also requires an
exact public host allowlist and a private mode-`600` token file. The runtime
rejects HTTP, redirects, IP literals, local/private targets, userinfo, and
unbounded responses. `paper-ops-monitor` intentionally has no egress network in
the default deployment; a separately reviewed webhook activation must add the
token mount and `paper_egress` together.

## Verification

Inspect container health with:

```sh
docker compose --env-file infra/papers/.env -f infra/papers/compose.yml ps
```

From the `web` Docker network, `/healthz` reports process liveness and `/readyz`
reports whether PostgreSQL is reachable. Then verify all of the following:

1. `schema_migrations` contains the exact known ordered versions.
2. All configured worker IDs appear healthy after the 90-second startup grace.
3. The public hostname redirects an unauthenticated browser to Cloudflare
   Access, while a direct origin request with the same Host header is rejected.
4. A no-change Drive run creates no duplicate paper and no duplicate job.
5. The library, reader, search, knowledge page, and admin page render on a phone.
6. A test reprocess creates a new version without changing the raw PDF SHA; a
   separately approved rollback restores the reviewed version.
7. A fresh v2 backup captures PostgreSQL plus raw/Reader durable data, then
   restores both into isolated temporary resources. See `ops/README.md`.

`POST /jobs/sync` requests an immediate run. If another run is queued or active,
it returns that job instead of creating overlapping work. `GET
/jobs/sync/latest` returns the most recent job and aggregate counts without
exposing paper names. These routes remain behind Cloudflare Access.

## Release and rollback

Before every schema-bearing release, take a verified custom-format PostgreSQL
backup and run the candidate migrations against an isolated restore. Build and
test the exact candidate images before changing production. After deployment,
repeat the backup and restore drill.

Do not roll a migrated database back to an older image: the migration registry
correctly rejects schema versions unknown to that image. For an application
problem, first disable writes and model/move workers and fix forward with the
current schema. A full rollback means stopping the new stack and restoring the
pre-release v2 database-plus-data backup into fresh volumes together with the
previous images; it is a data rollback, not just an image-tag change.

## Rollback

Stop the stack without deleting data:

```sh
docker compose --env-file infra/papers/.env -f infra/papers/compose.yml down
```

Do not add `--volumes` unless the PostgreSQL database, paper artifacts, model
cache, and taxonomy temporary volume are intentionally being deleted. Do not
install or enable the backup timer, configure offsite retention, add a webhook,
or edit the real Obsidian vault without the user's separate authorization.
