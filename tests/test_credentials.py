from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from anki_addon_release.credentials import resolve_env_credentials, resolve_present_env_credentials
from anki_addon_release.errors import PublishError


class CredentialTests(unittest.TestCase):
    def test_no_env_names_means_manual_login(self) -> None:
        self.assertIsNone(resolve_env_credentials(email_env=None, password_env=None))

    def test_resolves_credentials_from_environment(self) -> None:
        with patch.dict(os.environ, {"ANKIWEB_EMAIL": "user@example.com", "ANKIWEB_PASSWORD": "secret"}):
            credentials = resolve_env_credentials(
                email_env="ANKIWEB_EMAIL",
                password_env="ANKIWEB_PASSWORD",
            )

        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "secret")

    @patch("anki_addon_release.credentials.subprocess.run")
    def test_resolves_one_password_references_from_environment(self, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess(["op", "read"], 0, stdout="user@example.com\n", stderr=""),
            subprocess.CompletedProcess(["op", "read"], 0, stdout="secret\n", stderr=""),
        ]

        with patch.dict(
            os.environ,
            {
                "ANKIWEB_EMAIL": "op://Vault/Item/username",
                "ANKIWEB_PASSWORD": "op://Vault/Item/password",
            },
        ):
            credentials = resolve_env_credentials(
                email_env="ANKIWEB_EMAIL",
                password_env="ANKIWEB_PASSWORD",
            )

        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "secret")
        run.assert_any_call(
            ["op", "read", "op://Vault/Item/username"],
            check=False,
            capture_output=True,
            text=True,
        )

    @patch("anki_addon_release.credentials.subprocess.run")
    def test_one_password_reference_error_does_not_include_secret_value(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["op", "read"],
            1,
            stdout="",
            stderr="item or field not found\n",
        )

        with patch.dict(
            os.environ,
            {
                "ANKIWEB_EMAIL": "op://Vault/Item/username",
                "ANKIWEB_PASSWORD": "op://Vault/Item/password",
            },
        ):
            with self.assertRaisesRegex(PublishError, "ANKIWEB_EMAIL"):
                resolve_env_credentials(
                    email_env="ANKIWEB_EMAIL",
                    password_env="ANKIWEB_PASSWORD",
                )

    def test_requires_env_names_together(self) -> None:
        with self.assertRaises(PublishError):
            resolve_env_credentials(email_env="ANKIWEB_EMAIL", password_env=None)

    def test_missing_env_value_is_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PublishError):
                resolve_env_credentials(
                    email_env="ANKIWEB_EMAIL",
                    password_env="ANKIWEB_PASSWORD",
                )

    def test_present_credentials_can_be_absent_for_existing_browser_session(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                resolve_present_env_credentials(
                    email_env="ANKIWEB_EMAIL",
                    password_env="ANKIWEB_PASSWORD",
                )
            )

    def test_present_credentials_error_when_partially_absent(self) -> None:
        with patch.dict(os.environ, {"ANKIWEB_EMAIL": "user@example.com"}, clear=True):
            with self.assertRaisesRegex(PublishError, "ANKIWEB_PASSWORD"):
                resolve_present_env_credentials(
                    email_env="ANKIWEB_EMAIL",
                    password_env="ANKIWEB_PASSWORD",
                )


if __name__ == "__main__":
    unittest.main()
