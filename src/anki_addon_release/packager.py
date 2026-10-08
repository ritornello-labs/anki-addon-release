from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
import zipfile

from .config import ReleaseConfig
from .errors import PackageError
from .publication_receipt import prepare


@dataclass(frozen=True)
class PackagePlan:
    source_dir: Path
    artifact_path: Path
    files: tuple[Path, ...]


@dataclass(frozen=True)
class ArchiveEntry:
    filename: str
    file_size: int


def build_plan(config: ReleaseConfig) -> PackagePlan:
    if config.target != "addon":
        raise PackageError("package planning is only available for add-on targets")
    if config.source_dir is None or config.manifest is None:
        raise PackageError("add-on targets require source_dir and manifest")

    source_dir = config.source_dir.resolve()
    manifest = config.manifest.resolve()
    artifact_path = config.artifact_path.resolve()

    if not source_dir.exists() or not source_dir.is_dir():
        raise PackageError(f"source_dir is not a directory: {source_dir}")
    if not manifest.exists():
        raise PackageError(f"manifest not found: {manifest}")
    if not _is_relative_to(manifest, source_dir):
        raise PackageError("manifest must be inside source_dir")

    files = _included_files(config)
    if manifest not in files:
        raise PackageError("manifest is not included in package plan")
    if not files:
        raise PackageError("package plan contains no files")

    return PackagePlan(
        source_dir=source_dir,
        artifact_path=artifact_path,
        files=tuple(sorted(files, key=lambda path: path.relative_to(source_dir).as_posix())),
    )


def write_package(plan: PackagePlan) -> Path:
    plan.artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_relative_to(plan.artifact_path.resolve(), plan.source_dir.resolve()):
        planned_names = {path.relative_to(plan.source_dir).as_posix() for path in plan.files}
        artifact_name = plan.artifact_path.resolve().relative_to(plan.source_dir.resolve()).as_posix()
        if artifact_name in planned_names:
            raise PackageError("artifact path would be included in its own archive")

    receipt = plan.artifact_path.with_suffix(plan.artifact_path.suffix + ".publication.json")
    receipt.unlink(missing_ok=True)

    if plan.artifact_path.exists():
        plan.artifact_path.unlink()

    with zipfile.ZipFile(plan.artifact_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in plan.files:
            arcname = path.relative_to(plan.source_dir).as_posix()
            archive.write(path, arcname)

    with zipfile.ZipFile(plan.artifact_path) as archive:
        bad_file = archive.testzip()
    if bad_file is not None:
        raise PackageError(f"zip integrity check failed for {bad_file}")

    try:
        result = prepare(plan.artifact_path, set())
    except Exception:
        plan.artifact_path.unlink(missing_ok=True)
        raise PackageError("Publication check rejected the package; run anki-publication-check locally for details") from None
    import json
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return plan.artifact_path


def inspect_archive(path: Path) -> tuple[ArchiveEntry, ...]:
    if not path.exists():
        raise PackageError(f"archive not found: {path}")
    with zipfile.ZipFile(path) as archive:
        entries = [
            ArchiveEntry(filename=info.filename, file_size=info.file_size)
            for info in archive.infolist()
            if not info.is_dir()
        ]
    return tuple(entries)


def _included_files(config: ReleaseConfig) -> set[Path]:
    if config.source_dir is None:
        raise PackageError("add-on targets require source_dir")
    if config.include:
        files: set[Path] = set()
        for pattern in config.include:
            matches = list(config.source_dir.glob(pattern))
            if not matches:
                raise PackageError(f"include pattern matched no files: {pattern}")
            for match in matches:
                if match.is_file() and not _excluded(match, config):
                    files.add(match.resolve())
                elif match.is_dir():
                    for path in match.rglob("*"):
                        if path.is_file() and not _excluded(path, config):
                            files.add(path.resolve())
        return files

    files = set()
    for path in config.source_dir.rglob("*"):
        if path.is_file() and not _excluded(path, config):
            files.add(path.resolve())
    return files


def _excluded(path: Path, config: ReleaseConfig) -> bool:
    relative = path.resolve().relative_to(config.source_dir.resolve()).as_posix()
    parts = relative.split("/")
    for pattern in config.exclude:
        if pattern in parts:
            return True
        if fnmatch(relative, pattern):
            return True
        if any(fnmatch(part, pattern) for part in parts):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
