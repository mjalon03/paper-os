# Personal OS migration plan

## Current inventory

- The Personal OS Git tree still retains all 147 Paper source paths: 73 under
  `paper_service/`, 23 under `infra/papers/`, 50 Paper tests, and the Reader v2
  specification. They remain blob-identical to this repository.
- The Personal OS candidate queue contains nine pending Paper candidates. Its
  pre-migration SHA-256 is
  `55142da033b5c06e7f1e5bdb2372ef5e79c4b7307bf5bb6be73ae8e064cce12f`.
- The registered Paper OS metadata and local registry agree on
  `Projects/Paper OS/Paper OS.md`, but the review configuration allowlist still
  contains only `personal-os`; the Paper OS review/draft dry runs therefore
  fail closed.
- The live Vault has no `Projects/Paper OS/Paper OS.md`. One owned Personal OS
  AI Draft exists for 2026-08-30, with an existing retained backup. It contains
  exactly the three historical Reader-v2 candidate blocks listed below; it has
  no blocks for the six later 0.6 candidates.

## Legacy candidate manifest

The IDs, timestamps, kinds, and input digests below are the audit baseline.
The source records, including evidence, context, kind details, status, and
review history, must be preserved exactly by an approved migration.

| Original candidate ID | Created (UTC) | Kind | Input digest |
| --- | --- | --- | --- |
| `eb8ed425-e475-45f8-9659-28a4908d0941` | 2026-08-29T07:59:52Z | approved-next-action | `0ba40194c935d8af6c9cd38a04a63aa8a54f644e33511887e8ef6039b2630a0e` |
| `ee32167a-86c0-453b-9350-c640738a9a37` | 2026-08-29T08:56:35Z | verified-result | `e89acf6e802ec9307dacfa051d13926f071189df3150dfbe318de923a5af289f` |
| `9d1b84ea-2273-4cba-b9f5-9c260eca71b9` | 2026-08-29T11:43:55Z | verified-result | `ea1f5f957ed35ff0ee5b94e4bd59c971aece93fb6eecfa2568725b3186d59666` |
| `9c6c88c6-d078-4d97-a63e-691ebf8ee3dc` | 2026-08-30T09:16:54Z | verified-result | `87502544ec4313d94183cb451bfd939911cd455914279fe09fdf4610db0a44fc` |
| `90c86c51-faee-4393-81c3-f1d66145b996` | 2026-08-30T09:16:54Z | review-item | `e49950240090fe1225c015624b469518a5d14dd6b7f4bf8445daafa73e88c358` |
| `8e65a022-3290-4080-957e-e4c2eaa7f348` | 2026-08-30T10:46:58Z | verified-result | `810c306160b11cfc27a27cb67527978c2733278f1d6f7748ef5c5185e342c46e` |
| `e24663f1-f5a8-4766-890d-64920e632484` | 2026-08-30T10:46:58Z | review-item | `4771e1cf2ca90f93bec97ff41ee083a01fcad816089233f1ec3abf30d14d84e6` |
| `b2b32d95-2b51-4e86-a8fe-a3b5cc29d484` | 2026-08-30T10:46:58Z | review-item | `dc2d8f49710fe9d6893b3d2b5293892783f12f0c6b862d4f904a748f517c617b` |
| `3e784dfa-4aa9-4f36-9be5-730f74da3fb0` | 2026-08-30T11:09:49Z | verified-result | `c43535bfdcb591da546ba72a18413879299f9cd55b804e839a43f1cedd7272aa` |

## Required migration design

The current `personal-os candidate` CLI has no cross-project migration command.
It also has no field that can record a source project, original candidate ID,
or migration event. Direct JSON editing would lose the queue's validated,
atomic, audit-preserving behavior and is prohibited.

An approved implementation must instead:

1. Take a private exact backup of the queue and validate the manifest digests.
2. Add explicit immutable provenance for every migrated record: original ID,
   source project, pre-migration input digest, migration timestamp, and
   approver. Preserve the original IDs where the queue model allows it, or keep
   an immutable old-to-new ID map if a new ID is required.
3. Atomically change project ownership to `paper-os`, target
   `Projects/Paper OS/Paper OS.md`, and preserve every other supported field.
4. Re-read the queue under its lock; prove the nine mappings, status/history,
   source digest evidence, and original candidate count; then verify repeat is
   a no-op.
5. Only after the project is included in the reviewed allowlist, run the
   applicable Paper OS dry-run. Do not enable draft writes, DeepSeek, schedules,
   or Vault promotion as part of candidate migration.

## Vault migration gate

The canonical project note must be created first with the required headings
from the promotion specification. A live write requires explicit Vault-edit
authorization and must create a same-Vault exact-original backup, use one
managed block per candidate with stable candidate/input digests, atomically
replace and read back the note, and prove an immediate repeat is a no-op.

Removing the three Paper blocks from the Personal OS AI Draft is not supported
by the existing bounded draft writer; it intentionally has no cross-project
removal operation. An approved adapter or a tightly scoped manual edit must
first back up that owned draft, validate its ownership and all markers, remove
only the three manifest blocks (`eb8ed425`, `ee32167a`, and `9d1b84ea`),
read back the exact result, and leave a repeat no-op. It must not remove the
entire draft or alter unrelated blocks.

## Source cleanup gate

Deletion from Personal OS is destructive and is not authorized by this plan.
Before it can happen, require all of the following:

1. The candidate transaction and approved Vault migration are complete and
   verified.
2. All Personal OS documentation, scripts, project metadata, task tracking,
   and deployment runbooks point to this repository rather than the old paths.
3. The production deployment source path has been independently confirmed to
   use a reviewed Paper OS checkout and a current backup/recovery point exists.
4. The exact 147-path deletion manifest is reviewed; no environment files,
   state, credentials, backups, PDFs, or unrelated Personal OS paths are in
   scope.
5. The post-removal Personal OS suite and this repository's Paper suite pass,
   and a local commit is reviewed before any push.
