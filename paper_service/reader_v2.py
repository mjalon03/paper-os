"""Build version-two reader artifacts while v1 remains live."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .article import ArticleValidationError, validate_article
from .artifact_versions import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    ArtifactResolution,
    ArtifactVersionError,
    StagedArtifactVersion,
    activate_staged_version,
    artifact_store_paths,
    build_version_descriptor_from_directory,
    create_staged_version_from_directory,
    resolve_current_artifact,
    safe_relative_artifact_path,
    verify_raw_source_sha256,
    verify_version_directory,
    version_directory,
)
from .docling_adapter import DoclingAdapterFailure, adapt_docling
from .jats import JATSFailure, parse_jats
from .pmc import Fetch, PMCFailure, _fetch, discover_article, download_media, fetch_jats, media_urls
from .selectors import NodeSelector, SelectorIndex, build_selector_index


DISPLAY_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
READER_V2_RENDER_VERSION = "reader-html:3"
READER_V2_EXTRACTOR_VERSIONS = {
    "pmc_jats": "pmc-jats:1+adapter.1",
    "docling_pdf": "docling:2.71.0+adapter.1",
}
READER_V2_SCRATCH_BUILD_PREFIX = "paper-reader-v2-build-"
READER_V2_SCRATCH_OWNER_NAME = ".paper-reader-v2-owned"
READER_V2_SCRATCH_OWNER_BYTES = b"paper-reader-v2-scratch-v1\n"
READER_V2_SCRATCH_MAX_BYTES_DEFAULT = 1024 * 1024 * 1024
READER_V2_SCRATCH_MIN_FREE_BYTES_DEFAULT = 512 * 1024 * 1024
READER_V2_SCRATCH_STALE_SECONDS_DEFAULT = 3600
READER_V2_SCRATCH_MAX_FILES = 4_096
READER_V2_ARTICLE_MAX_BYTES = 16 * 1024 * 1024
READER_V2_DOCLING_JSON_MAX_BYTES = 128 * 1024 * 1024
_EXPECTED_CURRENT_UNSET = object()


class ReaderV2Failure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


PMCArticleBuilder = Callable[
    [dict[str, Any], Path], dict[str, Any] | None
]
PDFArticleConverter = Callable[[dict[str, Any], Path, Path], str]


@dataclass(frozen=True)
class PreparedPaperV2Version:
    """A verified Reader v2 build that is not yet visible to readers."""

    data_dir: Path
    paper_id: int
    logical_manifest_path: str
    source: str
    source_sha256: str
    version_id: str
    expected_current_version_id: str | None
    staged: StagedArtifactVersion | None
    reused_current: bool = False


@dataclass(frozen=True)
class ReaderV2ScratchConfig:
    root: Path
    max_bytes: int
    min_free_bytes: int
    stale_seconds: int


def _bounded_scratch_integer(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        raise ReaderV2Failure("reader_v2_scratch_config_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ReaderV2Failure("reader_v2_scratch_config_invalid")
    return value


def reader_v2_scratch_config(
    env: Mapping[str, str] | None = None,
) -> ReaderV2ScratchConfig:
    values = os.environ if env is None else env
    raw_root = values.get("PAPER_READER_V2_SCRATCH_DIR")
    if raw_root:
        root = Path(raw_root)
    else:
        root = Path(tempfile.gettempdir()) / f"paper-reader-v2-scratch-{os.getuid()}"
        try:
            root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    if not root.is_absolute():
        raise ReaderV2Failure("reader_v2_scratch_config_invalid")
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReaderV2Failure("reader_v2_scratch_unavailable")
    if not raw_root and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ReaderV2Failure("reader_v2_scratch_unavailable")
    return ReaderV2ScratchConfig(
        root=root,
        max_bytes=_bounded_scratch_integer(
            values,
            "PAPER_READER_V2_SCRATCH_MAX_BYTES",
            READER_V2_SCRATCH_MAX_BYTES_DEFAULT,
            minimum=MAX_TOTAL_BYTES,
            maximum=8 * 1024 * 1024 * 1024,
        ),
        min_free_bytes=_bounded_scratch_integer(
            values,
            "PAPER_READER_V2_SCRATCH_MIN_FREE_BYTES",
            READER_V2_SCRATCH_MIN_FREE_BYTES_DEFAULT,
            minimum=0,
            maximum=64 * 1024 * 1024 * 1024,
        ),
        stale_seconds=_bounded_scratch_integer(
            values,
            "PAPER_READER_V2_SCRATCH_STALE_SECONDS",
            READER_V2_SCRATCH_STALE_SECONDS_DEFAULT,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        ),
    )


def _scratch_usage(root: Path) -> tuple[int, int]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReaderV2Failure("reader_v2_scratch_unavailable")
    total = 0
    files = 0
    try:
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directory_names:
                child = (current_path / name).lstat()
                # os.walk(followlinks=False) never traverses a symlink.  Ignore
                # its target size here; artifact collection rejects it before
                # publication and cleanup only unlinks the scratch entry.
                if not stat.S_ISDIR(child.st_mode):
                    continue
            for name in file_names:
                child = (current_path / name).lstat()
                if not stat.S_ISREG(child.st_mode):
                    continue
                files += 1
                total += child.st_size
                if files > READER_V2_SCRATCH_MAX_FILES:
                    raise ReaderV2Failure("reader_v2_scratch_file_limit")
    except ReaderV2Failure:
        raise
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    return files, total


def _enforce_scratch_budget(config: ReaderV2ScratchConfig) -> None:
    _files, total = _scratch_usage(config.root)
    if total > config.max_bytes:
        raise ReaderV2Failure("reader_v2_scratch_size_limit")
    try:
        free = shutil.disk_usage(config.root).free
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    if free < config.min_free_bytes:
        raise ReaderV2Failure("reader_v2_scratch_free_limit")


def _owned_scratch_directory(path: Path) -> bool:
    if not path.name.startswith(READER_V2_SCRATCH_BUILD_PREFIX):
        return False
    try:
        metadata = path.lstat()
        marker = path / READER_V2_SCRATCH_OWNER_NAME
        marker_metadata = marker.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or not stat.S_ISREG(
            marker_metadata.st_mode
        ):
            return False
        descriptor = os.open(
            marker,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            payload = os.read(descriptor, len(READER_V2_SCRATCH_OWNER_BYTES) + 1)
            if os.read(descriptor, 1):
                return False
        finally:
            os.close(descriptor)
        return payload == READER_V2_SCRATCH_OWNER_BYTES
    except OSError:
        return False


def cleanup_reader_v2_scratch(
    *,
    remove_all: bool = False,
    now: float | None = None,
    config: ReaderV2ScratchConfig | None = None,
) -> int:
    """Remove only marked Reader v2 build directories from the scratch root."""

    if type(remove_all) is not bool:
        raise ReaderV2Failure("reader_v2_scratch_config_invalid")
    resolved = config or reader_v2_scratch_config()
    timestamp = time.time() if now is None else now
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ReaderV2Failure("reader_v2_scratch_config_invalid")
    removed = 0
    try:
        entries = tuple(resolved.root.iterdir())
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    for entry in entries:
        if not _owned_scratch_directory(entry):
            continue
        try:
            modified = entry.lstat().st_mtime
        except OSError:
            continue
        if not remove_all and timestamp - modified < resolved.stale_seconds:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            raise ReaderV2Failure("reader_v2_scratch_cleanup_failed") from exc
        removed += 1
    return removed


def _write_scratch_owner_marker(root: Path) -> None:
    directory = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        descriptor = os.open(
            READER_V2_SCRATCH_OWNER_NAME,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        view = memoryview(READER_V2_SCRATCH_OWNER_BYTES)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReaderV2Failure("reader_v2_scratch_unavailable")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(directory)
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


@contextmanager
def _scratch_build_directory() -> Iterator[tuple[Path, ReaderV2ScratchConfig]]:
    config = reader_v2_scratch_config()
    cleanup_reader_v2_scratch(config=config)
    _enforce_scratch_budget(config)
    try:
        build = Path(tempfile.mkdtemp(prefix=READER_V2_SCRATCH_BUILD_PREFIX, dir=config.root))
        _write_scratch_owner_marker(build)
        output = build / "output"
        temporary = build / "tmp"
        output.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
    except (OSError, ReaderV2Failure) as exc:
        if "build" in locals() and build.exists():
            shutil.rmtree(build, ignore_errors=True)
        if isinstance(exc, ReaderV2Failure):
            raise
        raise ReaderV2Failure("reader_v2_scratch_unavailable") from exc
    previous_environment = {
        name: os.environ.get(name) for name in ("TMPDIR", "TMP", "TEMP")
    }
    previous_tempdir = tempfile.tempdir
    try:
        for name in previous_environment:
            os.environ[name] = str(temporary)
        tempfile.tempdir = str(temporary)
        yield output, config
    finally:
        tempfile.tempdir = previous_tempdir
        for name, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        if _owned_scratch_directory(build):
            shutil.rmtree(build, ignore_errors=True)


def build_pmc_article(
    paper: dict[str, Any],
    output_dir: Path,
    fetch: Fetch = _fetch,
) -> dict[str, Any] | None:
    doi = str(paper.get("doi") or "").strip()
    if not doi:
        return None
    metadata = discover_article(doi, fetch)
    if metadata is None:
        return None
    provenance = {
        "source": "pmc_jats",
        "pmcid": metadata["resolved_pmcid"],
        "version": metadata["resolved_version"],
        "license": metadata.get("license_code"),
        "is_retracted": bool(metadata.get("is_retracted")),
        "xml_url": metadata["xml_url"],
    }
    jats_payload = fetch_jats(metadata, fetch)
    article = parse_jats(jats_payload, provenance)
    available = media_urls(metadata)
    assets_dir = output_dir / "assets"
    if len(article["figures"]) + 1 > MAX_FILES:
        raise PMCFailure("pmc_artifact_file_limit")
    downloaded_bytes = len(jats_payload)
    for figure in article["figures"]:
        name = figure.get("asset_name")
        url = available.get(name) if isinstance(name, str) else None
        if not url or Path(name).suffix.casefold() not in DISPLAY_IMAGE_SUFFIXES:
            figure["asset_path"] = None
            if f"figure_asset_missing:{figure['id']}" not in article["warnings"]:
                article["warnings"].append(f"figure_asset_missing:{figure['id']}")
            continue
        destination, md5 = download_media(url, assets_dir, name, fetch)
        try:
            downloaded_bytes += destination.lstat().st_size
        except OSError as exc:
            raise PMCFailure("pmc_asset_invalid") from exc
        if downloaded_bytes > MAX_TOTAL_BYTES:
            raise PMCFailure("pmc_artifact_size_limit")
        figure["asset_path"] = f"assets/{destination.name}"
        figure["asset_md5"] = md5
    if provenance["is_retracted"]:
        article["warnings"].append("article_retracted")
    return validate_article(article)


def write_article(article: dict[str, Any], output_dir: Path) -> Path:
    validate_article(article)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "article.json"
    temporary = output_dir / "article.json.tmp"
    payload = (
        json.dumps(
            article,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o640)
    temporary.replace(destination)
    return destination


def raw_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ReaderV2Failure("reader_v2_raw_unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except ReaderV2Failure:
        raise
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_raw_unsafe") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReaderV2Failure("reader_v2_raw_changed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def run_docling(pdf_path: Path, scratch_dir: Path) -> dict[str, Any]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise ReaderV2Failure("docling_unavailable") from exc
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_page_images = False
    options.generate_picture_images = True
    options.images_scale = 1.5
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    try:
        result = converter.convert(pdf_path)
        if not str(result.status).endswith("SUCCESS"):
            raise ReaderV2Failure("docling_conversion_incomplete")
        path = scratch_dir / "docling.json"
        result.document.save_as_json(path)
        try:
            payload = _read_regular_bytes(
                path,
                maximum=READER_V2_DOCLING_JSON_MAX_BYTES,
            )
            return json.loads(payload.decode("utf-8"))
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    except ReaderV2Failure:
        raise
    except Exception as exc:
        raise ReaderV2Failure("docling_conversion_failed") from exc


def _read_regular_bytes(path: Path, *, maximum: int = MAX_FILE_BYTES) -> bytes:
    descriptor = -1
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ReaderV2Failure("reader_v2_artifact_unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, maximum + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum:
                raise ReaderV2Failure("reader_v2_artifact_too_large")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReaderV2Failure("reader_v2_artifact_changed")
        return bytes(payload)
    except ReaderV2Failure:
        raise
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_artifact_unsafe") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _article_from_resolution(resolution: ArtifactResolution) -> dict[str, Any]:
    try:
        payload = _read_regular_bytes(
            resolution.root / "article.json",
            maximum=READER_V2_ARTICLE_MAX_BYTES,
        )
        article = json.loads(payload.decode("utf-8"))
        return validate_article(article)
    except ReaderV2Failure:
        raise
    except (ArticleValidationError, UnicodeError, ValueError, RecursionError) as exc:
        raise ReaderV2Failure("reader_v2_invalid") from exc


def _resolve_optional_current(
    data_dir: Path,
    paper_id: int,
) -> ArtifactResolution | None:
    try:
        return resolve_current_artifact(
            data_dir,
            paper_id,
            allow_legacy_v2=True,
        )
    except ArtifactVersionError as exc:
        if str(exc) == "current_artifact_missing":
            return None
        raise ReaderV2Failure("reader_v2_current_invalid") from exc


def _verify_raw_source(
    data_dir: Path,
    paper_id: int,
    raw_relative: str,
    source_sha256: str,
    *,
    changed: bool,
) -> None:
    try:
        verify_raw_source_sha256(
            data_dir,
            paper_id,
            raw_relative,
            source_sha256,
        )
    except ArtifactVersionError as exc:
        code = str(exc)
        if code == "raw_source_missing":
            safe_code = "reader_v2_raw_missing"
        elif code == "raw_source_sha256_mismatch":
            safe_code = (
                "reader_v2_raw_changed" if changed else "reader_v2_raw_hash_mismatch"
            )
        elif code in {
            "artifact_path_invalid",
            "artifact_path_reserved",
            "source_sha256_invalid",
        }:
            safe_code = "reader_v2_raw_path_invalid"
        else:
            safe_code = "reader_v2_raw_unsafe"
        raise ReaderV2Failure(safe_code) from exc


def _collect_artifact_paths(
    article: dict[str, Any],
    output_dir: Path,
) -> tuple[str, ...]:
    files: set[str] = {"article.json"}
    total_size = 0
    try:
        article_metadata = (output_dir / "article.json").lstat()
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_artifact_unsafe") from exc
    if (
        not stat.S_ISREG(article_metadata.st_mode)
        or article_metadata.st_size > MAX_FILE_BYTES
    ):
        raise ReaderV2Failure("reader_v2_artifact_unsafe")
    total_size += article_metadata.st_size
    assets_dir = output_dir / "assets"
    try:
        assets_metadata = assets_dir.lstat()
    except FileNotFoundError:
        assets_metadata = None
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_artifact_unsafe") from exc
    if assets_metadata is not None:
        if not stat.S_ISDIR(assets_metadata.st_mode):
            raise ReaderV2Failure("reader_v2_artifact_unsafe")
        for current, directory_names, file_names in os.walk(
            assets_dir,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in directory_names:
                try:
                    child = (current_path / name).lstat()
                except OSError as exc:
                    raise ReaderV2Failure("reader_v2_artifact_unsafe") from exc
                if not stat.S_ISDIR(child.st_mode):
                    raise ReaderV2Failure("reader_v2_artifact_unsafe")
            for name in file_names:
                path = current_path / name
                try:
                    relative = safe_relative_artifact_path(
                        path.relative_to(output_dir).as_posix()
                    )
                except (ArtifactVersionError, ValueError) as exc:
                    raise ReaderV2Failure("reader_v2_artifact_path_invalid") from exc
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ReaderV2Failure("reader_v2_artifact_unsafe") from exc
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise ReaderV2Failure("reader_v2_artifact_unsafe")
                files.add(relative)
                total_size += metadata.st_size
                if len(files) > MAX_FILES:
                    raise ReaderV2Failure("reader_v2_artifact_file_limit")
                if total_size > MAX_TOTAL_BYTES:
                    raise ReaderV2Failure("reader_v2_artifact_size_limit")
    if total_size > MAX_TOTAL_BYTES:
        raise ReaderV2Failure("reader_v2_artifact_size_limit")

    for figure in article["figures"]:
        raw_path = figure.get("asset_path")
        if raw_path is None:
            continue
        try:
            path = safe_relative_artifact_path(raw_path)
        except ArtifactVersionError as exc:
            raise ReaderV2Failure("reader_v2_artifact_path_invalid") from exc
        if not path.startswith("assets/") or path not in files:
            raise ReaderV2Failure("reader_v2_figure_asset_missing")
    return tuple(sorted(files))


def _extractor_version(article: dict[str, Any]) -> tuple[str, str]:
    provenance = article.get("provenance")
    source = provenance.get("source") if isinstance(provenance, dict) else None
    version = READER_V2_EXTRACTOR_VERSIONS.get(source)
    if version is None:
        raise ReaderV2Failure("reader_v2_source_invalid")
    return str(source), version


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _created_at_for_build(current: ArtifactResolution | None) -> str:
    now = _utc_now()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ReaderV2Failure("reader_v2_clock_invalid")
    value = now.astimezone(timezone.utc).replace(microsecond=0)
    if current is not None and current.descriptor is not None:
        try:
            previous = datetime.strptime(
                str(current.descriptor["created_at"]),
                "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReaderV2Failure("reader_v2_current_invalid") from exc
        if value <= previous:
            value = previous + timedelta(seconds=1)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _conversion_inputs(
    paper: dict[str, Any],
    data_dir: Path,
) -> tuple[int, str, str, Path, str]:
    try:
        paper_id = int(paper["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReaderV2Failure("reader_v2_paper_id_invalid") from exc
    if paper_id < 1 or isinstance(paper.get("id"), bool):
        raise ReaderV2Failure("reader_v2_paper_id_invalid")
    if not isinstance(data_dir, Path) or not data_dir.is_absolute():
        raise ReaderV2Failure("reader_v2_data_dir_invalid")
    raw_relative = str(paper.get("stored_path") or "")
    source_sha256 = str(paper.get("sha256") or "")
    _verify_raw_source(
        data_dir,
        paper_id,
        raw_relative,
        source_sha256,
        changed=False,
    )
    raw_path = data_dir / "library" / "raw" / Path(raw_relative)
    relative_manifest = f"{paper_id}/v2/article.json"
    return paper_id, raw_relative, source_sha256, raw_path, relative_manifest


def _current_version_id(current: ArtifactResolution | None) -> str | None:
    if current is None or current.mode == "legacy_v2":
        return None
    if current.mode != "versioned" or current.version_id is None:
        raise ReaderV2Failure("reader_v2_current_invalid")
    return current.version_id


def _recover_owned_unpublished_stage(
    paper: dict[str, Any],
    data_dir: Path,
    *,
    paper_id: int,
    source_sha256: str,
    current: ArtifactResolution | None,
) -> PreparedPaperV2Version | None:
    """Reuse only an exact, owned, verified stage newer than current.

    Unknown directories are never removed.  A version-shaped but invalid tree
    fails closed instead of being mistaken for converter output.
    """

    paths = artifact_store_paths(data_dir, paper_id)
    try:
        metadata = paths.staging_root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_prepared_invalid") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReaderV2Failure("reader_v2_prepared_invalid")
    previous_created_at: str | None = None
    if current is not None and current.descriptor is not None:
        previous_created_at = str(current.descriptor.get("created_at") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", previous_created_at):
            raise ReaderV2Failure("reader_v2_current_invalid")
    candidates: list[tuple[str, str, str]] = []
    try:
        entries = list(paths.staging_root.iterdir())
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_prepared_invalid") from exc
    for entry in entries:
        if not re.fullmatch(r"rv-[0-9a-f]{32}", entry.name):
            continue
        try:
            descriptor = verify_version_directory(entry)
        except (ArtifactVersionError, OSError) as exc:
            raise ReaderV2Failure("reader_v2_prepared_invalid") from exc
        if (
            descriptor["paper_id"] != paper_id
            or descriptor["source_sha256"] != source_sha256
            or descriptor["render_version"] != READER_V2_RENDER_VERSION
            or descriptor["extractor_version"]
            not in set(READER_V2_EXTRACTOR_VERSIONS.values())
        ):
            continue
        created_at = str(descriptor["created_at"])
        if previous_created_at is not None and created_at <= previous_created_at:
            continue
        candidates.append(
            (
                created_at,
                str(descriptor["version_id"]),
                str(descriptor["extractor_version"]),
            )
        )
    if not candidates:
        return None
    _created_at, version_id, extractor_version = max(candidates)
    recovered = resume_prepared_paper_v2_version(
        paper,
        data_dir,
        version_id,
        expected_current_version_id=_current_version_id(current),
    )
    if READER_V2_EXTRACTOR_VERSIONS.get(recovered.source) != extractor_version:
        raise ReaderV2Failure("reader_v2_prepared_invalid")
    return recovered


def prepare_paper_v2_version(
    paper: dict[str, Any],
    data_dir: Path,
    *,
    force_reprocess: bool = False,
    failure_injector: Callable[[str], None] | None = None,
    pmc_article_builder: PMCArticleBuilder | None = None,
    pdf_article_converter: PDFArticleConverter | None = None,
) -> PreparedPaperV2Version:
    """Build and verify an immutable version without changing the pointer."""

    if type(force_reprocess) is not bool:
        raise ReaderV2Failure("reader_v2_force_invalid")
    if failure_injector is not None and not callable(failure_injector):
        raise ReaderV2Failure("reader_v2_failure_injector_invalid")
    if pmc_article_builder is not None and not callable(pmc_article_builder):
        raise ReaderV2Failure("reader_v2_converter_callback_invalid")
    if (
        pdf_article_converter is not None
        and not callable(pdf_article_converter)
    ):
        raise ReaderV2Failure("reader_v2_converter_callback_invalid")
    selected_pmc_builder = pmc_article_builder or build_pmc_article
    (
        paper_id,
        raw_relative,
        source_sha256,
        raw_path,
        relative_manifest,
    ) = _conversion_inputs(paper, data_dir)
    current = _resolve_optional_current(data_dir, paper_id)
    if (
        not force_reprocess
        and current is not None
        and current.mode == "versioned"
        and current.descriptor is not None
        and current.descriptor["source_sha256"] == source_sha256
    ):
        article = _article_from_resolution(current)
        _verify_raw_source(
            data_dir,
            paper_id,
            raw_relative,
            source_sha256,
            changed=False,
        )
        source, _version = _extractor_version(article)
        return PreparedPaperV2Version(
            data_dir=data_dir,
            paper_id=paper_id,
            logical_manifest_path=relative_manifest,
            source=source,
            source_sha256=source_sha256,
            version_id=str(current.version_id),
            expected_current_version_id=str(current.version_id),
            staged=None,
            reused_current=True,
        )

    recovered = _recover_owned_unpublished_stage(
        paper,
        data_dir,
        paper_id=paper_id,
        source_sha256=source_sha256,
        current=current,
    )
    if recovered is not None:
        return recovered

    with _scratch_build_directory() as (output_dir, scratch_config):
        article = None
        try:
            article = selected_pmc_builder(paper, output_dir)
        except (PMCFailure, JATSFailure, ArticleValidationError):
            shutil.rmtree(output_dir / "assets", ignore_errors=True)
        _enforce_scratch_budget(scratch_config)
        if article is None:
            if pdf_article_converter is None:
                docling = run_docling(raw_path, output_dir)
                _enforce_scratch_budget(scratch_config)
                try:
                    article = adapt_docling(docling, raw_path, output_dir, paper)
                except DoclingAdapterFailure as exc:
                    raise ReaderV2Failure(exc.code) from exc
                _enforce_scratch_budget(scratch_config)
            else:
                source = pdf_article_converter(paper, data_dir, output_dir)
                if source != "docling_pdf":
                    raise ReaderV2Failure("reader_v2_converter_source_invalid")
                try:
                    payload = _read_regular_bytes(
                        output_dir / "article.json",
                        maximum=READER_V2_ARTICLE_MAX_BYTES,
                    )
                    article = json.loads(payload.decode("utf-8"))
                except ReaderV2Failure:
                    raise
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ReaderV2Failure(
                        "reader_v2_converter_article_invalid"
                    ) from exc
                _enforce_scratch_budget(scratch_config)
        article = validate_article(article)
        source, extractor_version = _extractor_version(article)
        write_article(article, output_dir)
        files = _collect_artifact_paths(article, output_dir)
        _enforce_scratch_budget(scratch_config)
        _verify_raw_source(
            data_dir,
            paper_id,
            raw_relative,
            source_sha256,
            changed=True,
        )
        try:
            descriptor = build_version_descriptor_from_directory(
                paper_id=paper_id,
                source_sha256=source_sha256,
                article_fingerprint=build_selector_index(article).article_fingerprint,
                extractor_version=extractor_version,
                render_version=READER_V2_RENDER_VERSION,
                created_at=_created_at_for_build(current),
                source_root=output_dir,
                files=files,
            )
            staged = create_staged_version_from_directory(
                data_dir,
                descriptor,
                source_root=output_dir,
                files=files,
                raw_source_relative=raw_relative,
                failure_injector=failure_injector,
            )
        except ArtifactVersionError as exc:
            if str(exc) == "staging_version_exists":
                return resume_prepared_paper_v2_version(
                    paper,
                    data_dir,
                    str(descriptor["version_id"]),
                    expected_current_version_id=_current_version_id(current),
                )
            if str(exc) == "raw_source_sha256_mismatch":
                raise ReaderV2Failure("reader_v2_raw_changed") from exc
            raise ReaderV2Failure("reader_v2_artifact_publish_failed") from exc
        except Exception as exc:
            raise ReaderV2Failure("reader_v2_artifact_publish_failed") from exc
    return PreparedPaperV2Version(
        data_dir=data_dir,
        paper_id=paper_id,
        logical_manifest_path=relative_manifest,
        source=source,
        source_sha256=source_sha256,
        version_id=str(staged.version_id),
        expected_current_version_id=_current_version_id(current),
        staged=staged,
        reused_current=False,
    )


def resume_prepared_paper_v2_version(
    paper: dict[str, Any],
    data_dir: Path,
    version_id: str,
    *,
    expected_current_version_id: Any = _EXPECTED_CURRENT_UNSET,
) -> PreparedPaperV2Version:
    """Recover an already planned staged/installed version after a crash."""

    (
        paper_id,
        raw_relative,
        source_sha256,
        _raw_path,
        relative_manifest,
    ) = _conversion_inputs(paper, data_dir)
    if expected_current_version_id is _EXPECTED_CURRENT_UNSET:
        expected_current_version_id = _current_version_id(
            _resolve_optional_current(data_dir, paper_id)
        )
    elif expected_current_version_id is not None and (
        not isinstance(expected_current_version_id, str)
        or not re.fullmatch(r"rv-[0-9a-f]{32}", expected_current_version_id)
    ):
        raise ReaderV2Failure("reader_v2_current_invalid")
    try:
        paths = artifact_store_paths(data_dir, paper_id)
        final = version_directory(paths, version_id)
        stage = paths.staging_root / version_id
        try:
            stage.lstat()
            artifact_root = stage
            descriptor = verify_version_directory(stage)
        except FileNotFoundError:
            try:
                final.lstat()
            except FileNotFoundError as exc:
                raise ReaderV2Failure("reader_v2_prepared_missing") from exc
            artifact_root = final
            descriptor = verify_version_directory(final)
        if (
            descriptor["paper_id"] != paper_id
            or descriptor["version_id"] != version_id
            or descriptor["source_sha256"] != source_sha256
        ):
            raise ReaderV2Failure("reader_v2_prepared_mismatch")
        article = _article_from_resolution(
            ArtifactResolution(
                mode="versioned",
                paper_id=paper_id,
                root=artifact_root,
                version_id=version_id,
                descriptor=descriptor,
            )
        )
        source, _extractor = _extractor_version(article)
        if artifact_root == final:
            staged = create_staged_version_from_directory(
                data_dir,
                descriptor,
                source_root=final,
                files=tuple(descriptor["files"]),
                raw_source_relative=raw_relative,
            )
        else:
            staged = StagedArtifactVersion(
                data_dir=data_dir,
                paper_id=paper_id,
                version_id=version_id,
                path=stage,
                descriptor=descriptor,
                raw_source_relative=raw_relative,
            )
    except ReaderV2Failure:
        raise
    except ArtifactVersionError as exc:
        raise ReaderV2Failure("reader_v2_prepared_invalid") from exc
    except OSError as exc:
        raise ReaderV2Failure("reader_v2_prepared_invalid") from exc
    return PreparedPaperV2Version(
        data_dir=data_dir,
        paper_id=paper_id,
        logical_manifest_path=relative_manifest,
        source=source,
        source_sha256=source_sha256,
        version_id=version_id,
        expected_current_version_id=expected_current_version_id,
        staged=staged,
        reused_current=False,
    )


def activate_prepared_paper_v2_version(
    prepared: PreparedPaperV2Version,
    *,
    failure_injector: Callable[[str], None] | None = None,
) -> Mapping[str, Any] | None:
    """Publish a prepared version; a failed publish preserves the old pointer."""

    if not isinstance(prepared, PreparedPaperV2Version):
        raise ReaderV2Failure("reader_v2_prepared_invalid")
    if failure_injector is not None and not callable(failure_injector):
        raise ReaderV2Failure("reader_v2_failure_injector_invalid")
    if prepared.reused_current:
        current = _resolve_optional_current(prepared.data_dir, prepared.paper_id)
        if (
            prepared.staged is not None
            or current is None
            or current.mode != "versioned"
            or current.version_id != prepared.version_id
            or current.descriptor is None
            or current.descriptor["source_sha256"] != prepared.source_sha256
        ):
            raise ReaderV2Failure("reader_v2_current_changed")
        return current.descriptor
    if (
        prepared.staged is None
        or prepared.staged.data_dir != prepared.data_dir
        or prepared.staged.paper_id != prepared.paper_id
        or prepared.staged.version_id != prepared.version_id
    ):
        raise ReaderV2Failure("reader_v2_prepared_invalid")
    try:
        return activate_staged_version(
            prepared.staged,
            failure_injector=failure_injector,
            expected_current_version_id=prepared.expected_current_version_id,
        )
    except ArtifactVersionError as exc:
        if str(exc) == "raw_source_sha256_mismatch":
            raise ReaderV2Failure("reader_v2_raw_changed") from exc
        if str(exc) == "activation_current_version_mismatch":
            raise ReaderV2Failure("reader_v2_current_changed") from exc
        raise ReaderV2Failure("reader_v2_artifact_publish_failed") from exc
    except Exception as exc:
        raise ReaderV2Failure("reader_v2_artifact_publish_failed") from exc


def convert_paper_v2(
    paper: dict[str, Any],
    data_dir: Path,
    *,
    force_reprocess: bool = False,
    failure_injector: Callable[[str], None] | None = None,
    pmc_article_builder: PMCArticleBuilder | None = None,
    pdf_article_converter: PDFArticleConverter | None = None,
) -> tuple[str, str]:
    prepared = prepare_paper_v2_version(
        paper,
        data_dir,
        force_reprocess=force_reprocess,
        failure_injector=failure_injector,
        pmc_article_builder=pmc_article_builder,
        pdf_article_converter=pdf_article_converter,
    )
    activate_prepared_paper_v2_version(
        prepared,
        failure_injector=failure_injector,
    )
    return prepared.logical_manifest_path, prepared.source


def read_article(data_dir: Path, paper_id: int) -> dict[str, Any]:
    try:
        resolution = resolve_current_artifact(
            data_dir,
            paper_id,
            allow_legacy_v2=True,
        )
    except ArtifactVersionError as exc:
        if str(exc) == "current_artifact_missing":
            raise FileNotFoundError("reader_v2_missing") from exc
        raise ValueError("reader_v2_invalid") from exc
    try:
        return _article_from_resolution(resolution)
    except ReaderV2Failure as exc:
        raise ValueError("reader_v2_invalid") from exc


def v2_asset_path(data_dir: Path, paper_id: int, asset_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", asset_name):
        raise FileNotFoundError("reader_v2_asset_invalid")
    try:
        resolution = resolve_current_artifact(
            data_dir,
            paper_id,
            allow_legacy_v2=True,
        )
        assets = resolution.root / "assets"
        if not stat.S_ISDIR(assets.lstat().st_mode):
            raise FileNotFoundError("reader_v2_asset_missing")
        path = assets / asset_name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise FileNotFoundError("reader_v2_asset_missing")
        return path
    except FileNotFoundError:
        raise
    except (ArtifactVersionError, OSError) as exc:
        raise FileNotFoundError("reader_v2_asset_missing") from exc


def _authors(value: Any) -> str:
    if not isinstance(value, list):
        return "Unknown authors"
    names = [_clean(author) for author in value if _clean(author)]
    return ", ".join(names) or "Unknown authors"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _section_list(
    sections: list[dict[str, Any]], parent_path: tuple[int, ...] = ()
) -> list[tuple[dict[str, Any], tuple[int, ...]]]:
    output: list[tuple[dict[str, Any], tuple[int, ...]]] = []
    for index, section in enumerate(sections):
        path = parent_path + (index,)
        output.append((section, path))
        output.extend(_section_list(section["sections"], path))
    return output


def _selector_attributes(selector: NodeSelector) -> str:
    page = f' data-page="{selector.page}"' if selector.page is not None else ""
    return (
        f'id="node-{html.escape(selector.node_id)}" '
        f'data-node-id="{html.escape(selector.node_id)}" '
        f'data-section-id="{html.escape(selector.section_id)}" '
        f'data-kind="{html.escape(selector.kind)}"{page}'
    )


def _render_node(
    node: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
    paper_id: int,
    selector: NodeSelector,
) -> str:
    kind = node["kind"]
    attributes = _selector_attributes(selector)
    if kind == "paragraph":
        return f'<p {attributes}>{html.escape(_clean(node.get("text")))}</p>'
    if kind == "list":
        return f'<div class="article-list" {attributes}>{html.escape(_clean(node.get("text")))}</div>'
    if kind == "equation":
        return f'<pre class="equation" {attributes}>{html.escape(_clean(node.get("text")))}</pre>'
    if kind == "figure":
        figure = figures.get(str(node.get("figure_id")))
        if not figure:
            return ""
        caption = html.escape(_clean(figure.get("caption") or figure.get("label")))
        page = figure.get("page")
        source = (
            f'<a class="source-page" href="/read/{paper_id}/source.pdf#page={int(page)}" target="_blank" rel="noopener">원본 {int(page)}쪽</a>'
            if isinstance(page, int) and page > 0
            else ""
        )
        asset = figure.get("asset_path")
        image = ""
        if isinstance(asset, str) and re.fullmatch(r"assets/[A-Za-z0-9._-]{1,180}", asset):
            name = html.escape(asset.split("/", 1)[1])
            image = f'<button class="figure-open" type="button" aria-label="그림 확대"><img src="/read/{paper_id}/v2/assets/{name}" loading="lazy" alt="{html.escape(_clean(figure.get("label") or "논문 그림"))}"></button>'
        return f'<figure {attributes}>{image}<figcaption>{caption} {source}</figcaption></figure>'
    if kind == "table":
        table = tables.get(str(node.get("table_id")))
        if not table:
            return ""
        rows = []
        for index, row in enumerate(table.get("rows", [])):
            tag = "th" if index == 0 else "td"
            rows.append("<tr>" + "".join(f"<{tag}>{html.escape(_clean(cell))}</{tag}>" for cell in row) + "</tr>")
        caption = html.escape(_clean(table.get("caption") or table.get("label")))
        return f'<figure class="table-card" {attributes}><div class="table-scroll"><table>{"".join(rows)}</table></div><figcaption>{caption}</figcaption></figure>'
    return ""


def _render_section(
    section: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
    paper_id: int,
    selector_index: SelectorIndex,
    section_path: tuple[int, ...],
) -> str:
    level = min(4, int(section["level"]) + 1)
    section_selector = selector_index.section(section_path)
    content = "".join(
        _render_node(
            node,
            figures,
            tables,
            paper_id,
            selector_index.node(section_path, content_index),
        )
        for content_index, node in enumerate(section["content"])
    )
    children = "".join(
        _render_section(
            child,
            figures,
            tables,
            paper_id,
            selector_index,
            section_path + (child_index,),
        )
        for child_index, child in enumerate(section["sections"])
    )
    section_id = html.escape(section_selector.section_id)
    return f'<section class="article-section" id="section-{section_id}" data-section-id="{section_id}" data-section="{html.escape(_clean(section["title"]))}"><h{level}>{html.escape(_clean(section["title"]))}</h{level}>{content}{children}</section>'


def render_reader_v2(paper: dict[str, Any], article: dict[str, Any]) -> str:
    validate_article(article)
    selector_index = build_selector_index(article)
    paper_id = int(paper["id"])
    identity = article["identity"]
    title = _clean(identity.get("title") or paper.get("title") or "Untitled Paper")
    figures = {str(item["id"]): item for item in article["figures"]}
    tables = {str(item["id"]): item for item in article["tables"]}
    abstract = "".join(
        f'<p {_selector_attributes(selector_index.abstract_node(index))}>{html.escape(_clean(node.get("text")))}</p>'
        for index, node in enumerate(article.get("abstract", []))
    )
    flat_sections = [
        (section, selector_index.section(path))
        for section, path in _section_list(article["sections"])
    ]
    toc = "".join(
        f'<li class="level-{min(3, int(section["level"]))}"><a href="#section-{html.escape(selector.section_id)}">{html.escape(_clean(section["title"]))}</a></li>'
        for section, selector in flat_sections
    )
    body = "".join(
        _render_section(section, figures, tables, paper_id, selector_index, (index,))
        for index, section in enumerate(article["sections"])
    )
    references = "".join(
        f'<li>{html.escape(_clean(reference.get("text")))}</li>'
        for reference in article["references"]
    )
    reference_panel = (
        f'<details class="secondary"><summary>참고문헌 <span>{len(article["references"])}</span></summary><ol class="references">{references}</ol></details>'
        if references
        else ""
    )
    meta = " · ".join(
        html.escape(_clean(value))
        for value in (identity.get("year"), paper.get("category"), paper.get("paper_type"))
        if value
    )
    doi = identity.get("doi")
    doi_link = (
        f'<a href="https://doi.org/{html.escape(_clean(doi))}" target="_blank" rel="noopener">DOI</a>'
        if doi
        else ""
    )
    source_label = "PMC JATS" if article["provenance"]["source"] == "pmc_jats" else "PDF structure"
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#f6f4ee;--surface:#fffdf8;--text:#20211f;--muted:#696d68;--accent:#146d65;--line:#d9d5ca;--measure:42rem;--scale:1}}@media(prefers-color-scheme:dark){{:root{{--bg:#151817;--surface:#202422;--text:#eeeae0;--muted:#a8aea9;--accent:#78d1c3;--line:#343a37}}}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font-family:ui-serif,Georgia,"Times New Roman",serif}}body.theme-light{{--bg:#f6f4ee;--surface:#fffdf8;--text:#20211f;--muted:#696d68;--accent:#146d65;--line:#d9d5ca}}body.theme-dark{{--bg:#151817;--surface:#202422;--text:#eeeae0;--muted:#a8aea9;--accent:#78d1c3;--line:#343a37}}a{{color:var(--accent)}}[data-node-id],.article-section{{scroll-margin-top:4rem}}.measure{{width:min(calc(100% - 2rem),var(--measure));margin:auto}}.top{{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}}.progress-track{{height:3px;background:var(--line)}}#progress{{height:100%;width:0;background:var(--accent)}}.toolbar{{min-height:3rem;display:flex;align-items:center;gap:.3rem;font:600 .7rem/1 ui-sans-serif,system-ui,sans-serif;overflow-x:auto}}.toolbar a,.toolbar button{{padding:.42rem .52rem;border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);text-decoration:none;font:inherit;white-space:nowrap}}#current{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);flex:1;min-width:3rem;text-align:center}}.hero{{padding:2.5rem 0 1.6rem;border-bottom:1px solid var(--line)}}.eyebrow{{color:var(--accent);font:700 .72rem/1.3 ui-sans-serif,system-ui;letter-spacing:.08em;text-transform:uppercase}}h1{{font-size:clamp(2rem,9vw,3.5rem);line-height:1.04;letter-spacing:-.035em;margin:.55rem 0 1rem}}.meta{{color:var(--muted);font:.92rem/1.55 ui-sans-serif,system-ui,sans-serif}}.start{{display:inline-block;margin-top:1.2rem;padding:.75rem 1rem;border-radius:999px;background:var(--accent);color:var(--bg);text-decoration:none;font:700 .9rem/1 ui-sans-serif,system-ui,sans-serif}}.abstract{{padding:1.5rem 0;border-bottom:1px solid var(--line)}}.abstract h2{{font:700 .8rem/1 ui-sans-serif,system-ui;color:var(--accent);letter-spacing:.06em;text-transform:uppercase}}.reader{{padding:1.5rem 0 18vh;font-size:calc(1.12rem * var(--scale));line-height:1.78}}.reader p{{margin:0 0 1.25em;text-wrap:pretty}}.article-section h2,.article-section h3,.article-section h4{{line-height:1.22;scroll-margin-top:4rem;color:var(--accent);margin:2.5em 0 .8em}}.article-section h2{{font-size:1.55em}}.article-section h3{{font-size:1.25em}}.toc,.secondary{{border:1px solid var(--line);background:var(--surface);border-radius:.9rem;padding:.8rem 1rem;margin:0 0 1.5rem;font:400 .9rem/1.5 ui-sans-serif,system-ui,sans-serif}}summary{{cursor:pointer;font-weight:700}}.toc ol{{padding-left:1.35rem}}.toc li{{margin:.5rem 0}}.toc .level-2{{margin-left:.8rem}}figure{{margin:2rem 0;padding:.65rem;background:var(--surface);border:1px solid var(--line);border-radius:1rem}}.figure-open{{display:block;width:100%;padding:0;border:0;background:none;cursor:zoom-in}}figure img{{display:block;width:100%;height:auto;border-radius:.55rem}}figcaption{{padding:.75rem .3rem .15rem;color:var(--muted);font:.86rem/1.55 ui-sans-serif,system-ui,sans-serif}}.source-page{{white-space:nowrap}}.table-scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font:.82rem/1.4 ui-sans-serif,system-ui,sans-serif}}th,td{{border:1px solid var(--line);padding:.5rem;text-align:left;vertical-align:top}}.references{{padding-left:1.4rem}}.references li{{margin:.65rem 0;color:var(--muted)}}mark{{background:#ffe36b;color:inherit;border-radius:.12em}}mark.remapped{{outline:2px dotted #d07b00}}.annotation-pin{{border:0;border-radius:999px;margin:-.8rem 0 1rem .35rem;padding:.2rem .45rem;background:var(--accent);color:var(--bg);font-weight:800}}.annotation-pin.remapped{{outline:2px dotted #d07b00}}dialog{{max-width:min(96vw,70rem);max-height:94vh;padding:.5rem;border:0;border-radius:1rem;background:var(--surface);color:var(--text)}}dialog::backdrop{{background:#000b}}dialog img{{display:block;max-width:100%;max-height:88vh}}#zoom button{{position:absolute;right:1rem;top:1rem;border:0;border-radius:999px;background:#111c;color:white;width:2.2rem;height:2.2rem}}.state-panel,.interaction-panel{{width:min(94vw,30rem);padding:1.1rem;font:500 .9rem/1.4 ui-sans-serif,system-ui,sans-serif}}.state-panel h2,.interaction-panel h2{{margin:.1rem 0 1rem}}.state-panel label,.interaction-panel label{{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.65rem 0;border-bottom:1px solid var(--line)}}.state-panel select,.interaction-panel select,.interaction-panel input,.interaction-panel textarea{{min-width:9rem;max-width:65%;padding:.45rem;border:1px solid var(--line);border-radius:.5rem;background:var(--bg);color:var(--text)}}.interaction-panel textarea{{min-height:5rem;resize:vertical}}.panel-actions{{display:flex;gap:.5rem;margin-top:1rem}}.panel-actions button,.state-panel .close{{position:static;width:auto;height:auto;flex:1;padding:.65rem;border:0;border-radius:.6rem;background:var(--accent);color:var(--bg);font-weight:700}}.panel-actions .secondary-action{{background:var(--line);color:var(--text)}}#syncState,#annotationMessage,#issueMessage{{color:var(--muted);font-size:.78rem}}#annotationPreview,blockquote{{margin:.7rem 0;padding:.6rem .75rem;border-left:3px solid var(--accent);background:var(--bg);color:var(--muted)}}.annotation-card,.issue-card{{padding:.8rem 0;border-top:1px solid var(--line)}}.annotation-card p,.issue-card p{{margin:.4rem 0}}.card-actions{{display:flex;gap:.4rem}}.card-actions button{{padding:.35rem .55rem;border:1px solid var(--line);border-radius:.5rem;background:var(--bg);color:var(--text)}}#selectionBar{{position:fixed;z-index:30;left:50%;bottom:max(1rem,env(safe-area-inset-bottom));transform:translateX(-50%);padding:.7rem 1rem;border:0;border-radius:999px;background:var(--accent);color:var(--bg);font:700 .85rem/1 ui-sans-serif,system-ui,sans-serif;box-shadow:0 .4rem 1.5rem #0005}}@media(min-width:48rem){{.measure{{width:min(calc(100% - 4rem),var(--measure))}}}}
</style></head><body><header class="top"><div class="progress-track"><div id="progress"></div></div><div class="measure toolbar"><a href="/library">← 목록</a><button id="tocButton" aria-label="목차 열기">목차</button><span id="current">시작</span><button id="stateButton" aria-label="읽기 상태">상태</button><button id="annotationButton" aria-label="메모와 하이라이트">메모</button><button id="issueButton" aria-label="변환 문제 신고">신고</button><a href="/knowledge/{paper_id}">지식</a><button id="size" aria-label="글자 크기">가</button><button id="theme" aria-label="테마">◐</button><a href="/read/{paper_id}/source.pdf" target="_blank">PDF</a></div></header>
<section class="hero"><div class="measure"><div class="eyebrow">Reader v2 · {source_label}</div><h1>{html.escape(title)}</h1><div class="meta">{html.escape(_authors(identity.get("authors")))}<br>{meta} {doi_link} · <a href="/read/{paper_id}">기존 리더</a></div><a class="start" href="#{'section-' + html.escape(flat_sections[0][1].section_id) if flat_sections else 'article'}" id="start">읽기 시작</a></div></section>
{f'<section class="abstract" id="section-{html.escape(selector_index.abstract_section_id)}" data-section-id="{html.escape(selector_index.abstract_section_id)}"><div class="measure"><h2>Abstract</h2>{abstract}</div></section>' if abstract else ''}<main class="measure reader" id="article" data-paper-id="{paper_id}" data-selector-version="{selector_index.selector_version}" data-article-fingerprint="{html.escape(selector_index.article_fingerprint)}"><details class="toc" id="toc"><summary>논문 구조</summary><ol>{toc}</ol></details>{body}{reference_panel}</main><dialog id="zoom"><button type="button" aria-label="닫기">×</button><img alt="확대된 논문 그림"></dialog>
<dialog id="statePanel" class="state-panel"><form method="dialog"><h2>읽기 상태</h2><label>상태<select id="readingStatus"><option value="unread">안 읽음</option><option value="reading">읽는 중</option><option value="completed">완료</option><option value="on_hold">보류</option><option value="rereading">다시 읽기</option></select></label><label>즐겨찾기<input id="favorite" type="checkbox"></label><label>우선순위<select id="priority"><option value="0">없음</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option><option value="5">5</option></select></label><p id="syncState">불러오는 중…</p><button class="close" value="close">닫기</button></form></dialog>
<dialog id="annotationPanel" class="interaction-panel"><h2>하이라이트 · 메모 · 질문</h2><form id="annotationForm"><blockquote id="annotationPreview">본문을 먼저 선택하세요.</blockquote><label>종류<select name="kind"><option value="highlight">하이라이트</option><option value="note">메모</option><option value="question">질문</option></select></label><label>분류<input name="category" maxlength="80" placeholder="예: 핵심 결과"></label><label>내용<textarea name="body" maxlength="20000" placeholder="선택 사항"></textarea></label><label>AI 답변에 사용<input name="ai_share_allowed" type="checkbox"></label><small>기본값은 꺼짐입니다. 켜야만 이 메모가 DeepSeek 질문 답변에 전송될 수 있습니다.</small><div class="panel-actions"><button type="submit">저장</button><button type="button" class="secondary-action" data-close-dialog>닫기</button></div></form><p id="annotationMessage">문구를 선택하면 정확한 위치와 함께 저장됩니다.</p><section id="annotationList"></section></dialog>
<dialog id="issuePanel" class="interaction-panel"><h2>변환 문제 신고</h2><form id="issueForm"><label>종류<select name="kind"><option value="extraction_text">불필요하거나 빠진 글</option><option value="sentence_break">끊긴 문장</option><option value="section_structure">잘못된 구조</option><option value="figure_crop">잘못된 그림</option><option value="table">표 문제</option><option value="metadata">논문 정보</option><option value="other">기타</option></select></label><label>설명<textarea name="description" maxlength="4000" placeholder="어떤 점이 불편한지 적어 주세요"></textarea></label><div class="panel-actions"><button type="submit">현재 위치와 저장</button><button type="button" class="secondary-action" data-close-dialog>닫기</button></div></form><p id="issueMessage">현재 읽고 있는 위치가 자동으로 함께 저장됩니다.</p><section id="issueList"></section></dialog><button id="selectionBar" type="button" hidden>선택한 문구 저장</button><script src="/static/reader-v2.js" defer></script></body></html>'''
