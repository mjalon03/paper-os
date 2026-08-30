# Paper OS backup and restore runbook

This directory contains a local-backup command, an isolated restore drill, and
systemd templates. Nothing here installs a timer or configures an external
service. A real backup briefly stops the application services, captures one
quiesced PostgreSQL plus durable-data point, and starts exactly the services
that were running before it. The dump, raw PDFs, Reader artifacts, and metadata
are private; never commit or place the backup directory in a publicly
synchronized folder.

Use `RELEASE.md` alongside this document for the candidate build, migration,
production transition, verification, and rollback sequence.

## Safety model

- Every run requires absolute paths for the service root, Compose file,
  environment file, and backup root. The Compose and environment files must be
  inside the service root, and the environment file must be mode `600` or
  stricter.
- The backup inventories every public table, every row count, source-hash
  integrity, and a digest of the sorted paper SHA set. It stops all application
  writers and conversion boundaries, uses PostgreSQL custom format, verifies
  the dump with `pg_restore --list`, and archives only `library/raw` plus
  `library/mobile` through a networkless tool container.
- The durable-data manifest contains exact paths, sizes, and SHA-256 values.
  Incoming files, job/scratch directories, models, locks, credentials, rclone
  state, and secrets are excluded. The `.env` and secret directories are never
  mounted into the archive tool.
- The first live run initializes an empty backup root with a private marker.
  Retention operates only with that marker and only deletes verified,
  checksum-valid, immediate child directories with the exact expected files
  and strict generated names. An unfamiliar, corrupt, linked, or partial path
  is left for human review.
- The restore drill verifies checksums before Docker, starts a uniquely named
  PostgreSQL `17.11` container with `--network none` and tmpfs storage, restores
  the dump, and compares the exact table inventory, every row count, paper
  SHA-set digest, hash integrity, and validated constraints. It also restores
  the data tar into a new empty Docker volume and re-hashes every file. Both
  temporary resources are always removed. It never prints paper titles, paths,
  contents, or database secrets.

## Server prerequisites

Run these checks manually before activation:

1. Confirm Python 3, Docker with the Compose plugin, and the pinned
   `postgres:17.11-alpine3.24` and `python:3.13.7-slim-bookworm` images are
   already available. The backup path must not depend on a network pull.
2. Confirm the repository is at `/home/ubuntu/services/paper-os`, the live
   environment file is `infra/papers/.env`, and that file is owned by the
   service operator with mode `600`.
3. Create the dedicated empty directory
   `/home/ubuntu/services/paper-os/backups/automated` with owner `ubuntu`, group
   `ubuntu`, and mode `700`. Do not reuse the sibling directory holding the
   pre-migration manual baseline.
4. Ensure free space can hold at least two complete database-plus-data backups,
   one PostgreSQL restore, and one expanded data restore. Google Drive remains
   the upload source, but rollback no longer assumes Reader artifacts can be
   regenerated into an old database state.
5. Accept that an enabled backup produces a short private-reader outage while
   application services are quiesced. Failure to restart every previously
   running service makes the command fail and requires operator review.

## Dry run and first local backup

The dry run validates paths and options but creates nothing and calls neither
Docker nor rclone:

```sh
python3 infra/papers/ops/paper_backup.py \
  --service-root /home/ubuntu/services/paper-os \
  --compose-file /home/ubuntu/services/paper-os/infra/papers/compose.yml \
  --env-file /home/ubuntu/services/paper-os/infra/papers/.env \
  --backup-root /home/ubuntu/services/paper-os/backups/automated \
  backup --retention-days 14 --dry-run
```

Remove only `--dry-run` for the first local backup. A successful directory
contains exactly `paper_os.dump`, `paper_data.tar`, `paper_data.json`,
`backup.json`, and `SHA256SUMS`. Preserve the reported backup identifier; no
paper title appears in it.

## Required restore proof

Use the exact completed backup directory, first with `--dry-run`, then without:

```sh
python3 infra/papers/ops/paper_backup.py \
  --service-root /home/ubuntu/services/paper-os \
  --compose-file /home/ubuntu/services/paper-os/infra/papers/compose.yml \
  --env-file /home/ubuntu/services/paper-os/infra/papers/.env \
  --backup-root /home/ubuntu/services/paper-os/backups/automated \
  restore-drill \
  --backup-dir /home/ubuntu/services/paper-os/backups/automated/REPLACE_WITH_BACKUP_ID \
  --dry-run
```

The live drill is read-only with respect to the Paper OS stack. Treat a failed
checksum, count, table, source-set digest, data-file hash, constraint, or cleanup
check as a release blocker.
If the process is killed at the host level, confirm that no container beginning
with `paper-os-restore-drill-` remains before retrying.

## Rollback materialization without activation

After the restore drill passes, create a rollback candidate only in fresh named
volumes. This command never changes the Compose stack and refuses an existing
volume name. Start with a dry run:

```sh
python3 infra/papers/ops/paper_backup.py \
  --service-root /home/ubuntu/services/paper-os \
  --compose-file /home/ubuntu/services/paper-os/infra/papers/compose.yml \
  --env-file /home/ubuntu/services/paper-os/infra/papers/.env \
  --backup-root /home/ubuntu/services/paper-os/backups/automated \
  materialize-restore \
  --backup-dir /home/ubuntu/services/paper-os/backups/automated/REPLACE_WITH_BACKUP_ID \
  --dry-run
```

Remove `--dry-run` and add `--confirm-create-new-volumes` only after reviewing
the backup identifier. The command restores and proves every database invariant
and durable-data hash again, then reports one new database volume and one new
data volume. On failure it removes only those newly generated rollback volumes;
the live volumes and running stack are untouched.

Activation is deliberately not automated. Preserve the current Compose file,
image IDs, PostgreSQL volume, and data volume. Stop the stack without
`--volumes`, create a private Compose override that maps `paper_postgres_data`
and `paper_data` to the two reported external volumes, and start the recorded
previous images with that override. Verify database counts/SHA-set digest,
Reader current pointers, Cloudflare Access, direct-origin rejection, and mobile
reads before accepting the rollback. If any check fails, stop that candidate
and restart the untouched pre-rollback volumes. Never edit a named volume in
place and never use `down --volumes` for this procedure.

## Optional encrypted offsite copy (manual decision required)

Do not enable offsite transfer until the user has chosen all of the following:

- storage provider and account;
- retention and deletion policy on that provider;
- a separately configured rclone backend plus a `crypt` remote layered over it;
- who retains the crypt password/recovery material and how recovery access is
  tested without storing the secret in Git or this runbook.

After that decision, put the rclone config in an explicit private path with mode
`600` and install rclone `1.64` or newer on the host (`config redacted` was added
in that release). The command disables password prompts, so an encrypted rclone
configuration also needs a separately reviewed non-interactive credential
mechanism; the simplest initial setup is a mode-`600` config protected by host
access controls. Verify with `rclone config redacted REMOTE_NAME` that its type
is `crypt`. Test one upload by adding all three flags to the backup command:

```text
--offsite --rclone-config /ABSOLUTE/PRIVATE/rclone.conf \
--rclone-crypt-remote paper-backup-crypt:paper-os
```

The command fails closed if either value is absent, the remote is not reported
as `type = crypt`, or upload fails. It uploads all four payload files and then
the checksum manifest last, using immutable destinations. Complete a download
plus both-part restore drill before calling offsite recovery proven. Local
retention does not delete remote data.

## Timer template (not installed or enabled)

`paper-os-backup.service` and `paper-os-backup.timer` are reviewable templates.
They schedule one local backup around 03:30 Hong Kong time with a randomized
delay and 14-day retention. They intentionally omit offsite flags and alerts.
After reviewing paths and the host timezone behavior, an operator must manually
copy the files to `/etc/systemd/system/`, reload systemd, run the service once,
perform a restore drill, and only then enable the timer. This repository does
not authorize or perform those steps.

## Notification items that still require a human choice

Before wiring failure notifications, select the destination (for example a
private email or webhook), create a least-privilege credential outside Git,
decide whether backup identifiers and aggregate counts may be sent, and send a
real test failure through the chosen channel. Add a separately reviewed
`OnFailure=` unit only after those choices. Never place a webhook URL, token,
database error output, paper title, or dump path containing private metadata in
systemd unit text or logs.

At least quarterly, run a fresh restore drill and record only the date, backup
identifier, aggregate result, and unresolved risks in the project review flow.
