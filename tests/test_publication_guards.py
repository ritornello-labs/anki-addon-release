"""Security regressions use invented data and disposable Git repositories only."""

from __future__ import annotations

import contextlib
import hashlib
import gzip
import tarfile
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from anki_addon_release import publication as guard
from anki_addon_release.publication_receipt import prepare
from anki_addon_release.private_artifacts import private_directory, validate_private_path


@contextlib.contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout.decode().strip()


def commit(message):
    # Disposable fixture objects, not project commits or published history.
    tree = run("write-tree")
    parent = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], capture_output=True)
    parents = ["-p", parent.stdout.decode().strip()] if parent.returncode == 0 else []
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    oid = (
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit-tree", tree, *parents],
            input=message.encode(),
            check=True,
            capture_output=True,
            env=env,
        )
        .stdout.decode()
        .strip()
    )
    run("update-ref", "HEAD", oid)
    return oid


def package(directory, scheduled=False, reviewed=False, field="Country"):
    db = directory / "db"
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(
            "CREATE TABLE notes(flds TEXT,tags TEXT); "
            "CREATE TABLE revlog(id INTEGER); "
            "CREATE TABLE col(models TEXT,decks TEXT,conf TEXT); "
            "CREATE TABLE cards(type INT,queue INT,reps INT,lapses INT,ivl INT,"
            "factor INT,odue INT,odid INT);"
        )
        conn.execute("INSERT INTO notes VALUES (?,?)", (field, ""))
        conn.execute("INSERT INTO col VALUES (?,?,?)", ("{}", "{}", "{}"))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)",
            (2, 2, 1, 0, 3, 2500, 0, 0) if scheduled else (0, 0, 0, 0, 0, 0, 0, 0),
        )
        if reviewed:
            conn.execute("INSERT INTO revlog VALUES (1)")
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as z:
        z.writestr("collection.anki2", db.read_bytes())
        z.writestr("media", "{}")
    db.unlink()
    return result.getvalue()


class ContentTests(unittest.TestCase):
    def test_safe_public_json(self):
        self.assertEqual(
            guard.content_reasons("counts.json", b'{"notes": 12, "verified": true}'), set()
        )

    def test_private_path(self):
        self.assertIn(
            "private-artifact-path", guard.content_reasons("backups/live-moves/x.txt", b"safe")
        )

    def test_force_added_env(self):
        self.assertIn("private-artifact-path", guard.content_reasons(".env.production", b"x=y"))
        self.assertEqual(
            guard.content_reasons(".env.example", b"API_KEY=op://Vault/Item/field"), set()
        )

    def test_renamed_note_snapshot(self):
        for name in ("export.json", "export.txt"):
            self.assertIn(
                "live-collection-structure",
                guard.content_reasons(
                    name,
                    json.dumps(
                        [{"noteId": 1700000000001, "fields": {"answer": "synthetic"}}]
                    ).encode(),
                ),
            )

    def test_renamed_scheduling(self):
        self.assertTrue(guard.live_structure({"queue": 2, "due": 1, "reps": 5, "lapses": 1}))
        self.assertTrue(guard.live_structure({"1700000000001": {"deck": "synthetic", "mod": 123}}))

    def test_credentials(self):
        self.assertIn("credential-signature", guard.content_reasons("x.txt", b"ghp_" + b"X" * 36))

    def test_personal_path(self):
        self.assertIn(
            "personal-home-path", guard.content_reasons("x.txt", b"/" + b"Users/fixture/private")
        )

    def test_unknown_binary(self):
        self.assertIn("unsupported-binary", guard.content_reasons("x.bin", b"\0\xff"))

    def test_lfs_pointer(self):
        self.assertIn(
            "uninspected-lfs-object",
            guard.content_reasons(
                "x.txt", b"version https://git-lfs.github.com/spec/v1\noid sha256:test"
            ),
        )

    def test_archive_private_content(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("renamed.json", '{"noteIds": [1], "cardIds": [2]}')
        self.assertIn(
            "live-collection-structure", guard.content_reasons("asset.zip", payload.getvalue())
        )

    def test_compressed_text_and_tar_are_inspected(self):
        snapshot = b'{"noteIds":[1],"cardIds":[2]}'
        self.assertIn("live-collection-structure", guard.content_reasons("source.json.gz", gzip.compress(snapshot)))
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            entry = tarfile.TarInfo("package/renamed.txt")
            entry.size = len(snapshot)
            archive.addfile(entry, io.BytesIO(snapshot))
        self.assertIn("live-collection-structure", guard.content_reasons("vendor.tgz", payload.getvalue()))

    def test_compressed_limits_and_tar_links(self):
        with patch.object(guard, "MAX_BYTES", 32):
            self.assertIn("archive-size-limit", guard.content_reasons("map.svg.gz", gzip.compress(b"x" * 33)))
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            entry = tarfile.TarInfo("link")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../private"
            archive.addfile(entry)
        self.assertIn("unsafe-archive-entry", guard.content_reasons("vendor.tgz", payload.getvalue()))

    def test_failed_receipt_removes_stale_success(self):
        from anki_addon_release.publication_receipt import main
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "bad.apkg"
            artifact.write_bytes(b"invalid")
            receipt = Path(tmp) / "receipt.json"
            receipt.write_text('{"publication_check":"passed"}')
            with self.assertRaises(SystemExit):
                main([str(artifact), "--receipt", str(receipt)])
            self.assertFalse(receipt.exists())

    def test_archive_traversal(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("../x", "hello")
        self.assertIn(
            "unsafe-archive-entry", guard.content_reasons("asset.zip", payload.getvalue())
        )

    def test_corrupt_archive(self):
        self.assertIn("uninspectable-archive", guard.content_reasons("broken.apkg", b"bad"))

    def test_apkg_new_and_studied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(guard.content_reasons("deck.apkg", package(root)), set())
            self.assertIn(
                "study-state", guard.content_reasons("deck.apkg", package(root, scheduled=True))
            )
            self.assertIn(
                "review-history", guard.content_reasons("deck.apkg", package(root, reviewed=True))
            )
            field = "/" + "Users/fixture/private"
            self.assertIn(
                "personal-home-path", guard.content_reasons("deck.apkg", package(root, field=field))
            )

    def test_unsupported_modern_apkg(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("collection.anki21b", b"unhandled")
        self.assertIn(
            "unsupported-apkg-format", guard.content_reasons("deck.apkg", payload.getvalue())
        )

    def test_release_receipt_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "deck.apkg"
            p.write_bytes(package(Path(tmp)))
            receipt = prepare(p, set())
            self.assertEqual(
                set(receipt), {"schema", "sha256", "bytes", "publication_check", "visual_review"}
            )
            self.assertEqual(receipt["sha256"], hashlib.sha256(p.read_bytes()).hexdigest())
            p.write_bytes(package(Path(tmp), reviewed=True))
            with self.assertRaises(ValueError):
                prepare(p, set())

    def test_visual_review_hash_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "screenshot.gif"
            p.write_bytes(b"GIF89a")
            with self.assertRaises(ValueError):
                prepare(p, set())
            prepare(p, {hashlib.sha256(p.read_bytes()).hexdigest()})


class MP4Tests(unittest.TestCase):
    @staticmethod
    def box(kind, payload=b"", extended=False):
        if extended:
            return b"\0\0\0\1" + kind + (len(payload) + 16).to_bytes(8, "big") + payload
        return (len(payload) + 8).to_bytes(4, "big") + kind + payload

    def movie(self, metadata=b"Synthetic encoder", extra=b"", extended=False):
        box = self.box
        text = box(b"data", b"\0\0\0\1" + b"\0" * 4 + metadata)
        meta = box(b"meta", b"\0" * 4 + box(b"ilst", box(b"\xa9too", text)))
        moov = box(b"moov", box(b"mvhd", b"\0" * 20) + box(b"trak", box(b"tkhd", b"\0" * 20)) + box(b"udta", meta))
        return box(b"ftyp", b"isom\0\0\0\0isomiso2avc1mp41") + moov + extra + box(b"mdat", b"\0\xff\0\xff", extended)

    def test_bounded_mp4_and_extended_sizes(self):
        for extended in (False, True):
            self.assertEqual(guard.content_reasons("demo.mp4", self.movie(extended=extended)), set())
            self.assertEqual(guard.content_reasons("renamed.bin", self.movie(extended=extended)), set())

    def test_text_metadata_and_padding_are_inspected(self):
        for payload, reason in ((b"ghp_" + b"X" * 36, "credential-signature"),
                                (b'{"noteIds":[1],"cardIds":[2]}', "live-collection-structure"),
                                (b"/" + b"Users/fixture/private", "personal-home-path")):
            self.assertIn(reason, guard.content_reasons("demo.mp4", self.movie(payload)))
            self.assertIn(reason, guard.content_reasons("demo.mp4", self.movie(extra=self.box(b"free", payload))))

    def test_unknown_boxes_and_binary_metadata_fail(self):
        self.assertIn("unsupported-mp4-box", guard.content_reasons("demo.mp4", self.movie(extra=self.box(b"uuid", b"payload"))))
        self.assertIn("unsupported-binary", guard.content_reasons("demo.mp4", self.movie(b"\0\xff")))

    def test_truncated_corrupt_and_missing_boxes_fail(self):
        movie = self.movie()
        for data in (b"not a movie", movie[:-1], movie + b"x", movie[:32],
                     b"\0\0\0\1ftyp", b"\0\0\0\4ftyp", movie.replace(b"isom", b"fake")):
            self.assertTrue(guard.content_reasons("demo.mp4", data))

    def test_nested_limits_fail(self):
        payload = self.box(b"mvhd")
        for _ in range(18):
            payload = self.box(b"udta", payload)
        self.assertIn("invalid-media-format", guard.content_reasons("demo.mp4", self.movie(extra=self.box(b"moov", payload))))

    def test_image_extension_mismatch_uses_recognized_bytes(self):
        self.assertEqual(guard.content_reasons("legacy.png", b"\xff\xd8\xffpublic image"), set())
        self.assertIn("credential-signature", guard.content_reasons("legacy.png", b"\xff\xd8\xffghp_" + b"X" * 36))
        self.assertIn("unsupported-binary", guard.content_reasons("unknown.png", b"\xff\0unknown"))

    def test_mp4_receipt_requires_exact_visual_review_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.mp4"
            path.write_bytes(self.movie())
            with self.assertRaises(ValueError):
                prepare(path, set())
            receipt = prepare(path, {hashlib.sha256(path.read_bytes()).hexdigest()})
            self.assertEqual(receipt["visual_review"], "approved")


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = cwd(self.root)
        self.cwd.__enter__()
        run("init", "-q")
        Path("safe.txt").write_text("safe")
        run("add", "safe.txt")
        self.base = commit("base")

    def tearDown(self):
        self.cwd.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_installed_guard_is_frozen_against_later_source_edits(self):
        from anki_addon_release import publication_hooks
        package = Path(publication_hooks.__file__).parent
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for name in ("__init__.py", "publication.py", "private_artifacts.py"):
                (source / name).write_bytes((package / name).read_bytes())
            with patch.object(publication_hooks, "__file__", str(source / "publication_hooks.py")):
                hooks = publication_hooks.install()
            (source / "publication.py").write_text("raise SystemExit(0)\n")
            Path("renamed.txt").write_text('{"noteIds":[1],"cardIds":[2]}')
            run("add", "renamed.txt")
            result = subprocess.run([str(hooks / "pre-commit")], capture_output=True)
            self.assertEqual(result.returncode, 1)

    def test_installer_preserves_existing_hook_configuration(self):
        from anki_addon_release.publication_hooks import install
        run("config", "core.hooksPath", "custom-hooks")
        with self.assertRaises(ValueError):
            install()
        self.assertEqual(run("config", "--get", "core.hooksPath"), "custom-hooks")

    def test_installer_blocks_actual_staged_snapshot(self):
        from anki_addon_release.publication_hooks import install
        hooks = install()
        Path("renamed.txt").write_text('{"noteIds":[1],"cardIds":[2]}')
        run("add", "renamed.txt")
        result = subprocess.run([str(hooks / "pre-commit")], capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_ci_scans_intermediate_deleted_snapshot_and_fails_on_missing_boundary(self):
        Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
        run("add", "renamed.json")
        commit("intermediate")
        run("rm", "renamed.json")
        commit("delete")
        event = self.root / "event.json"
        event.write_text(json.dumps({"before": self.base}))
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event)}):
            self.assertTrue(guard.scan_refs(guard.ci_refs()))
            event.write_text(json.dumps({"before": "f" * 40}))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(guard.main(["--ci"]), 2)

    def test_index_not_worktree(self):
        Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
        run("add", "renamed.json")
        Path("renamed.json").write_text("{}")
        self.assertTrue(guard.scan_refs([None]))
        run("add", "renamed.json")
        Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
        self.assertFalse(guard.scan_refs([None]))

    def test_outgoing_deleted_file_and_tag(self):
        remote = self.root.parent / (self.root.name + "-remote.git")
        try:
            run("init", "--bare", "-q", str(remote))
            run("push", str(remote), "HEAD:refs/heads/main")
            Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
            run("add", "renamed.json")
            leaked = commit("intermediate")
            run("rm", "renamed.json")
            final = commit("delete")
            self.assertFalse(guard.scan_refs([final]))
            updates = f"refs/heads/main {final} refs/heads/main {self.base}\n"
            refs = guard.outgoing_refs(str(remote), updates)
            self.assertIn(leaked, refs)
            self.assertTrue(guard.scan_refs(refs))
            run("-c", "tag.gpgsign=false", "tag", "test", leaked)
            refs = guard.outgoing_refs(
                str(remote), f"refs/tags/test {leaked} refs/tags/test " + "0" * 40
            )
            self.assertTrue(guard.scan_refs(refs))
        finally:
            import shutil

            shutil.rmtree(remote, ignore_errors=True)

    def test_installed_hook_blocks_actual_push(self):
        remote = self.root.parent / (self.root.name + "-hook-remote.git")
        try:
            run("init", "--bare", "-q", str(remote))
            run("push", str(remote), "HEAD:refs/heads/main")
            Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
            run("add", "renamed.json")
            commit("intermediate")
            run("rm", "renamed.json")
            commit("delete")
            hooks = Path(guard.__file__).resolve().parents[2] / ".githooks"
            run("config", "core.hooksPath", str(hooks))
            result = subprocess.run(
                ["git", "push", str(remote), "HEAD:refs/heads/main"], capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            remote_tip = run("ls-remote", str(remote), "refs/heads/main").split()[0]
            self.assertEqual(remote_tip, self.base)
        finally:
            import shutil

            shutil.rmtree(remote, ignore_errors=True)

    def test_precommit_hook_reads_index(self):
        Path("renamed.json").write_text('{"noteIds":[1],"cardIds":[2]}')
        run("add", "renamed.json")
        hooks = Path(guard.__file__).resolve().parents[2] / ".githooks"
        result = subprocess.run([str(hooks / "pre-commit")], capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_remote_unavailable_fails_closed(self):
        with self.assertRaises(subprocess.CalledProcessError):
            guard.outgoing_refs(
                str(self.root / "absent.git"),
                f"refs/heads/main {self.base} refs/heads/main " + "0" * 40,
            )

    def test_symlink_rejected(self):
        Path("link").symlink_to("safe.txt")
        run("add", "link")
        self.assertEqual(guard.scan_refs([None])[0]["reason"], "symlink-or-submodule")

    def test_quiet_failure_hides_paths_and_values(self):
        Path("private-location.json").write_text('{"noteIds":[123],"cardIds":[456]}')
        run("add", "private-location.json")
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertEqual(guard.main(["--staged"]), 1)
        self.assertNotIn("private-location", output.getvalue())
        self.assertNotIn("123", output.getvalue())

    def test_quiet_error_hides_traceback(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertEqual(guard.main(["--ref", "nonexistent-private-ref"]), 2)
        self.assertNotIn("nonexistent-private-ref", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_private_directory_rejects_git(self):
        with self.assertRaises(ValueError):
            validate_private_path(self.root / "new" / "backup.json")
        with patch.dict(os.environ, {"ANKI_PUBLICATION_PRIVATE_DIR": str(self.root)}):
            with self.assertRaises(ValueError):
                private_directory("live-imports")

    def test_private_directory_outside_git_and_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            with patch.dict(os.environ, {"ANKI_PUBLICATION_PRIVATE_DIR": outside}):
                target = private_directory("live-imports")
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            link = Path(outside) / "redirect"
            link.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(ValueError):
                validate_private_path(link / "snapshot.json")

    def test_missing_private_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                private_directory("live-imports")

    def test_public_report_cannot_be_written_into_git(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(guard.main(["--private-report", "audit.json"]), 2)
        self.assertFalse(Path("audit.json").exists())


if __name__ == "__main__":
    unittest.main()
