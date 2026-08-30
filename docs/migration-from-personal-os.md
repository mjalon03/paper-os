# Migration from Personal OS

## Scope

Paper OS was separated from `/Users/alon-jo/Developer/Personal OS` on 2026-08-30 without rewriting or deleting the source repository.

The filtered repository retains only:

- `paper_service/`
- `infra/papers/`
- `tests/test_paper*.py`
- `docs/paper-reader-v2-spec.md`

## History preservation

The filter analyzed 44 source commits and retained ten commits containing Paper OS changes. The source HEAD `232a8efb8d33339a6c6bf61f25beedcc7de52a24` maps to filtered HEAD `485cb028dc315d35a6f84066feeef8440e4bb156`.

The complete source-to-filtered commit map is retained in local Git metadata at `.git/filter-repo/commit-map` and is intentionally not version-controlled.

## Verification

- 147 filtered tracked paths exactly match the selected source paths.
- Every filtered file has the same Git blob as the source HEAD.
- The filtered tree contains no unexpected project paths, environment files, PDFs, databases, private keys, or symbolic links.
- The local Paper suite passed 636 tests; four PostgreSQL-dependent tests were skipped in the local environment.
- Python compile validation passed.
- Docker CLI was unavailable on the migration Mac, so Compose validation was not repeated after extraction. The Compose files are byte-identical to the previously verified source tree.

## Preserved boundaries

The migration did not modify the source repository, production server, Docker services or volumes, Google Drive, Obsidian, credentials, external workers, schedules, or GitHub.
