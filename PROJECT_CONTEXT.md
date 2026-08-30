---
project_id: paper-os
status: active
---

# Paper OS Context

## Purpose

Operate a private research-paper system that ingests and organizes papers, provides a secure mobile reader, extracts durable knowledge, and supports grounded search while preserving source artifacts and operational recovery paths.

## Current Status

- Paper OS code and its ten relevant commits were extracted from the Personal OS repository into this independent repository on 2026-08-30.
- The extracted tree contains `paper_service/`, `infra/papers/`, Paper-specific tests, and the Paper Reader v2 specification.
- All 147 extracted tracked files match the source repository blobs exactly.
- The local Paper test suite passes 636 tests, with four environment-dependent PostgreSQL tests skipped locally.
- The existing production service and its external credentials were not changed by the repository separation.

## Boundaries

- Git is authoritative for source, tests, deployment configuration, and operational documentation.
- Obsidian holds durable project knowledge and context; raw papers, databases, backups, secrets, and full task transcripts stay outside the Vault.
- Production deployment, external-connected workers, Drive changes, and DeepSeek calls require their own operational gates.
- Preserve the original source-to-filtered commit map under local Git metadata for migration audit.

## Vault Connection

This project connects logically to Projects/Paper OS/Paper OS.md through project_id: paper-os.

The project note has not yet been created. Vault creation and candidate migration remain separate authorized steps.
