"""Fail-closed Mac-side import of reviewed paper knowledge into Obsidian.

The writer sees only a configured vault directory and the fixed
``Research/Papers`` subtree.  It never scans the vault and it never overwrites
an existing note.  A small ledger outside the vault provides crash recovery,
revision immutability, and idempotency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any
from uuid import UUID

from .knowledge import (
    payload_sha256,
    render_knowledge_markdown,
    safe_knowledge_filename,
    validate_knowledge_payload,
)


VAULT_SUBTREE = Path("Research") / "Papers"
LEDGER_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PAPER_KEY_RE = re.compile(
    r"(?:int:[1-9][0-9]*|str:[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
)


class VaultSyncError(RuntimeError):
    """A vault or ledger safety invariant was not satisfied."""


@dataclass(frozen=True)
class ImportPlan:
    revision_id: str
    status: str
    relative_path: str
    payload_sha256: str
    markdown_sha256: str


@dataclass(frozen=True)
class ImportResult:
    result: str
    revision_id: str
    relative_path: str
    payload_sha256: str
    markdown_sha256: str


def _fail(code: str) -> VaultSyncError:
    return VaultSyncError(code)


def plan_vault_import(payload: Any) -> ImportPlan:
    """Create a write-free, deterministic plan for reviewed knowledge only."""
    canonical = validate_knowledge_payload(payload)
    if canonical["status"] != "reviewed":
        raise _fail("reviewed_status_required")
    markdown = render_knowledge_markdown(canonical).encode("utf-8")
    relative_path = (VAULT_SUBTREE / safe_knowledge_filename(canonical)).as_posix()
    return ImportPlan(
        revision_id=canonical["revision_id"],
        status=canonical["status"],
        relative_path=relative_path,
        payload_sha256=payload_sha256(canonical),
        markdown_sha256=hashlib.sha256(markdown).hexdigest(),
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _regular_file_bytes_at(
    directory_descriptor: int, name: str, *, missing_code: str
) -> bytes:
    try:
        details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _fail(missing_code) from exc
    if not stat.S_ISREG(details.st_mode):
        raise _fail("target_not_regular_file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise _fail("target_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _fail("target_not_regular_file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 10_000_000:
                raise _fail("target_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_directory(path: Path) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise _fail("safe_directory_open_unsupported")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise _fail("vault_subtree_invalid")
    return descriptor


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise _fail("safe_directory_open_unsupported")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise _fail("vault_subtree_changed") from exc
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise _fail("vault_subtree_invalid")
    return descriptor


def _directory_descriptor_matches(path: Path, descriptor: int) -> bool:
    try:
        path_details = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (
        stat.S_ISDIR(path_details.st_mode)
        and path_details.st_dev == opened.st_dev
        and path_details.st_ino == opened.st_ino
    )


def _open_vault_subtree(root: Path) -> tuple[Path, int]:
    root_descriptor = _open_directory(root)
    research_descriptor = -1
    try:
        if not _directory_descriptor_matches(root, root_descriptor):
            raise _fail("vault_root_changed")
        research = root / VAULT_SUBTREE.parts[0]
        research_descriptor = _open_directory_at(
            root_descriptor, VAULT_SUBTREE.parts[0]
        )
        if not _directory_descriptor_matches(research, research_descriptor):
            raise _fail("vault_subtree_changed")
        papers = research / VAULT_SUBTREE.parts[1]
        papers_descriptor = _open_directory_at(
            research_descriptor, VAULT_SUBTREE.parts[1]
        )
        if not _directory_descriptor_matches(papers, papers_descriptor):
            os.close(papers_descriptor)
            raise _fail("vault_subtree_changed")
        return papers, papers_descriptor
    finally:
        if research_descriptor >= 0:
            os.close(research_descriptor)
        os.close(root_descriptor)


def _entry_details_at(directory_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _rename_no_replace(
    directory_descriptor: int, source_name: str, target_name: str
) -> None:
    """Atomically publish ``source`` while refusing an existing target.

    macOS and Linux expose exclusive rename variants outside Python's standard
    library.  A hard-link publication is the safe atomic fallback on local
    filesystems; unlike ``os.rename``, it can never clobber an Obsidian note.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    function = None
    arguments: tuple[Any, ...] = ()
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is not None:
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            arguments = (
                directory_descriptor,
                os.fsencode(source_name),
                directory_descriptor,
                os.fsencode(target_name),
                0x00000004 | 0x00000010,
            )
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is not None:
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            arguments = (
                directory_descriptor,
                os.fsencode(source_name),
                directory_descriptor,
                os.fsencode(target_name),
                1,
            )
    if function is not None:
        if function(*arguments) == 0:
            return
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise _fail("target_collision")
        unsupported = {errno.ENOSYS, errno.EINVAL}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if code not in unsupported:
            raise OSError(code, os.strerror(code), target_name)
    try:
        os.link(
            source_name,
            target_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise _fail("target_collision") from exc
    os.unlink(source_name, dir_fd=directory_descriptor)


def _empty_ledger() -> dict[str, Any]:
    return {"schema_version": LEDGER_SCHEMA_VERSION, "imports": {}, "papers": {}}


def _validate_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "imports",
        "papers",
    }:
        raise _fail("ledger_schema_invalid")
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != LEDGER_SCHEMA_VERSION
    ):
        raise _fail("ledger_version_invalid")
    imports = value["imports"]
    papers = value["papers"]
    if not isinstance(imports, dict) or not isinstance(papers, dict):
        raise _fail("ledger_schema_invalid")
    for revision_id, entry in imports.items():
        if not isinstance(revision_id, str) or not isinstance(entry, dict):
            raise _fail("ledger_import_invalid")
        try:
            parsed_revision_id = UUID(revision_id)
        except (ValueError, AttributeError) as exc:
            raise _fail("ledger_revision_invalid") from exc
        if parsed_revision_id.int == 0 or str(parsed_revision_id) != revision_id:
            raise _fail("ledger_revision_invalid")
        if set(entry) != {
            "paper_key",
            "relative_path",
            "payload_sha256",
            "markdown_sha256",
        }:
            raise _fail("ledger_import_invalid")
        if not all(isinstance(entry[key], str) for key in entry):
            raise _fail("ledger_import_invalid")
        relative = Path(entry["relative_path"])
        if (
            relative.is_absolute()
            or relative.parts[:2] != VAULT_SUBTREE.parts
            or len(relative.parts) != 3
            or relative.suffix != ".md"
            or "\\" in entry["relative_path"]
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in entry["relative_path"]
            )
        ):
            raise _fail("ledger_path_invalid")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise _fail("ledger_path_invalid")
        if not _SHA256_RE.fullmatch(entry["payload_sha256"]) or not _SHA256_RE.fullmatch(
            entry["markdown_sha256"]
        ):
            raise _fail("ledger_hash_invalid")
    for paper_key, revision_id in papers.items():
        if not isinstance(paper_key, str) or not isinstance(revision_id, str):
            raise _fail("ledger_paper_invalid")
        if not _PAPER_KEY_RE.fullmatch(paper_key):
            raise _fail("ledger_paper_invalid")
        if revision_id not in imports or imports[revision_id]["paper_key"] != paper_key:
            raise _fail("ledger_paper_invalid")
    if set(papers.values()) != set(imports) or len(set(papers.values())) != len(papers):
        raise _fail("ledger_paper_invalid")
    return value


def _load_ledger(directory_descriptor: int, name: str) -> dict[str, Any]:
    details = _entry_details_at(directory_descriptor, name)
    if details is None:
        return _empty_ledger()
    if not stat.S_ISREG(details.st_mode):
        raise _fail("ledger_file_invalid")
    raw = _regular_file_bytes_at(
        directory_descriptor, name, missing_code="ledger_file_missing"
    )
    if len(raw) > 5_000_000:
        raise _fail("ledger_file_too_large")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _fail("ledger_duplicate_key")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise _fail(f"ledger_non_finite_{value}")

    def parse_integer(value: str) -> int:
        digits = value[1:] if value.startswith("-") else value
        if len(digits) > 19:
            raise _fail("ledger_integer_too_long")
        return int(value)

    def reject_float(value: str) -> None:
        raise _fail("ledger_float_unsupported")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
            parse_int=parse_integer,
            parse_float=reject_float,
        )
    except RecursionError as exc:
        raise _fail("ledger_json_too_deep") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("ledger_json_invalid") from exc
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > 64:
            raise _fail("ledger_json_too_deep")
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth + 1) for child in children)
    return _validate_ledger(value)


def _write_ledger(
    directory_descriptor: int, name: str, ledger: dict[str, Any]
) -> None:
    _validate_ledger(ledger)
    encoded = (
        json.dumps(
            ledger,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = -1
    temporary_name = ""
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for _ in range(32):
            temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            break
        if descriptor < 0:
            raise _fail("ledger_temporary_file_collision")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        existing = _entry_details_at(directory_descriptor, name)
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise _fail("ledger_file_invalid")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
        readback = _regular_file_bytes_at(
            directory_descriptor, name, missing_code="ledger_file_missing"
        )
        if hashlib.sha256(readback).digest() != hashlib.sha256(encoded).digest():
            raise _fail("ledger_readback_mismatch")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _paper_key(paper_id: int | str) -> str:
    return f"int:{paper_id}" if isinstance(paper_id, int) else f"str:{paper_id}"


def _audit_ledger_targets(
    directory_descriptor: int,
    ledger: dict[str, Any],
    *,
    skip_revision_id: str | None = None,
) -> None:
    """Verify only ledger-known notes; never enumerate unrelated vault content."""
    for revision_id, entry in ledger["imports"].items():
        if revision_id == skip_revision_id:
            continue
        target_name = Path(entry["relative_path"]).name
        try:
            contents = _regular_file_bytes_at(
                directory_descriptor,
                target_name,
                missing_code="ledger_target_missing",
            )
        except VaultSyncError as exc:
            if str(exc) == "target_not_regular_file":
                raise _fail("ledger_target_not_regular") from exc
            raise
        if hashlib.sha256(contents).hexdigest() != entry["markdown_sha256"]:
            raise _fail("ledger_target_changed")


class VaultSyncWriter:
    """Create paper notes in one fixed subtree without inspecting the vault."""

    def __init__(
        self,
        vault_root: str | Path,
        ledger_path: str | Path,
    ) -> None:
        self.vault_root = Path(vault_root)
        self.ledger_path = Path(ledger_path)

    def _safe_roots(self) -> tuple[Path, Path, Path]:
        if self.vault_root.is_symlink() or not self.vault_root.is_dir():
            raise _fail("vault_root_invalid")
        root = self.vault_root.resolve(strict=True)
        if root == Path(root.anchor):
            raise _fail("vault_root_too_broad")
        ledger_parent_input = self.ledger_path.parent
        if ledger_parent_input.is_symlink() or not ledger_parent_input.is_dir():
            raise _fail("ledger_parent_invalid")
        ledger_parent = ledger_parent_input.resolve(strict=True)
        if (
            not self.ledger_path.name
            or self.ledger_path.name in {".", ".."}
            or len(self.ledger_path.name.encode("utf-8")) > 240
        ):
            raise _fail("ledger_filename_invalid")
        ledger = ledger_parent / self.ledger_path.name
        if _is_within(ledger, root):
            raise _fail("ledger_must_be_outside_vault")
        if ledger.is_symlink():
            raise _fail("ledger_file_invalid")
        if ledger.exists() and not ledger.is_file():
            raise _fail("ledger_file_invalid")
        return root, root / VAULT_SUBTREE, ledger

    @staticmethod
    def _ensure_subtree(root: Path) -> Path:
        current = root
        for part in VAULT_SUBTREE.parts:
            current = current / part
            if current.is_symlink():
                raise _fail("vault_subtree_symlink")
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if current.is_symlink() or not current.is_dir():
                raise _fail("vault_subtree_invalid")
        resolved = current.resolve(strict=True)
        if not _is_within(resolved, root) or resolved != root / VAULT_SUBTREE:
            raise _fail("vault_subtree_outside_root")
        return resolved

    @staticmethod
    def _existing_subtree(root: Path) -> Path | None:
        current = root
        for part in VAULT_SUBTREE.parts:
            current = current / part
            if current.is_symlink():
                raise _fail("vault_subtree_symlink")
            if not current.exists():
                return None
            if not current.is_dir():
                raise _fail("vault_subtree_invalid")
        resolved = current.resolve(strict=True)
        if not _is_within(resolved, root) or resolved != root / VAULT_SUBTREE:
            raise _fail("vault_subtree_outside_root")
        return resolved

    @staticmethod
    def _target(subtree: Path, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or relative.parts[:2] != VAULT_SUBTREE.parts:
            raise _fail("target_path_invalid")
        filename_parts = relative.parts[2:]
        if len(filename_parts) != 1 or filename_parts[0] in {"", ".", ".."}:
            raise _fail("target_path_invalid")
        target = subtree / filename_parts[0]
        if target.parent.resolve(strict=True) != subtree:
            raise _fail("target_outside_subtree")
        return target

    @staticmethod
    def _expected_entry(
        plan: ImportPlan, paper_key: str
    ) -> dict[str, str]:
        return {
            "paper_key": paper_key,
            "relative_path": plan.relative_path,
            "payload_sha256": plan.payload_sha256,
            "markdown_sha256": plan.markdown_sha256,
        }

    def import_payload(self, payload: Any) -> ImportResult:
        canonical = validate_knowledge_payload(payload)
        plan = plan_vault_import(canonical)
        markdown = render_knowledge_markdown(canonical).encode("utf-8")
        root, expected_subtree, ledger_path = self._safe_roots()
        ledger_parent_descriptor = _open_directory(ledger_path.parent)
        if not _directory_descriptor_matches(
            ledger_path.parent, ledger_parent_descriptor
        ):
            os.close(ledger_parent_descriptor)
            raise _fail("ledger_parent_changed")
        lock_name = f".{ledger_path.name}.lock"
        lock_details = _entry_details_at(ledger_parent_descriptor, lock_name)
        if lock_details is not None and not stat.S_ISREG(lock_details.st_mode):
            os.close(ledger_parent_descriptor)
            raise _fail("ledger_lock_invalid")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_descriptor = os.open(
                lock_name,
                flags,
                0o600,
                dir_fd=ledger_parent_descriptor,
            )
        except OSError:
            os.close(ledger_parent_descriptor)
            raise
        subtree_descriptor = -1
        try:
            os.fchmod(lock_descriptor, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            root_again, _, ledger_again = self._safe_roots()
            if root_again != root or ledger_again != ledger_path:
                raise _fail("configured_path_changed")
            if not _directory_descriptor_matches(
                ledger_path.parent, ledger_parent_descriptor
            ):
                raise _fail("ledger_parent_changed")
            ledger = _load_ledger(ledger_parent_descriptor, ledger_path.name)
            existing_subtree = self._existing_subtree(root)
            if ledger["imports"] and existing_subtree is None:
                raise _fail("ledger_history_targets_unavailable")
            subtree = self._ensure_subtree(root)
            if subtree != expected_subtree:
                raise _fail("vault_subtree_changed")
            opened_subtree, subtree_descriptor = _open_vault_subtree(root)
            if opened_subtree != subtree:
                raise _fail("vault_subtree_changed")
            if not _directory_descriptor_matches(subtree, subtree_descriptor):
                raise _fail("vault_subtree_changed")
            target = self._target(subtree, plan.relative_path)
            target_name = target.name
            paper_key = _paper_key(canonical["paper_identity"]["paper_id"])
            expected_entry = self._expected_entry(plan, paper_key)

            prior = ledger["imports"].get(plan.revision_id)
            _audit_ledger_targets(
                subtree_descriptor,
                ledger,
                skip_revision_id=plan.revision_id,
            )
            if prior is not None:
                if prior != expected_entry:
                    raise _fail("revision_id_reused")
                target_bytes = _regular_file_bytes_at(
                    subtree_descriptor,
                    target_name,
                    missing_code="imported_target_missing",
                )
                if hashlib.sha256(target_bytes).hexdigest() != plan.markdown_sha256:
                    raise _fail("imported_target_changed")
                return ImportResult(
                    result="already_imported",
                    revision_id=plan.revision_id,
                    relative_path=plan.relative_path,
                    payload_sha256=plan.payload_sha256,
                    markdown_sha256=plan.markdown_sha256,
                )

            prior_revision = ledger["papers"].get(paper_key)
            if prior_revision is not None:
                raise _fail("paper_already_imported_create_only")

            recovery = False
            target_details = _entry_details_at(subtree_descriptor, target_name)
            if target_details is not None:
                if stat.S_ISLNK(target_details.st_mode):
                    raise _fail("target_symlink")
                if not stat.S_ISREG(target_details.st_mode):
                    raise _fail("target_not_regular_file")
                target_bytes = _regular_file_bytes_at(
                    subtree_descriptor, target_name, missing_code="target_missing"
                )
                if hashlib.sha256(target_bytes).hexdigest() != plan.markdown_sha256:
                    raise _fail("target_collision")
                recovery = True
            else:
                descriptor = -1
                temporary_name = ""
                published_identity: tuple[int, int] | None = None
                try:
                    temporary_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        temporary_flags |= os.O_NOFOLLOW
                    for _ in range(32):
                        temporary_name = (
                            f".paper-knowledge-{secrets.token_hex(16)}.tmp"
                        )
                        try:
                            descriptor = os.open(
                                temporary_name,
                                temporary_flags,
                                0o600,
                                dir_fd=subtree_descriptor,
                            )
                        except FileExistsError:
                            continue
                        break
                    if descriptor < 0:
                        raise _fail("temporary_file_collision")
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        descriptor = -1
                        handle.write(markdown)
                        handle.flush()
                        os.fsync(handle.fileno())
                        temporary_details = os.fstat(handle.fileno())
                        published_identity = (
                            temporary_details.st_dev,
                            temporary_details.st_ino,
                        )
                    if _entry_details_at(subtree_descriptor, target_name) is not None:
                        raise _fail("target_collision")
                    if not _directory_descriptor_matches(
                        subtree, subtree_descriptor
                    ):
                        raise _fail("vault_subtree_changed")
                    _rename_no_replace(
                        subtree_descriptor, temporary_name, target_name
                    )
                    if not _directory_descriptor_matches(
                        subtree, subtree_descriptor
                    ):
                        current_target = _entry_details_at(
                            subtree_descriptor, target_name
                        )
                        if (
                            published_identity is not None
                            and current_target is not None
                            and (current_target.st_dev, current_target.st_ino)
                            == published_identity
                        ):
                            try:
                                os.unlink(target_name, dir_fd=subtree_descriptor)
                            except FileNotFoundError:
                                pass
                            os.fsync(subtree_descriptor)
                        raise _fail("vault_subtree_changed")
                    os.fsync(subtree_descriptor)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    if temporary_name:
                        try:
                            os.unlink(temporary_name, dir_fd=subtree_descriptor)
                        except FileNotFoundError:
                            pass

            if not _directory_descriptor_matches(subtree, subtree_descriptor):
                raise _fail("vault_subtree_changed")
            target_bytes = _regular_file_bytes_at(
                subtree_descriptor, target_name, missing_code="target_missing"
            )
            if hashlib.sha256(target_bytes).hexdigest() != plan.markdown_sha256:
                raise _fail("target_readback_mismatch")
            ledger["imports"][plan.revision_id] = expected_entry
            ledger["papers"][paper_key] = plan.revision_id
            _write_ledger(ledger_parent_descriptor, ledger_path.name, ledger)
            return ImportResult(
                result="recovered" if recovery else "created",
                revision_id=plan.revision_id,
                relative_path=plan.relative_path,
                payload_sha256=plan.payload_sha256,
                markdown_sha256=plan.markdown_sha256,
            )
        finally:
            try:
                if subtree_descriptor >= 0:
                    os.close(subtree_descriptor)
            finally:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(lock_descriptor)
                    finally:
                        os.close(ledger_parent_descriptor)

    def preflight(self, payload: Any) -> dict[str, Any]:
        """Check current filesystem/ledger state without creating any file."""
        canonical = validate_knowledge_payload(payload)
        plan = plan_vault_import(canonical)
        root, _, ledger_path = self._safe_roots()
        ledger_parent_descriptor = _open_directory(ledger_path.parent)
        try:
            if not _directory_descriptor_matches(
                ledger_path.parent, ledger_parent_descriptor
            ):
                raise _fail("ledger_parent_changed")
            ledger = _load_ledger(ledger_parent_descriptor, ledger_path.name)
        finally:
            os.close(ledger_parent_descriptor)
        paper_key = _paper_key(canonical["paper_identity"]["paper_id"])
        expected_entry = self._expected_entry(plan, paper_key)
        prior = ledger["imports"].get(plan.revision_id)
        if prior is not None and prior != expected_entry:
            raise _fail("revision_id_reused")
        prior_revision = ledger["papers"].get(paper_key)
        if prior is None and prior_revision is not None:
            raise _fail("paper_already_imported_create_only")

        subtree = self._existing_subtree(root)
        if subtree is None:
            if ledger["imports"]:
                raise _fail("ledger_history_targets_unavailable")
            state = "ready_new_subtree"
        else:
            opened_subtree, descriptor = _open_vault_subtree(root)
            try:
                if opened_subtree != subtree:
                    raise _fail("vault_subtree_changed")
                if not _directory_descriptor_matches(subtree, descriptor):
                    raise _fail("vault_subtree_changed")
                _audit_ledger_targets(
                    descriptor,
                    ledger,
                    skip_revision_id=plan.revision_id,
                )
                target_name = Path(plan.relative_path).name
                details = _entry_details_at(descriptor, target_name)
                if details is None:
                    if prior is not None:
                        raise _fail("imported_target_missing")
                    state = "ready"
                else:
                    if stat.S_ISLNK(details.st_mode):
                        raise _fail("target_symlink")
                    if not stat.S_ISREG(details.st_mode):
                        raise _fail("target_not_regular_file")
                    contents = _regular_file_bytes_at(
                        descriptor, target_name, missing_code="target_missing"
                    )
                    if hashlib.sha256(contents).hexdigest() != plan.markdown_sha256:
                        if prior is not None:
                            raise _fail("imported_target_changed")
                        raise _fail("target_collision")
                    state = "already_imported" if prior is not None else "recoverable"
            finally:
                os.close(descriptor)
        return {
            **asdict(plan),
            "filesystem_preflight": state,
            "validation_scope": "structural; source proof is server-owned",
        }

    def plan(self, payload: Any) -> dict[str, Any]:
        """Return the same sanitized plan emitted by the CLI dry run."""
        return asdict(plan_vault_import(payload))


__all__ = [
    "ImportPlan",
    "ImportResult",
    "LEDGER_SCHEMA_VERSION",
    "VAULT_SUBTREE",
    "VaultSyncError",
    "VaultSyncWriter",
    "plan_vault_import",
]
