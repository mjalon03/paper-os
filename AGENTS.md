# Paper OS Agent Guide

## Identity

- `project_id`: `paper-os`
- Purpose: Operate the private Paper OS service for paper ingestion, mobile reading, knowledge extraction, search, and safe research automation.

Read `PROJECT_CONTEXT.md` and the centrally managed policy at `../Personal OS/docs/vault-management-spec.md` before recording or transferring project context.

## Obsidian Connection

Connect to the Obsidian project note with this exact `project_id`. Keep Git authoritative for code and tests; use Obsidian for concise, durable, reviewed knowledge and runbooks. Never copy or symlink the vault.

## Durable Recording

Record reviewed decisions and rationale, verified results, material constraints, blockers, and approved next actions. Exclude secrets, credentials, raw data, source-code copies, caches, generated output, logs, and unverified AI content.

After qualifying important work, capture one concise local knowledge candidate for each distinct result after verification and before the final handoff, without waiting for a separate prompt. Use `personal-os candidate capture --project-id paper-os` with the appropriate kind and evidence. Skip simple questions, read-only explanations, and facts already represented by an active equivalent candidate. Do not copy the session, transcripts, command output, or source code. If a safe evidence-backed candidate cannot be created, report the omission instead of inventing one. A candidate remains a reviewable local draft and never authorizes a vault edit.

## Safety

Treat AI output as a draft until verified. Do not modify the vault or external systems without explicit user authorization. Read existing notes first, preserve properties and links, and make the smallest useful change.

## Task Tracking

Track long-running, state-changing, interruption-sensitive, vault-related, backup, synchronization, and data-processing work. Exclude simple questions, read-only inspection, and short commands. For manageable local commands, use the Personal OS job lifecycle or `job run`; retain only a task identifier, lifecycle timestamps, and a safe category. Never place conversation content, commands, arguments, output, or secrets in task state.

## Automatic Local Commit Policy

After completing an explicitly requested implementation, create a local Git commit without additional confirmation only when all relevant tests and validation pass, the complete diff has been reviewed, no unresolved high- or medium-priority findings remain, and only expected project files are included. Never include secrets, machine-local state, external data, or unrelated user changes.

Use one concise commit for one verified logical change and report its hash. Do not automatically commit work performed only for explanation, planning, diagnosis, or review. Do not push, merge, rebase, tag, release, or publish without explicit user authorization. If any commit condition is uncertain, leave the changes uncommitted and report the blocker.
