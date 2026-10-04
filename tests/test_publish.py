from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from anki_addon_release.config import AnkiWebConfig, DeckConfig, ReleaseConfig
from anki_addon_release.errors import PublishError
from anki_addon_release.manifest import ManifestReport
from anki_addon_release.publish import (
    ankiweb_description_warnings,
    build_deck_publish_plan,
    build_publish_plan,
    default_profile_dir,
    describe_deck_publish_plan,
    describe_publish_plan,
)


class PublishPlanTests(unittest.TestCase):
    def test_auto_mode_uses_update_when_addon_id_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                artifact_name="study-triage.ankiaddon",
                ankiweb=AnkiWebConfig(addon_id="123456789", title="Study Triage"),
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "study_triage", "name": "Study Triage"},
                warnings=(),
            )

            plan = build_publish_plan(config, manifest, mode="auto", submit=False)

            self.assertEqual(plan.mode, "update")
            self.assertEqual(plan.addon_id, "123456789")
            self.assertEqual(plan.upload_url, "https://ankiweb.net/shared/upload?id=123456789")
            self.assertEqual(plan.title, "Study Triage")
            self.assertFalse(plan.submit)

    def test_auto_mode_uses_create_without_addon_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={
                    "package": "new_addon",
                    "name": "New Add-on",
                    "min_point_version": 55,
                    "max_point_version": 250902,
                },
                warnings=(),
            )

            plan = build_publish_plan(config, manifest, mode="auto", base_url="http://127.0.0.1:9999")

            self.assertEqual(plan.mode, "create")
            self.assertEqual(plan.base_url, "http://127.0.0.1:9999")
            self.assertEqual(plan.upload_url, "http://127.0.0.1:9999/shared/upload")
            self.assertEqual(plan.title, "New Add-on")
            self.assertIsNone(plan.support_url)
            self.assertEqual(plan.branch_min_version, "2.1.55")
            self.assertEqual(plan.branch_max_version, "25.09.2")

    def test_update_mode_requires_addon_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "new_addon", "name": "New Add-on"},
                warnings=(),
            )

            with self.assertRaises(PublishError):
                build_publish_plan(config, manifest, mode="update")

    def test_description_and_changelog_files_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            title = root / "TITLE.txt"
            support_url = root / "SUPPORT_URL.txt"
            description = root / "README.md"
            changelog = root / "CHANGELOG.md"
            title.write_text("File Title\n", encoding="utf-8")
            support_url.write_text("https://github.com/example/file-title\n", encoding="utf-8")
            description.write_text("# Description\n", encoding="utf-8")
            changelog.write_text("## Changes\n", encoding="utf-8")
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                ankiweb=AnkiWebConfig(
                    title_file=title,
                    support_url_file=support_url,
                    description_file=description,
                    changelog_file=changelog,
                ),
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "new_addon", "name": "New Add-on"},
                warnings=(),
            )

            plan = build_publish_plan(config, manifest)

            self.assertEqual(plan.title, "File Title")
            self.assertEqual(plan.support_url, "https://github.com/example/file-title")
            self.assertEqual(plan.description, "# Description\n")
            self.assertEqual(plan.changelog, "## Changes\n")

    def test_listing_file_front_matter_and_body_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = root / "release" / "ankiweb.md"
            listing.parent.mkdir()
            listing.write_text(
                "\n".join(
                    [
                        "---",
                        "title: File Listing Title",
                        "support_url: https://github.com/example/file-listing",
                        "---",
                        "",
                        "# Listing description",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                ankiweb=AnkiWebConfig(listing_file=listing),
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "new_addon", "name": "Manifest Name"},
                warnings=(),
            )

            plan = build_publish_plan(config, manifest)

            self.assertEqual(plan.title, "File Listing Title")
            self.assertEqual(plan.support_url, "https://github.com/example/file-listing")
            self.assertEqual(plan.description, "# Listing description\n")

    def test_describe_publish_plan_avoids_dumping_long_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                ankiweb=AnkiWebConfig(description="long description", changelog="changes"),
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "new_addon", "name": "New Add-on"},
                warnings=(),
            )

            lines = describe_publish_plan(build_publish_plan(config, manifest))

            self.assertIn("description: 16 chars", lines)
            self.assertIn("changelog: 7 chars", lines)

    def test_describe_publish_plan_warns_when_github_url_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
                ankiweb=AnkiWebConfig(
                    support_url="https://github.com/example/addon",
                    description="[GitHub](https://github.com/example/addon)\n",
                ),
            )
            manifest = ManifestReport(
                path=root / "manifest.json",
                data={"package": "new_addon", "name": "New Add-on"},
                warnings=(),
            )

            lines = describe_publish_plan(build_publish_plan(config, manifest))

            self.assertTrue(any(line.startswith("warning: ") for line in lines))

    def test_description_warning_accepts_visible_github_url(self) -> None:
        warnings = ankiweb_description_warnings(
            "https://github.com/example/addon",
            "GitHub: [https://github.com/example/addon](https://github.com/example/addon)\n",
        )

        self.assertEqual(warnings, [])

    def test_description_warning_rejects_plain_visible_github_url(self) -> None:
        warnings = ankiweb_description_warnings(
            "https://github.com/example/addon",
            "GitHub: https://github.com/example/addon\n",
        )

        self.assertEqual(len(warnings), 1)

    def test_default_profile_dir_is_project_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ReleaseConfig(
                project_root=root,
                source_dir=root,
                manifest=root / "manifest.json",
                artifact_dir=root / "dist",
            )

            self.assertEqual(
                default_profile_dir(config),
                root / ".anki-addon-release" / "browser-profile",
            )

    def test_deck_publish_plan_uses_env_private_source_and_redacts_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = root / "release" / "ankiweb.md"
            listing.parent.mkdir()
            listing.write_text(
                "\n".join(
                    [
                        "---",
                        "title: Geography Deck",
                        "tags: geography maps",
                        "support_url: https://github.com/example/geography-deck",
                        "---",
                        "",
                        "Deck description from listing.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            old = os.environ.get("ANKIWEB_SOURCE_DECK_ID")
            os.environ["ANKIWEB_SOURCE_DECK_ID"] = "1650000000000"
            try:
                config = ReleaseConfig(
                    project_root=root,
                    artifact_dir=root / "dist",
                    target="deck",
                    ankiweb=AnkiWebConfig(
                        shared_id="987654321",
                        listing_file=listing,
                    ),
                    deck=DeckConfig(source_deck_id_env="ANKIWEB_SOURCE_DECK_ID", copyright_confirmed=True),
                )

                plan = build_deck_publish_plan(config, base_url="http://127.0.0.1:9999", submit=True)
            finally:
                if old is None:
                    os.environ.pop("ANKIWEB_SOURCE_DECK_ID", None)
                else:
                    os.environ["ANKIWEB_SOURCE_DECK_ID"] = old

            self.assertEqual(plan.share_url, "http://127.0.0.1:9999/decks/share/1650000000000")
            self.assertEqual(plan.shared_id, "987654321")
            self.assertEqual(plan.title, "Geography Deck")
            self.assertEqual(plan.tags, "geography maps")
            self.assertEqual(plan.description, "Deck description from listing.\n")
            self.assertTrue(plan.copyright_confirmed)
            lines = describe_deck_publish_plan(plan)
            self.assertIn("source_deck_id: configured", lines)
            self.assertIn("share_url: http://127.0.0.1:9999/decks/share/<source-deck-id>", lines)
            self.assertNotIn("1650000000000", "\n".join(lines))

    def test_deck_publish_submit_requires_copyright_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            description = root / "README.md"
            description.write_text("# Deck\n", encoding="utf-8")
            config = ReleaseConfig(
                project_root=root,
                artifact_dir=root / "dist",
                target="deck",
                ankiweb=AnkiWebConfig(
                    shared_id="987654321",
                    title="Geography Deck",
                    description_file=description,
                ),
                deck=DeckConfig(source_deck_id="1650000000000"),
            )

            with self.assertRaisesRegex(PublishError, "copyright"):
                build_deck_publish_plan(config, submit=True)

    def test_first_deck_publish_can_discover_its_shared_id_after_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            description = root / "README.md"
            description.write_text("# Deck\n", encoding="utf-8")
            config = ReleaseConfig(
                project_root=root,
                artifact_dir=root / "dist",
                target="deck",
                ankiweb=AnkiWebConfig(title="Geography Deck", description_file=description),
                deck=DeckConfig(source_deck_id="1650000000000", copyright_confirmed=True),
            )

            plan = build_deck_publish_plan(config, submit=True)
            self.assertIsNone(plan.shared_id)
            self.assertTrue(plan.submit)

    def test_deck_publish_can_resolve_source_name_through_anki_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            description = root / "README.md"
            description.write_text("# Deck\n", encoding="utf-8")
            config = ReleaseConfig(
                project_root=root,
                artifact_dir=root / "dist",
                target="deck",
                ankiweb=AnkiWebConfig(
                    shared_id="987654321",
                    title="Geography Deck",
                    description_file=description,
                ),
                deck=DeckConfig(
                    source_deck_name="Private::Geography",
                    anki_connect_url="http://127.0.0.1:8765",
                    copyright_confirmed=True,
                ),
            )

            response = FakeAnkiConnectResponse({"result": {"Private::Geography": 1650000000000}, "error": None})
            with patch("anki_addon_release.publish.urlopen", return_value=response) as urlopen:
                plan = build_deck_publish_plan(config, submit=True)

            self.assertEqual(plan.source_deck_id, "1650000000000")
            request = urlopen.call_args.args[0]
            self.assertEqual(json.loads(request.data.decode("utf-8"))["action"], "deckNamesAndIds")


class FakeAnkiConnectResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeAnkiConnectResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
