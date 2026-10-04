from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from anki_addon_release.browser import (
    AnkiWebBrowser,
    _description_markers,
    _pause_for_review,
    playwright_available,
)
from anki_addon_release.credentials import LoginCredentials
from anki_addon_release.errors import PublishError
from anki_addon_release.publish import DeckPublishPlan, PublishPlan


RUN_BROWSER_TESTS = os.environ.get("ANKI_ADDON_RELEASE_BROWSER_TESTS") == "1"


class BrowserHelperTests(unittest.TestCase):
    def test_review_prompt_requires_interactive_terminal(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaisesRegex(PublishError, "interactive terminal"):
                _pause_for_review()

    def test_description_markers_ignore_image_markdown_and_keep_visible_links(self) -> None:
        markers = _description_markers(
            "## Geography\n\n"
            "GitHub: [https://github.com/example/deck](https://github.com/example/deck)\n\n"
            "![A rendered card](https://assets.example/card.png)"
        )

        self.assertEqual(
            markers,
            (
                "Geography",
                "GitHub: https://github.com/example/deck",
            ),
        )

    def test_description_markers_keep_heading_as_its_own_paragraph(self) -> None:
        markers = _description_markers(
            "Map cards.\n\n"
            '<img src="https://assets.example/card.png">\n\n'
            "## Edition and credit\n\n"
            "Based on an earlier deck."
        )

        self.assertEqual(
            markers,
            (
                "Map cards.",
                "Edition and credit",
                "Based on an earlier deck.",
            ),
        )


@unittest.skipUnless(
    RUN_BROWSER_TESTS and playwright_available(),
    "set ANKI_ADDON_RELEASE_BROWSER_TESTS=1 with playwright installed to run browser flows",
)
class BrowserFlowTests(unittest.TestCase):
    def test_login_flow_fills_and_submits_credentials(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).login(
                f"{server.url}/account/login",
                credentials=LoginCredentials(email="user@example.com", password="secret"),
                submit=True,
            )

            self.assertEqual(result.status, "login-submitted")
            self.assertEqual(server.last_post_path, "/account/login")
            self.assertIn(b"user%40example.com", server.last_post_body)
            self.assertIn(b"secret", server.last_post_body)

    def test_login_with_credentials_logs_out_existing_session(self) -> None:
        with FakeAnkiWebServer(login_is_logged_in=True) as server, tempfile.TemporaryDirectory() as tmp:
            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).login(
                f"{server.url}/account/login",
                credentials=LoginCredentials(email="user@example.com", password="secret"),
                submit=True,
            )

            self.assertEqual(result.status, "login-submitted")
            self.assertEqual(server.logout_get_count, 1)
            self.assertEqual(server.last_post_path, "/account/login")
            self.assertIn(b"user%40example.com", server.last_post_body)

    def test_create_flow_uploads_artifact_and_metadata(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            artifact = _artifact(Path(tmp))
            plan = PublishPlan(
                mode="create",
                base_url=server.url,
                upload_url=f"{server.url}/shared/addons/create",
                login_url=f"{server.url}/account/login",
                artifact_path=artifact,
                addon_id=None,
                title="Study Triage",
                support_url="https://github.com/ritornello-labs/study-triage",
                description="Description text",
                changelog="Initial upload",
                submit=True,
                branch_min_version="2.1.55",
                branch_max_version="25.09.2",
            )

            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).publish(plan)

            self.assertEqual(result.status, "submitted")
            self.assertEqual(server.last_post_path, "/shared/addons/create")
            self.assertIn(b"Study Triage", server.last_post_body)
            self.assertIn(
                b"https://github.com/ritornello-labs/study-triage",
                server.last_post_body,
            )
            self.assertIn(b"2.1.55", server.last_post_body)
            self.assertIn(b"25.09.2", server.last_post_body)
            self.assertIn(b"Description text", server.last_post_body)
            self.assertIn(b"Initial upload", server.last_post_body)
            self.assertIn(b"fake addon", server.last_post_body)

    def test_update_flow_uploads_artifact_and_addon_id(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            artifact = _artifact(Path(tmp))
            plan = PublishPlan(
                mode="update",
                base_url=server.url,
                upload_url=f"{server.url}/shared/addons/update",
                login_url=f"{server.url}/account/login",
                artifact_path=artifact,
                addon_id="123456789",
                title="Study Triage",
                support_url=None,
                description=None,
                changelog="Bug fixes",
                submit=True,
            )

            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).publish(plan)

            self.assertEqual(result.status, "submitted")
            self.assertEqual(server.last_post_path, "/shared/addons/update")
            self.assertEqual(server.post_paths, ["/shared/addons/update", "/shared/addons/update"])
            self.assertIn(b"fake addon", server.post_bodies[0])
            self.assertIn(b"123456789", server.last_post_body)
            self.assertIn(b"Bug fixes", server.last_post_body)

    def test_addon_submit_fails_when_ankiweb_leaves_the_upload_form_open(self) -> None:
        with FakeAnkiWebServer(keep_upload_form_after_post=True) as server, tempfile.TemporaryDirectory() as tmp:
            artifact = _artifact(Path(tmp))
            plan = PublishPlan(
                mode="create",
                base_url=server.url,
                upload_url=f"{server.url}/shared/addons/create",
                login_url=f"{server.url}/account/login",
                artifact_path=artifact,
                addon_id=None,
                title="Study Triage",
                support_url=None,
                description="Description text",
                changelog=None,
                submit=True,
            )

            with self.assertRaisesRegex(PublishError, "did not confirm"):
                AnkiWebBrowser(
                    profile_dir=Path(tmp) / "profile",
                    headless=True,
                    timeout_ms=250,
                ).publish(plan)

    def test_account_too_new_blocker_is_reported(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            artifact = _artifact(Path(tmp))
            plan = PublishPlan(
                mode="create",
                base_url=server.url,
                upload_url=f"{server.url}/shared/addons/too-new",
                login_url=f"{server.url}/account/login",
                artifact_path=artifact,
                addon_id=None,
                title="Study Triage",
                support_url=None,
                description="Description text",
                changelog=None,
                submit=False,
            )

            with self.assertRaisesRegex(PublishError, "account is too new"):
                AnkiWebBrowser(
                    profile_dir=Path(tmp) / "profile",
                    headless=True,
                    timeout_ms=10_000,
                ).publish(plan)

    def test_deck_share_flow_fills_metadata_and_copyright(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags="geography maps",
                support_url="https://github.com/example/geography-deck",
                description="Deck description",
                submit=True,
                copyright_confirmed=True,
            )

            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).publish_deck(plan)

            self.assertEqual(result.status, "submitted")
            self.assertTrue(result.final_url.startswith(f"{server.url}/shared/info/987654321?cb="))
            self.assertEqual(server.last_post_path, "/decks/share/1650000000000")
            self.assertIn(b"Geography+Deck", server.last_post_body)
            self.assertIn(b"geography+maps", server.last_post_body)
            self.assertIn(b"https%3A%2F%2Fgithub.com%2Fexample%2Fgeography-deck", server.last_post_body)
            self.assertIn(b"Deck+description", server.last_post_body)
            self.assertIn(b"confirmCopyright=on", server.last_post_body)

    def test_first_deck_share_discovers_and_verifies_new_owner_id(self) -> None:
        with FakeAnkiWebServer() as server, tempfile.TemporaryDirectory() as tmp:
            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile", headless=True, timeout_ms=10_000,
            ).publish_deck(self._first_deck_plan(server))
            self.assertEqual(result.status, "submitted")
            self.assertIn("/shared/info/987654321?cb=", result.final_url)
            self.assertGreaterEqual(server.public_listing_get_count, 2)

    def test_first_deck_share_refuses_existing_title_before_submit(self) -> None:
        with FakeAnkiWebServer(existing_deck=True) as server, tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PublishError, "already has this title"):
                AnkiWebBrowser(
                    profile_dir=Path(tmp) / "profile", headless=True, timeout_ms=10_000,
                ).publish_deck(self._first_deck_plan(server))
            self.assertEqual(server.post_paths, [])

    def test_first_deck_share_worker_without_new_listing_is_not_success(self) -> None:
        with FakeAnkiWebServer(create_deck=False) as server, tempfile.TemporaryDirectory() as tmp:
            with patch("anki_addon_release.browser._DECK_PUBLIC_LISTING_TIMEOUT_MS", 250):
                with self.assertRaisesRegex(PublishError, "no new owner listing"):
                    AnkiWebBrowser(
                        profile_dir=Path(tmp) / "profile", headless=True, timeout_ms=250,
                    ).publish_deck(self._first_deck_plan(server))

    @staticmethod
    def _first_deck_plan(server: FakeAnkiWebServer) -> DeckPublishPlan:
        return DeckPublishPlan(
            base_url=server.url, share_url=f"{server.url}/decks/share/1650000000000",
            login_url=f"{server.url}/account/login", source_deck_id="1650000000000",
            shared_id=None, title="Geography Deck", tags=None, support_url=None,
            description="Deck description", submit=True, copyright_confirmed=True,
        )

    def test_deck_share_verifies_rendered_description_and_screenshot(self) -> None:
        description = (
            "Map cards for a small geography deck.\n\n"
            "GitHub: [https://github.com/example/geography-deck](https://github.com/example/geography-deck)\n\n"
            "![Rendered card](https://assets.example/geography-card.png)"
        )
        with FakeAnkiWebServer(
            public_listing_description=(
                "Map cards for a small geography deck.\n\n"
                "GitHub: https://github.com/example/geography-deck"
            ),
            public_listing_image_sources=("https://assets.example/geography-card.png",),
        ) as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags="geography maps",
                support_url="https://github.com/example/geography-deck",
                description=description,
                submit=True,
                copyright_confirmed=True,
            )

            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).publish_deck(plan)

            self.assertEqual(result.status, "submitted")
            self.assertGreaterEqual(server.public_listing_get_count, 1)

    def test_deck_share_reports_pending_public_review_when_owner_listing_is_ready(self) -> None:
        with FakeAnkiWebServer(
            public_listing_unavailable=True,
            public_listing_unavailable_delay_ms=100,
            owner_listing_description="Fresh description",
        ) as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags=None,
                support_url=None,
                description="Fresh description",
                submit=True,
                copyright_confirmed=True,
            )

            result = AnkiWebBrowser(
                profile_dir=Path(tmp) / "profile",
                headless=True,
                timeout_ms=10_000,
            ).publish_deck(plan)

            self.assertEqual(result.status, "submitted-pending-public-review")
            self.assertTrue(result.final_url.startswith(f"{server.url}/shared/info/987654321?cb="))

    def test_deck_share_rejects_a_stale_public_listing(self) -> None:
        with FakeAnkiWebServer(public_listing_description="Previous description") as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags=None,
                support_url=None,
                description="Fresh description",
                submit=True,
                copyright_confirmed=True,
            )

            with patch("anki_addon_release.browser._DECK_PUBLIC_LISTING_TIMEOUT_MS", 250):
                with self.assertRaisesRegex(PublishError, "listing did not match"):
                    AnkiWebBrowser(
                        profile_dir=Path(tmp) / "profile",
                        headless=True,
                        timeout_ms=250,
                    ).publish_deck(plan)

    def test_deck_share_rejects_a_public_listing_missing_its_screenshot(self) -> None:
        description = "Fresh description\n\n![Rendered card](https://assets.example/geography-card.png)"
        with FakeAnkiWebServer(public_listing_description="Fresh description") as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags=None,
                support_url=None,
                description=description,
                submit=True,
                copyright_confirmed=True,
            )

            with patch("anki_addon_release.browser._DECK_PUBLIC_LISTING_TIMEOUT_MS", 250):
                with self.assertRaisesRegex(PublishError, "screenshot https://assets.example/geography-card.png"):
                    AnkiWebBrowser(
                        profile_dir=Path(tmp) / "profile",
                        headless=True,
                        timeout_ms=250,
                    ).publish_deck(plan)

    def test_deck_share_requires_an_ankiweb_confirmation_page(self) -> None:
        with FakeAnkiWebServer(keep_deck_share_form_after_post=True) as server, tempfile.TemporaryDirectory() as tmp:
            plan = DeckPublishPlan(
                base_url=server.url,
                share_url=f"{server.url}/decks/share/1650000000000",
                login_url=f"{server.url}/account/login",
                source_deck_id="1650000000000",
                shared_id="987654321",
                title="Geography Deck",
                tags="geography maps",
                support_url=None,
                description="Deck description",
                submit=True,
                copyright_confirmed=True,
            )

            with self.assertRaisesRegex(PublishError, "did not confirm the deck submission"):
                AnkiWebBrowser(
                    profile_dir=Path(tmp) / "profile",
                    headless=True,
                    timeout_ms=250,
                ).publish_deck(plan)


class FakeAnkiWebServer:
    def __init__(
        self,
        *,
        login_is_logged_in: bool = False,
        keep_upload_form_after_post: bool = False,
        keep_deck_share_form_after_post: bool = False,
        public_listing_title: str = "Geography Deck",
        public_listing_description: str = "Deck description",
        public_listing_image_sources: tuple[str, ...] = (),
        owner_listing_title: str | None = None,
        owner_listing_description: str | None = None,
        owner_listing_image_sources: tuple[str, ...] | None = None,
        public_listing_unavailable: bool = False,
        public_listing_unavailable_delay_ms: int = 0,
        existing_deck: bool = False,
        create_deck: bool = True,
    ) -> None:
        self.login_is_logged_in = login_is_logged_in
        self.keep_upload_form_after_post = keep_upload_form_after_post
        self.keep_deck_share_form_after_post = keep_deck_share_form_after_post
        self.public_listing_title = public_listing_title
        self.public_listing_description = public_listing_description
        self.public_listing_image_sources = public_listing_image_sources
        self.owner_listing_title = owner_listing_title or public_listing_title
        self.owner_listing_description = owner_listing_description or public_listing_description
        self.owner_listing_image_sources = owner_listing_image_sources or public_listing_image_sources
        self.public_listing_unavailable = public_listing_unavailable
        self.public_listing_unavailable_delay_ms = public_listing_unavailable_delay_ms
        self.existing_deck = existing_deck
        self.create_deck = create_deck

    def __enter__(self) -> FakeAnkiWebServer:
        self.last_post_path = ""
        self.last_post_body = b""
        self.post_paths = []
        self.post_bodies = []
        self.login_get_count = 0
        self.logout_get_count = 0
        self.public_listing_get_count = 0

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler) -> None:
                path = urlparse(handler.path).path
                if path == "/shared/addons/create":
                    body = _create_form()
                elif path == "/shared/addons/update":
                    body = _update_form()
                elif path == "/shared/addons/too-new":
                    body = _account_too_new_page()
                elif path == "/decks/share/1650000000000":
                    body = _deck_share_form()
                elif path == "/shared/mine":
                    rows = ""
                    if owner.existing_deck or (owner.create_deck and "/decks/share/1650000000000" in owner.post_paths):
                        rows = '<tr><td><a href="/shared/info/987654321">Info</a></td><td>Geography Deck</td></tr>'
                    body = f"<html><body><h1>Your Shared Items</h1><table>{rows}</table></body></html>".encode()
                elif path == "/shared/info/987654321":
                    owner.public_listing_get_count += 1
                    if "session=owner" in handler.headers.get("Cookie", ""):
                        body = _public_deck_listing(
                            owner.owner_listing_title,
                            owner.owner_listing_description,
                            owner.owner_listing_image_sources,
                        )
                    elif owner.public_listing_unavailable:
                        body = _delayed_unavailable_shared_item_page(
                            owner.public_listing_unavailable_delay_ms
                        )
                    else:
                        body = _public_deck_listing(
                            owner.public_listing_title,
                            owner.public_listing_description,
                            owner.public_listing_image_sources,
                        )
                elif path == "/account/login":
                    owner.login_get_count += 1
                    if owner.login_is_logged_in and owner.login_get_count == 1:
                        body = _logged_in_page()
                    else:
                        body = _login_form()
                elif path == "/account/logout":
                    owner.logout_get_count += 1
                    body = b"<html><body>Logged out</body></html>"
                else:
                    body = b"<html><body><a href='/shared/addons/create'>Upload</a></body></html>"
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Set-Cookie", "session=owner; Path=/")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def do_POST(handler) -> None:
                length = int(handler.headers.get("Content-Length", "0"))
                owner.last_post_path = handler.path
                owner.post_paths.append(handler.path)
                owner.last_post_body = handler.rfile.read(length)
                owner.post_bodies.append(owner.last_post_body)
                if owner.keep_deck_share_form_after_post and handler.path == "/decks/share/1650000000000":
                    body = _deck_share_form()
                else:
                    body = _create_form() if owner.keep_upload_form_after_post else _logged_in_page()
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _artifact(root: Path) -> Path:
    path = root / "addon.ankiaddon"
    path.write_bytes(b"fake addon")
    return path


def _login_form() -> bytes:
    return b"""
    <html><body>
      <form method="post" action="/account/login">
        <input type="email" name="email">
        <input type="password" name="password">
        <button type="submit">Log In</button>
      </form>
    </body></html>
    """


def _logged_in_page() -> bytes:
    return b"""
    <html><body>
      <main>Decks</main>
      <a href="/account/logout">Log Out</a>
    </body></html>
    """


def _create_form() -> bytes:
    return b"""
    <html><body>
      <form method="post" enctype="multipart/form-data">
        <input type="file" name="file">
        <input name="title">
        <input name="support_url">
        <input name="branch_min_version" maxlength="9">
        <input name="branch_max_version" maxlength="9">
        <textarea name="description"></textarea>
        <textarea name="changes"></textarea>
        <button type="submit" formaction="/shared/addons/upload-file">Upload file</button>
        <button type="submit">Publish</button>
      </form>
    </body></html>
    """


def _update_form() -> bytes:
    return b"""
    <html><body>
      <form method="post" enctype="multipart/form-data">
        <input type="file" name="file">
        <input name="addon_id">
        <input name="branch_min_version" maxlength="9">
        <input name="branch_max_version" maxlength="9">
        <textarea name="changelog"></textarea>
        <button type="submit" formaction="/shared/addons/upload-file">Upload file</button>
        <button type="submit" formaction="/shared/addons/add-branch">Add New Branch</button>
        <button type="submit">Save</button>
      </form>
    </body></html>
    """


def _account_too_new_page() -> bytes:
    return b"""
    <html><head><title>Account Too New - AnkiWeb</title></head>
      <body>
        <h4>Sorry, your account is too new for this action.</h4>
      </body>
    </html>
    """


def _deck_share_form() -> bytes:
    return b"""
    <html><body>
      <form method="post" action="/decks/share/1650000000000">
        <input placeholder="Title" name="title">
        <input placeholder="Tags" name="tags">
        <input placeholder="Support Page" name="supportUrl">
        <textarea name="description"></textarea>
        <input type="checkbox" name="confirmCopyright">
        <button type="submit">Share</button>
      </form>
    </body></html>
    """


def _public_deck_listing(title: str, description: str, image_sources: tuple[str, ...]) -> bytes:
    images = "".join(f'<img src="{escape(source, quote=True)}">' for source in image_sources)
    return (
        "<html><body>"
        f"<h1>{escape(title)}</h1>"
        f'<div class="shared-item-description">{escape(description)}</div>'
        f"{images}"
        "</body></html>"
    ).encode("utf-8")


def _unavailable_shared_item_page() -> bytes:
    return b"<html><body>This shared item is missing or currently unavailable.</body></html>"


def _delayed_unavailable_shared_item_page(delay_ms: int) -> bytes:
    if delay_ms <= 0:
        return _unavailable_shared_item_page()
    return f"""
    <html><body><main></main><script>
      setTimeout(() => {{
        document.querySelector('main').innerText = 'This shared item is missing or currently unavailable.';
      }}, {delay_ms});
    </script></body></html>
    """.encode("utf-8")


if __name__ == "__main__":
    unittest.main()
