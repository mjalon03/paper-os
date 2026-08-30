# Paper OS separation handoff

## Scope and evidence

This is a concise, repository-local handoff created during the 2026-08-30
separation from Personal OS. It preserves the historical Codex task identity
`01a04904-5a92-77b2-9037-2b1d7dd19f5d` (`Design AI paper knowledge system`)
without copying its transcript. Repository history, local candidate state, and
the task's verified completion summaries are the evidence for this handoff.

## Verified repository state

- Paper OS is the active registered project (`project_id: paper-os`) at commit
  `465c5f3`. Its 147 retained source paths still match the Personal OS source
  tree exactly; the repository has 153 tracked paths including separation
  metadata and documentation.
- Ten Paper-related commits are retained, from the private-server foundation
  through the 0.6 release candidate. The source-to-filtered map remains local
  Git metadata, as described in `docs/migration-from-personal-os.md`.
- The Paper suite previously passed 636 tests with four opt-in PostgreSQL
  probes skipped locally. Docker is unavailable on this Mac, so Compose was
  not revalidated here.
- GitHub SSH authentication for the configured account succeeds. No GitHub
  remote is configured for this repository.

## Durable decisions and constraints

- Keep this repository authoritative for Paper OS source, tests, deployment
  configuration, and operational documentation. Obsidian is only concise,
  reviewed project knowledge; it is never a source mirror.
- The mobile reader uses structured PMC JATS when eligible and a bounded
  Docling PDF fallback otherwise. Reader v2 is the default for the organized
  library; Reader v1 remains available as a recovery path.
- Production 0.6 was deployed with the API, schema, isolated conversion
  services, and local search. The historical verified snapshot retained 18
  papers, 9 jobs, and 18 search indexes. These are historical deployment facts,
  not a fresh production probe.
- External-connected processing remains an explicit boundary: Drive, PMC, and
  DeepSeek-capable workers are inactive pending separate approval. Credentials,
  production services, Drive, and the live Vault were not changed by this
  separation work.
- The release process has a verified backup and isolated restore drill. Any
  later production change must follow `infra/papers/ops/RELEASE.md` and its
  backup gate.

## Remaining actions and authority

1. Migrate the nine legacy Paper candidates under the transaction requirements
   in `docs/personal-os-migration-plan.md`; do not edit queue JSON directly.
2. Create the canonical Paper OS Vault note and, only after it exists, use a
   bounded, backup-backed managed-block workflow to move Paper material out of
   the Personal OS AI Draft.
3. With explicit approval, activate external-connected workers and verify
   their heartbeats and side effects. A user-authenticated Cloudflare Access
   browser check remains the right final mobile UI gate.
4. Add a GitHub remote only after the user supplies or approves the exact
   repository destination; pushing is a separate approval.
5. Remove the duplicated source paths from Personal OS only after candidate and
   Vault migration, deployment-path cutover, and a confirmed recovery point.

## Excluded intentionally

This handoff contains no credentials, raw research data, deployment commands,
full task transcript, or verbose command output.
