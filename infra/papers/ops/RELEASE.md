# Paper OS release runbook

Use this runbook for schema-bearing Paper OS releases on the OVH host. The
examples assume the live service root is `/home/ubuntu/services/paper-os`, the
candidate root is `/home/ubuntu/services/paper-os-candidate-0.6.0`, and the
release tag is `0.6.0`. Never print or copy the live `.env` or secret files.

## 1. Release gates

Do not change production until all gates pass:

1. Review the complete Paper-only diff and confirm no secret, PDF, model output,
   machine-local state, or unrelated project file is included.
2. Run all Paper unit tests in the exact API image and the Reader-v2 focused
   tests in the exact Reader image.
3. Validate the rendered Compose configuration and container hardening.
4. Take a fresh database backup and complete an isolated restore drill.
5. Apply every candidate migration to a separate restore of the current live
   database and verify migration checksums, constraints, paper hashes, and
   aggregate state.
6. Run staging API and security smoke tests without a live DeepSeek call, a
   Drive move, a knowledge outbox lease, or a production artifact activation.
7. Confirm at least 2 GB available RAM and enough disk for the candidate images,
   database backup, and temporary migration database.

## 2. Candidate construction

Mirror only `paper_service/`, `infra/papers/`, and `tests/test_paper*.py` into
the private candidate directory. Do not copy the live `.env`, rclone config,
API key, PDFs, database volume, backup data, or Obsidian state. Remove stale
files inside those exact candidate subtrees before building so an old module
cannot remain in the image.

Render the candidate configuration with the live env file but never display it:

```sh
docker compose \
  --env-file /home/ubuntu/services/paper-os/infra/papers/.env \
  -f /home/ubuntu/services/paper-os-candidate-0.6.0/infra/papers/compose.yml \
  config --quiet
```

Build `personal-os-paper-api:0.6.0` and
`personal-os-paper-reader-v2:0.6.0` sequentially on the small host. Confirm both
image IDs and creation times without printing image environment data.

## 3. Exact-image tests

Run the Paper test suite in a disposable, network-disabled API container with
the candidate tests mounted read-only. Run Reader-v2 artifact, conversion, and
reprocess tests again in the Reader image. Never mount `/data`, rclone, the
DeepSeek key, the live env file, or Docker socket into a unit-test container.

For PostgreSQL integration tests, create a dedicated PostgreSQL 17 container on
a dedicated internal Docker network. Use independent empty databases or reset
the dedicated container between suites. Never point test DSNs at production or
the restored migration-compatibility database. Remove the test container and
network after a pass or failure.

## 4. Current-data migration proof

Restore the newest verified pre-release dump into a uniquely named PostgreSQL
17 container on a private candidate-only network. Connect the candidate API
image using `--env-file` and an overridden database host; run only
`ensure_schema()` against that restored database.

Verify, without printing paper titles or paths:

- the exact ordered migration versions and checksums;
- all expected v1-v8 tables, indexes, foreign keys, and check constraints;
- unchanged paper count and source SHA-256 set;
- zero invalid reading, annotation, knowledge, search, taxonomy, and operations
  rows;
- idempotence of a second `ensure_schema()` call.

Keep this database isolated until the production decision, then remove its
container and private network.

## 5. Production transition

1. Record current container image IDs and service health.
2. Take another verified backup immediately before the transition.
3. Copy the reviewed candidate `paper_service/` and `infra/papers/` subtrees to
   the live root. Preserve the live `.env`, `secrets/`, `backups/`, Docker
   volumes, and paper data volume.
4. Render the live Compose configuration with `config --quiet`.
5. Start PostgreSQL unchanged, then the API so migrations execute under the
   advisory lock.
6. Verify `/readyz` internally before starting workers.
7. Start workers. Keep taxonomy auto-enqueue false and operational webhook
   delivery disabled.
8. Wait through the 90-second heartbeat startup grace and confirm the exact seven
   worker IDs are healthy.
9. Rebuild/reuse search generations locally. Do not trigger live DeepSeek or a
   Drive move as part of deployment.

## 6. Post-release proof

Verify both internal and external boundaries:

1. `/healthz` and `/readyz` work from the `web` network.
2. The public hostname remains behind Cloudflare Access.
3. A direct request to the origin IP with the Papers Host header is rejected.
4. Human read routes work; a human browser cannot use the machine outbox and a
   service identity cannot use human/admin routes.
5. Same-origin mutation protection rejects a missing or wrong Origin and a
   missing `X-Paper-Action: 1` header.
6. Library, reader, search, knowledge review, and admin pages render at mobile
   width.
7. No unexpected model generation, taxonomy generation, Drive move, reprocess,
   rollback, or vault outbox claim is queued.
8. Take a post-release backup and complete a new isolated restore drill.

## 7. Failure response and rollback

For an application-only defect after migration, set writes off, stop the
model/move workers, and fix forward on the current schema. Do not point an older
image at the migrated database: the older migration registry should reject the
unknown versions.

A full rollback is allowed only with the exact pre-release v2 backup:

1. stop the new stack without deleting volumes;
2. preserve the failed database for investigation;
3. run the dry-run and confirmed `materialize-restore` procedure from
   `ops/README.md`; it creates and re-verifies fresh PostgreSQL and data volumes
   without selecting them;
4. create a private Compose override selecting those two external volumes, then
   start the recorded previous images and previous Compose definition;
5. verify internal health, Cloudflare Access, origin rejection, paper SHA set,
   and aggregate counts;
6. do not replay post-release writes until they have been reviewed.

Never use `down --volumes`, `docker system prune`, a broad recursive delete, or
an unverified database dump as a rollback shortcut.

## 8. Separately authorized activation

The release does not authorize any of the following:

- installing or enabling the systemd backup timer;
- configuring an operational webhook or alert credential;
- choosing or enabling encrypted offsite backup and retention;
- creating a Cloudflare service token or changing Access policies;
- installing the Mac Obsidian LaunchAgent or writing the real vault;
- making the first paper-text DeepSeek knowledge or Q&A request;
- sending an eligible taxonomy audit to DeepSeek Pro;
- approving a taxonomy move, reader reprocess, or rollback.

Each item needs its own explicit user action and an end-to-end verification.
