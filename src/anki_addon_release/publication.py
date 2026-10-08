#!/usr/bin/env python3
"""Offline publication gate; quiet by default, --details for local use only.

Known signatures are checked, not arbitrary personal information. Git modes read
objects rather than working files. Unsupported binary/package formats fail closed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import gzip
import os
import tarfile
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
PRIVATE_NAMES = {
    "cards-info.json",
    "cards-scheduling.json",
    "identity.json",
    "notes-info.json",
    "original-cards-info.json",
    "original-notes-info.json",
    "state-compact.json",
}
SECRET_PATTERNS = (
    rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b",
    rb"\bAKIA[0-9A-Z]{16}\b",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"\bsk_live_[A-Za-z0-9]{20,}\b",
)
HOME_PATH = re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/")
MEDIA = {
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".gif": b"GIF8",
    ".woff": b"wOFF",
    ".woff2": b"wOF2",
    ".webp": b"RIFF",
}


def requires_visual_review(path: str, data: bytes) -> bool:
    return PurePosixPath(path).suffix.lower() in {*MEDIA, ".mp4"} or data[4:8] == b"ftyp"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def private_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        name in PRIVATE_NAMES
        or name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or "live-imports" in parts
        or "live-moves" in parts
        or name.endswith((".colpkg", ".anki2", ".anki21", ".anki21b"))
    )


def live_structure(value) -> bool:
    if isinstance(value, dict):
        keys = {str(k).lower() for k in value}
        if (
            ("noteid" in keys and "fields" in keys)
            or ("cardid" in keys and keys & {"reps", "lapses", "interval", "due"})
            or {"noteids", "cardids"} <= keys
            or {"reps", "lapses", "due", "queue"} <= keys
        ):
            return True
        if any(
            re.fullmatch(r"\d{13}", str(k)) and isinstance(v, dict) and {"deck", "mod"} <= set(v)
            for k, v in value.items()
        ):
            return True
        return any(live_structure(v) for v in value.values())
    if isinstance(value, list):
        return any(live_structure(v) for v in value)
    return False


def sqlite_reasons(data: bytes) -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "collection.db"
        db.write_bytes(data)
        with contextlib.closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"notes", "cards", "revlog", "col"} <= tables:
                return {"unsupported-collection-schema"}
            reasons = set()
            if connection.execute("SELECT count(*) FROM revlog").fetchone()[0]:
                reasons.add("review-history")
            if connection.execute(
                "SELECT count(*) FROM cards WHERE type != 0 OR queue != 0 "
                "OR reps != 0 OR lapses != 0 OR ivl != 0 OR factor != 0 "
                "OR odue != 0 OR odid != 0"
            ).fetchone()[0]:
                reasons.add("study-state")
            for flds, tags in connection.execute("SELECT flds,tags FROM notes"):
                reasons.update(content_reasons("note.txt", (flds + tags).encode()))
            for models, decks, conf in connection.execute("SELECT models,decks,conf FROM col"):
                for text in (models, decks, conf):
                    reasons.update(content_reasons("metadata.json", text.encode()))
            return reasons


def archive_reasons(path: str, data: bytes, depth: int) -> set[str]:
    if depth > 3:
        return {"archive-depth-limit"}
    reasons = set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if (sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES or len(entries) > 20000
                or any(e.file_size > MAX_BYTES for e in entries)):
            return {"archive-size-limit"}
        media_names = {}
        if PurePosixPath(path).suffix.lower() == ".apkg":
            names = {e.filename for e in entries}
            if (
                not names & {"collection.anki2", "collection.anki21"}
                or "collection.anki21b" in names
            ):
                return {"unsupported-apkg-format"}
            media_names = json.loads(archive.read("media"))
            if not isinstance(media_names, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in media_names.items()
            ):
                return {"invalid-media-manifest"}
        if len({e.filename for e in entries}) != len(entries):
            reasons.add("duplicate-archive-entry")
        for entry in entries:
            if entry.is_dir():
                continue
            name = entry.filename
            if (
                name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or entry.flag_bits & 1
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                reasons.add("unsafe-archive-entry")
                continue
            payload = archive.read(entry)
            if (
                name in {"collection.anki2", "collection.anki21"}
                and PurePosixPath(path).suffix.lower() == ".apkg"
            ):
                if not payload.startswith(b"SQLite format 3\0"):
                    reasons.add("unsupported-collection-format")
                else:
                    reasons.update(sqlite_reasons(payload))
            else:
                reasons.update(content_reasons(media_names.get(name, name), payload, depth + 1))
    return reasons


def compressed_reasons(path: str, data: bytes, depth: int) -> set[str]:
    if depth > 3:
        return {"archive-depth-limit"}
    if path.lower().endswith((".tgz", ".tar.gz", ".tar")):
        reasons, names, total = set(), set(), 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for index, entry in enumerate(archive):
                total += entry.size
                if index >= 20000 or total > MAX_ARCHIVE_BYTES or entry.size > MAX_BYTES:
                    return reasons | {"archive-size-limit"}
                if entry.name in names:
                    reasons.add("duplicate-archive-entry")
                names.add(entry.name)
                if entry.isdir():
                    continue
                if (not entry.isfile() or entry.name.startswith("/")
                        or ".." in PurePosixPath(entry.name).parts):
                    reasons.add("unsafe-archive-entry")
                    continue
                stream = archive.extractfile(entry)
                if stream is None:
                    return reasons | {"uninspectable-archive"}
                with stream:
                    reasons.update(content_reasons(entry.name, stream.read(MAX_BYTES + 1), depth + 1))
        return reasons
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        return {"archive-size-limit"}
    return content_reasons(str(PurePosixPath(path).with_suffix("")), payload, depth + 1)



def mp4_reasons(data: bytes) -> set[str]:
    """Inspect bounded, nonfragmented MP4 boxes; unknown extensions fail closed.

    Media samples still require human visual review. Metadata and padding are
    inspected separately so a valid container is not a blanket binary exemption.
    Box layout: https://developer.apple.com/documentation/quicktime-file-format/atoms
    """
    containers = {b"moov", b"trak", b"mdia", b"minf", b"dinf", b"stbl", b"edts",
                  b"udta", b"meta", b"ilst", b"\xa9too"}
    leaves = {b"mvhd", b"tkhd", b"elst", b"mdhd", b"hdlr", b"vmhd", b"smhd",
              b"dref", b"stsd", b"stts", b"stss", b"ctts", b"stsc", b"stsz",
              b"stco", b"co64"}
    brands = {b"isom", b"iso2", b"avc1", b"mp41", b"mp42", b"M4V "}
    reasons = set()
    count = 0

    def boxes(start, end, depth=0):
        nonlocal count
        if depth > 16:
            raise ValueError("box nesting limit")
        types = []
        while start < end:
            count += 1
            if count > 100000 or end - start < 8:
                raise ValueError("invalid box header")
            size = int.from_bytes(data[start:start + 4], "big")
            kind = data[start + 4:start + 8]
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError("truncated extended size")
                size = int.from_bytes(data[start + 8:start + 16], "big")
                header = 16
            elif size == 0:
                if depth != 0 or kind != b"mdat":
                    raise ValueError("unsupported zero size")
                size = end - start
            if size < header or size > end - start:
                raise ValueError("invalid box size")
            payload_start, stop = start + header, start + size
            types.append(kind)
            if depth == 0 and kind not in {b"ftyp", b"moov", b"mdat", b"free"}:
                reasons.add("unsupported-mp4-box")
            elif kind == b"ftyp" and depth == 0:
                payload = data[payload_start:stop]
                if len(payload) < 8 or len(payload) % 4 or payload[:4] not in brands:
                    raise ValueError("invalid file type")
                if any(payload[i:i + 4] not in brands for i in range(8, len(payload), 4)):
                    reasons.add("unsupported-mp4-brand")
            elif kind == b"mdat" and depth == 0:
                if stop == payload_start:
                    raise ValueError("empty media samples")
            elif kind == b"free":
                payload = data[payload_start:stop]
                if payload.strip(b"\0"):
                    reasons.update(content_reasons("padding.txt", payload))
            elif kind in containers:
                if kind == b"meta":
                    if data[payload_start:payload_start + 4] != b"\0" * 4:
                        raise ValueError("unsupported metadata version")
                    payload_start += 4
                children = boxes(payload_start, stop, depth + 1)
                if kind == b"moov" and not {b"mvhd", b"trak"} <= set(children):
                    raise ValueError("incomplete movie metadata")
            elif kind == b"data":
                # Apple-style text metadata: type indicator and locale precede UTF-8.
                if stop - payload_start < 8 or data[payload_start:payload_start + 4] != b"\0\0\0\1":
                    reasons.add("unsupported-mp4-metadata")
                else:
                    reasons.update(content_reasons("metadata.txt", data[payload_start + 8:stop]))
            elif kind not in leaves:
                reasons.add("unsupported-mp4-box")
            start = stop
        return types

    try:
        top = boxes(0, len(data))
        if not top or top[0] != b"ftyp" or any(top.count(k) != 1 for k in (b"ftyp", b"moov", b"mdat")):
            raise ValueError("incomplete movie")
    except (ValueError, RecursionError):
        reasons.add("invalid-media-format")
    return reasons


def content_reasons(path: str, data: bytes, depth: int = 0) -> set[str]:
    reasons = {"private-artifact-path"} if private_path(path) else set()
    if len(data) > MAX_BYTES:
        return reasons | {"file-size-limit"}
    if any(re.search(pattern, data) for pattern in SECRET_PATTERNS):
        reasons.add("credential-signature")
    if HOME_PATH.search(data):
        reasons.add("personal-home-path")
    if data.startswith(b"version https://git-lfs.github.com/spec/"):
        reasons.add("uninspected-lfs-object")
    suffix = PurePosixPath(path).suffix.lower()
    if data.startswith(b"\x1f\x8b") or suffix in {".gz", ".tgz", ".tar"}:
        try:
            return reasons | compressed_reasons(path, data, depth)
        except Exception:
            return reasons | {"uninspectable-archive"}
    if data.startswith(b"PK\x03\x04") or suffix in {".zip", ".apkg", ".ankiaddon"}:
        try:
            return reasons | archive_reasons(path, data, depth)
        except Exception:
            return reasons | {"uninspectable-archive"}
    if suffix == ".mp4" or data[4:8] == b"ftyp":
        return reasons | mp4_reasons(data)
    # Inspect recognizable image bytes even when an immutable public URL has
    # a historical image-extension mismatch; font/unknown binaries remain blocked.
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        for image_suffix in (".png", ".jpg", ".gif", ".webp"):
            if data.startswith(MEDIA[image_suffix]):
                if image_suffix == ".webp" and data[8:12] != b"WEBP":
                    return reasons | {"invalid-media-format"}
                return reasons
    if suffix in MEDIA and data.startswith(MEDIA[suffix]):
        if suffix == ".webp" and data[8:12] != b"WEBP":
            return reasons | {"invalid-media-format"}
        return reasons
    try:
        text = data.decode("utf-8")
        if "\0" in text:
            return reasons | {"unsupported-binary"}
    except UnicodeDecodeError:
        return reasons | {"unsupported-binary"}
    if suffix == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            if live_structure(json.loads(text)):
                reasons.add("live-collection-structure")
        except (ValueError, RecursionError):
            if suffix == ".json":
                reasons.add("invalid-json")
    return reasons


def entries(ref: str | None):
    raw = git("ls-files", "--stage", "-z") if ref is None else git("ls-tree", "-r", "-z", ref)
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, kind_or_oid, oid_or_stage = header.split()
        oid = kind_or_oid if ref is None else oid_or_stage
        if ref is None and oid_or_stage != b"0":
            raise RuntimeError("unmerged index")
        yield mode.decode(), oid.decode(), path.decode("utf-8", "surrogateescape")


def scan_refs(refs: list[str | None]) -> list[dict]:
    findings, seen, cache = [], set(), {}
    for ref in refs:
        for mode, oid, path in entries(ref):
            if (mode, oid, path) in seen:
                continue
            seen.add((mode, oid, path))
            if mode not in {"100644", "100755"}:
                reasons = {"symlink-or-submodule"}
            elif private_path(path):
                # Do not load potentially huge live snapshots just to establish
                # that a forbidden artifact was committed.
                reasons = {"private-artifact-path"}
            else:
                key = (oid, PurePosixPath(path).suffix.lower())
                if key not in cache:
                    size = int(git("cat-file", "-s", oid))
                    cache[key] = (
                        content_reasons(path, git("cat-file", "blob", oid))
                        if size <= MAX_BYTES
                        else {"file-size-limit"}
                    )
                reasons = cache[key]
            for reason in sorted(reasons):
                findings.append({"ref": ref or "index", "path": path, "reason": reason})
    return findings


def outgoing_refs(remote: str, updates: str) -> list[str]:
    # Use the actual destination, not stale origin tracking refs. Unknown local
    # boundary objects mean scanning more ancestry, never silently skipping it.
    advertised = git("ls-remote", "--refs", remote).decode().splitlines()
    exclusions = []
    for row in advertised:
        oid = row.split()[0]
        try:
            git("cat-file", "-e", oid + "^{commit}")
            exclusions.append("^" + oid)
        except subprocess.CalledProcessError:
            pass
    tips = []
    for row in updates.splitlines():
        _, local_oid, _, _ = row.split()
        if set(local_oid) != {"0"}:
            tips.append(git("rev-parse", local_oid + "^{commit}").decode().strip())
    if not tips:
        return []
    return git("rev-list", *tips, *exclusions).decode().splitlines() + tips


def ci_refs() -> list[str]:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    name = os.environ["GITHUB_EVENT_NAME"]
    if name == "pull_request":
        before = event["pull_request"]["base"]["sha"]
    elif name == "push":
        before = event["before"]
    else:
        raise ValueError("Unsupported publication event")
    if not re.fullmatch(r"[0-9a-f]{40,64}", before):
        raise ValueError("Invalid event boundary")
    if set(before) == {"0"}:
        base = os.environ.get("PUBLICATION_BASE_REF", "refs/remotes/origin/main")
        if not re.fullmatch(r"refs/remotes/origin/[A-Za-z0-9_./-]+", base):
            raise ValueError("Invalid default branch")
        before = git("merge-base", "HEAD", base).decode().strip()
    return ["HEAD", *git("rev-list", before + "..HEAD").decode().splitlines()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ci", action="store_true")
    group.add_argument("--staged", action="store_true")
    group.add_argument("--history", action="store_true")
    group.add_argument("--pre-push", metavar="REMOTE")
    group.add_argument("--artifact", action="append", type=Path)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--private-report", type=Path)
    parser.add_argument("--reviewed-media-sha256", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.artifact:
            findings = []
            for path in args.artifact:
                data = path.read_bytes()
                reasons = content_reasons(path.name, data)
                if (
                    requires_visual_review(path.name, data)
                    and hashlib.sha256(data).hexdigest() not in args.reviewed_media_sha256
                ):
                    reasons.add("visual-review-required")
                findings.extend({"path": str(path), "reason": r} for r in sorted(reasons))
        else:
            if args.ci:
                refs = ci_refs()
            elif args.pre_push:
                refs = outgoing_refs(args.pre_push, sys.stdin.read())
            elif args.staged:
                refs = [None]
            elif args.history:
                refs = git("rev-list", args.ref).decode().splitlines()
            else:
                refs = [args.ref]
            findings = scan_refs(refs)
        if args.private_report:
            from .private_artifacts import validate_private_path

            report = validate_private_path(args.private_report)
            report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            report.write_text(json.dumps({"findings": findings}, indent=2) + "\n")
            report.chmod(0o600)
        if args.details:
            for finding in findings:
                print(json.dumps(finding, ensure_ascii=True), file=sys.stderr)
        if findings:
            print(
                "Publication check failed. Run the local checker with --details.", file=sys.stderr
            )
            return 1
        print("Publication check passed.")
        return 0
    except Exception:
        print("Publication check could not complete; publication is blocked.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
