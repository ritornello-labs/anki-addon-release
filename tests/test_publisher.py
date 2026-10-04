from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch
import zipfile

from anki_addon_release.errors import ReleaseError
from anki_addon_release.publisher import (
    PublisherPaths,
    apply_publisher_prune,
    backup_publisher_collection,
    build_publisher_prune_plan,
    configure_anki_connect,
    initialize_publisher,
    launch_publisher,
    publisher_status,
    register_deck_id,
    start_sync,
)


class PublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        port_check = patch("anki_addon_release.publisher._publisher_port_in_use", return_value=False)
        port_check.start()
        self.addCleanup(port_check.stop)

    def test_prune_plan_retains_requested_roots_and_their_children(self) -> None:
        decks = {
            "Default": "1",
            "Private": "10",
            "Private::Child": "11",
            "Public": "20",
            "Public::Child": "21",
            "Public::Child::Grandchild": "22",
        }

        plan = build_publisher_prune_plan(decks, keep_deck_ids=["20"])

        self.assertEqual(plan.keep_roots, ("Public",))
        self.assertEqual(plan.retained_decks, ("Default", "Public", "Public::Child", "Public::Child::Grandchild"))
        self.assertEqual(plan.delete_stages, (("Private::Child",), ("Private",)))

    def test_prune_plan_reports_unknown_keep_ids_without_deleting(self) -> None:
        plan = build_publisher_prune_plan({"Default": "1", "Public": "2"}, keep_deck_ids=["999"])

        self.assertEqual(plan.missing_keep_deck_ids, ("999",))
        self.assertEqual(plan.delete_decks, ("Public",))

    def test_apply_prune_uses_leaf_first_anki_deck_deletion(self) -> None:
        plan = build_publisher_prune_plan(
            {"Default": "1", "Private": "10", "Private::Child": "11", "Public": "20"},
            keep_deck_ids=["20"],
        )

        with patch("anki_addon_release.publisher.anki_connect_request") as request:
            deleted = apply_publisher_prune("http://127.0.0.1:8766", plan)

        self.assertEqual(deleted, 2)
        self.assertEqual(
            request.call_args_list,
            [
                call("http://127.0.0.1:8766", "deleteDecks", decks=["Private::Child"], cardsToo=True),
                call("http://127.0.0.1:8766", "deleteDecks", decks=["Private"], cardsToo=True),
            ],
        )

    def test_backup_contains_consistent_database_media_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = PublisherPaths(base=root / "publisher", profile="Publisher")
            paths.profile_dir.mkdir(parents=True)
            with sqlite3.connect(paths.collection_path) as database:
                database.execute("create table sample (value text)")
                database.execute("insert into sample values ('ready')")
            paths.media_dir.mkdir()
            (paths.media_dir / "map.png").write_bytes(b"image")

            backup = backup_publisher_collection(paths)

            with zipfile.ZipFile(backup) as archive:
                self.assertEqual(set(archive.namelist()), {"collection.anki2", "collection.media/map.png", "publisher-backup.json"})
                self.assertEqual(archive.getinfo("collection.anki2").compress_type, zipfile.ZIP_STORED)
                manifest = archive.read("publisher-backup.json").decode("utf-8")
                self.assertIn('"media_file_count": 1', manifest)

    def test_register_deck_id_replaces_only_target_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("ANKIWEB_EMAIL=op://Vault/Item/username\nTARGET=old\n", encoding="utf-8")

            register_deck_id(env_file, variable="TARGET", deck_id="123456")

            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                "ANKIWEB_EMAIL=op://Vault/Item/username\nTARGET=123456\n",
            )

    def test_register_deck_id_rejects_invalid_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ReleaseError):
                register_deck_id(Path(temporary) / ".env", variable="123", deck_id="123456")

    def test_initialize_seeds_an_isolated_profile_and_copies_addon_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-addon"
            source.mkdir()
            (source / "__init__.py").write_text("# addon\n", encoding="utf-8")
            (source / "config.json").write_text("{}\n", encoding="utf-8")
            (source / "meta.json").write_text("{}\n", encoding="utf-8")
            (source / "user_files").mkdir()
            (source / "user_files" / "state.txt").write_text("private\n", encoding="utf-8")
            paths = PublisherPaths(base=root / "publisher", profile="Publisher")

            with patch("anki_addon_release.publisher._seed_profile") as seed:
                initialize_publisher(paths, anki_python="anki-python", anki_connect_source=source)

            seed.assert_called_once_with(paths, "anki-python")
            self.assertTrue((paths.anki_connect_dir / "__init__.py").is_file())
            self.assertFalse((paths.anki_connect_dir / "meta.json").exists())
            self.assertFalse((paths.anki_connect_dir / "user_files").exists())

    def test_configure_anki_connect_uses_a_dedicated_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.anki_connect_dir.mkdir(parents=True)
            paths.anki_connect_config.write_text('{"webBindPort": 8765}\n', encoding="utf-8")

            configure_anki_connect(paths, port=8766)

            self.assertIn('"webBindPort": 8766', paths.anki_connect_config.read_text(encoding="utf-8"))

    def test_status_accepts_current_anki_preferences_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.base.mkdir()
            (paths.base / "prefs21.db").write_bytes(b"prefs")

            self.assertEqual(publisher_status(paths)["initialized"], True)

    def test_launch_uses_an_isolated_direct_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.base.mkdir()
            (paths.base / "prefs21.db").write_bytes(b"prefs")
            paths.anki_connect_dir.mkdir(parents=True)
            paths.anki_connect_config.write_text('{"webBindPort": 8766}\n', encoding="utf-8")
            process = MagicMock(pid=42)

            with patch("anki_addon_release.publisher.default_anki_python", return_value="/anki/python"), patch(
                "anki_addon_release.publisher.subprocess.Popen", return_value=process
            ) as popen:
                returned, command = launch_publisher(paths, anki_bin="/anki/anki")

            self.assertIs(returned, process)
            self.assertEqual(command[0], "/anki/python")
            self.assertIn("publisher_run_anki.py", command[1])
            self.assertEqual(command[-4:], ["-p", "Publisher", "--lang", "en"])
            self.assertEqual(popen.call_args.kwargs["env"]["ANKI_SINGLE_INSTANCE_KEY"].startswith("anki-addon-release-publisher-"), True)
            first_key = popen.call_args.kwargs["env"]["ANKI_SINGLE_INSTANCE_KEY"]
            with patch("anki_addon_release.publisher.default_anki_python", return_value="/anki/python"), patch(
                "anki_addon_release.publisher.subprocess.Popen", return_value=process
            ) as again:
                launch_publisher(paths, anki_bin="/anki/anki")
            self.assertEqual(again.call_args.kwargs["env"]["ANKI_SINGLE_INSTANCE_KEY"], first_key)

    def test_launch_refuses_an_already_running_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary), profile="Publisher")
            (paths.base / "prefs21.db").write_bytes(b"prefs")
            with patch("anki_addon_release.publisher._publisher_port_in_use", return_value=True), patch(
                "anki_addon_release.publisher.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(ReleaseError, "already in use"):
                    launch_publisher(paths, anki_bin="/anki/anki")
            popen.assert_not_called()

    def test_launch_can_request_database_check_in_publisher_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.base.mkdir()
            (paths.base / "prefs21.db").write_bytes(b"prefs")
            paths.anki_connect_dir.mkdir(parents=True)
            paths.anki_connect_config.write_text('{"webBindPort": 8766}\n', encoding="utf-8")

            with patch("anki_addon_release.publisher.default_anki_python", return_value="/anki/python"), patch(
                "anki_addon_release.publisher.subprocess.Popen", return_value=MagicMock(pid=42)
            ) as popen:
                launch_publisher(paths, anki_bin="/anki/anki", check_database=True)

            self.assertEqual(popen.call_args.kwargs["env"]["ANKI_ADDON_RELEASE_PUBLISHER_CHECK_DATABASE"], "1")

    def test_launch_can_request_unused_media_cleanup_in_publisher_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.base.mkdir()
            (paths.base / "prefs21.db").write_bytes(b"prefs")
            paths.anki_connect_dir.mkdir(parents=True)
            paths.anki_connect_config.write_text('{"webBindPort": 8766}\n', encoding="utf-8")

            with patch("anki_addon_release.publisher.default_anki_python", return_value="/anki/python"), patch(
                "anki_addon_release.publisher.subprocess.Popen", return_value=MagicMock(pid=42)
            ) as popen:
                launch_publisher(paths, anki_bin="/anki/anki", clean_media=True)

            self.assertEqual(popen.call_args.kwargs["env"]["ANKI_ADDON_RELEASE_PUBLISHER_CLEAN_MEDIA"], "1")

    def test_launch_scrubs_original_login_environment_variables_from_anki(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            paths.base.mkdir()
            (paths.base / "prefs21.db").write_bytes(b"prefs")
            paths.anki_connect_dir.mkdir(parents=True)
            paths.anki_connect_config.write_text('{"webBindPort": 8766}\n', encoding="utf-8")

            with patch.dict("anki_addon_release.publisher.os.environ", {"ANKIWEB_EMAIL": "email", "ANKIWEB_PASSWORD": "password"}, clear=True), patch(
                "anki_addon_release.publisher.default_anki_python", return_value="/anki/python"
            ), patch("anki_addon_release.publisher.subprocess.Popen", return_value=MagicMock(pid=42)) as popen:
                launch_publisher(
                    paths,
                    anki_bin="/anki/anki",
                    login_credentials=("email", "password"),
                    login_credential_env_names=("ANKIWEB_EMAIL", "ANKIWEB_PASSWORD"),
                )

            env = popen.call_args.kwargs["env"]
            self.assertNotIn("ANKIWEB_EMAIL", env)
            self.assertNotIn("ANKIWEB_PASSWORD", env)
            self.assertEqual(env["ANKI_ADDON_RELEASE_PUBLISHER_EMAIL"], "email")
            self.assertEqual(env["ANKI_ADDON_RELEASE_PUBLISHER_PASSWORD"], "password")

    def test_sync_explains_when_anki_requires_a_one_way_sync(self) -> None:
        with patch(
            "anki_addon_release.publisher.anki_connect_request",
            side_effect=ReleaseError("AnkiConnect sync failed: Sync status 2 not one of [0, 1]"),
        ):
            with self.assertRaisesRegex(ReleaseError, "one-way sync.*Upload to AnkiWeb"):
                start_sync("http://127.0.0.1:8766")

    def test_status_reports_missing_collection_without_claiming_initialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = PublisherPaths(base=Path(temporary) / "publisher", profile="Publisher")
            self.assertEqual(
                publisher_status(paths)["initialized"],
                False,
            )


if __name__ == "__main__":
    unittest.main()
