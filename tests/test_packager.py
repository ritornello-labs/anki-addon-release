from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
import zipfile

from anki_addon_release.config import ReleaseConfig
from anki_addon_release.errors import PackageError
from anki_addon_release.packager import build_plan, inspect_archive, write_package


class PackagerTests(unittest.TestCase):
    def test_builds_archive_without_top_level_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "addon"
            source.mkdir()
            (source / "__init__.py").write_text("print('hello')\n", encoding="utf-8")
            (source / "manifest.json").write_text(
                json.dumps({"package": "study_triage", "name": "Study Triage"}),
                encoding="utf-8",
            )
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")

            config = ReleaseConfig(
                project_root=root,
                source_dir=source,
                manifest=source / "manifest.json",
                artifact_dir=root / "dist",
                artifact_name="study-triage.ankiaddon",
            )

            plan = build_plan(config)
            artifact = write_package(plan)

            with zipfile.ZipFile(artifact) as archive:
                self.assertEqual(sorted(archive.namelist()), ["__init__.py", "manifest.json"])

    def test_private_payload_rejected_without_artifact_or_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text('{"name":"Fixture"}')
            (root / "renamed.txt").write_text('{"noteIds":[1],"cardIds":[2]}')
            config = ReleaseConfig(project_root=root, source_dir=root, manifest=root / "manifest.json",
                artifact_dir=root / "dist", artifact_name="fixture.ankiaddon",
                include=("manifest.json", "renamed.txt"))
            receipt = config.artifact_path.with_suffix(".ankiaddon.publication.json")
            receipt.parent.mkdir()
            receipt.write_text('{"publication_check":"passed"}')
            with self.assertRaises(PackageError):
                write_package(build_plan(config))
            self.assertFalse(config.artifact_path.exists())
            self.assertFalse(receipt.exists())

    def test_include_list_limits_packaged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__init__.py").write_text("", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps({"package": "study_triage", "name": "Study Triage"}),
                encoding="utf-8",
            )
            (root / "dev_notes.md").write_text("not packaged", encoding="utf-8")

            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                artifact_name="study-triage.ankiaddon",
                include=("__init__.py", "manifest.json"),
            )

            artifact = write_package(build_plan(config))
            entries = inspect_archive(artifact)

            self.assertEqual([entry.filename for entry in entries], ["__init__.py", "manifest.json"])


if __name__ == "__main__":
    unittest.main()

