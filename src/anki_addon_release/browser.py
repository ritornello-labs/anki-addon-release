from __future__ import annotations

from dataclasses import dataclass, replace
from html import unescape
from pathlib import Path
import re
from time import monotonic, time
from urllib.parse import urljoin

from .errors import PublishError
from .credentials import LoginCredentials
from .publish import DeckPublishPlan, PublishPlan


_DECK_SHARE_COMPLETION_TIMEOUT_MS = 90_000
_DECK_PUBLIC_LISTING_TIMEOUT_MS = 90_000
_DECK_PUBLIC_LISTING_POLL_MS = 1_000
_SHARED_ITEM_UNAVAILABLE_TEXT = "This shared item is missing or currently unavailable."


@dataclass(frozen=True)
class BrowserPublishResult:
    status: str
    final_url: str
    screenshot: Path | None = None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class AnkiWebBrowser:
    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = False,
        timeout_ms: int = 15_000,
        slow_mo_ms: int = 0,
        diagnostics_dir: Path | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.slow_mo_ms = slow_mo_ms
        self.diagnostics_dir = diagnostics_dir

    def login(
        self,
        login_url: str,
        *,
        credentials: LoginCredentials | None = None,
        submit: bool = False,
    ) -> BrowserPublishResult:
        if self.headless and (credentials is None or not submit):
            raise PublishError("headless login requires credentials and --submit-login")

        sync_playwright = _sync_playwright()
        with sync_playwright() as playwright:
            context = None
            page = None
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.goto(login_url)
                _wait_for_frontend(page)
                status = "login-opened"
                if credentials is not None:
                    if _looks_logged_in(page):
                        _logout(page, login_url)
                        page.goto(login_url)
                        _wait_for_frontend(page)
                    _fill_login_form(page, credentials)
                    status = "login-filled"
                    if submit:
                        _click_login_submit(page)
                        _wait_for_login_result(page)
                        status = "login-submitted"
                elif _looks_logged_in(page):
                    status = "login-already-active"
                elif not self.headless:
                    input("Complete AnkiWeb login in the browser, then press Enter here to continue: ")
                screenshot = self._screenshot(page, "login")
                final_url = page.url
            except Exception as exc:
                raise self._failure("browser login failed", exc, page, "login-failed") from exc
            finally:
                _close_context(context)
        return BrowserPublishResult(status=status, final_url=final_url, screenshot=screenshot)

    def publish(self, plan: PublishPlan) -> BrowserPublishResult:
        if not plan.artifact_path.exists():
            raise PublishError(f"artifact does not exist: {plan.artifact_path}")

        sync_playwright = _sync_playwright()
        with sync_playwright() as playwright:
            context = None
            page = None
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.goto(plan.upload_url)
                _wait_for_frontend(page)
                _raise_known_publish_blockers(page)

                _ensure_upload_form(page)
                _fill_form(page, plan, include_artifact=True)

                if plan.submit:
                    _click_submit(page)
                    _wait_for_addon_submission(page)
                    if plan.mode == "update":
                        _save_update_metadata(page, plan)
                    status = "submitted"
                else:
                    status = "prepared"

                screenshot = self._screenshot(page, f"publish-{plan.mode}-{status}")
                final_url = page.url
                if not plan.submit and not self.headless:
                    _pause_for_review()
            except Exception as exc:
                raise self._failure(
                    "browser publish failed",
                    exc,
                    page,
                    f"publish-{plan.mode}-failed",
                ) from exc
            finally:
                _close_context(context)
        return BrowserPublishResult(status=status, final_url=final_url, screenshot=screenshot)

    def publish_deck(self, plan: DeckPublishPlan) -> BrowserPublishResult:
        if self.headless and not plan.submit:
            raise PublishError("headless deck publishing requires --submit")
        if plan.submit and not plan.copyright_confirmed:
            raise PublishError("deck publishing with --submit requires copyright confirmation")
        sync_playwright = _sync_playwright()
        with sync_playwright() as playwright:
            context = None
            page = None
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                previous_items = None
                if plan.submit and not plan.shared_id:
                    previous_items = _deck_owner_items(page, plan)
                    if plan.title in previous_items.values():
                        raise PublishError(
                            "an owner listing already has this title; configure its ankiweb.shared_id before updating"
                        )
                page.goto(plan.share_url)
                _wait_for_frontend(page)
                _raise_known_publish_blockers(page)

                _fill_deck_share_form(page, plan)

                if plan.submit:
                    _click_deck_submit(page)
                    _wait_for_deck_submission(page, plan)
                    _wait_for_deck_share_completion(
                        page,
                        timeout_ms=max(self.timeout_ms, _DECK_SHARE_COMPLETION_TIMEOUT_MS),
                    )
                    if previous_items is not None:
                        shared_id = _discover_new_deck_listing(
                            page, plan, previous_items,
                            timeout_ms=max(self.timeout_ms, _DECK_PUBLIC_LISTING_TIMEOUT_MS),
                        )
                        plan = replace(plan, shared_id=shared_id)
                    owner_listing_url = _verify_deck_owner_listing(
                        page,
                        plan,
                        timeout_ms=max(self.timeout_ms, _DECK_PUBLIC_LISTING_TIMEOUT_MS),
                    )
                    public_listing_url = _verify_deck_public_listing(
                        playwright,
                        plan,
                        timeout_ms=max(self.timeout_ms, _DECK_PUBLIC_LISTING_TIMEOUT_MS),
                    )
                    final_url = public_listing_url or owner_listing_url
                    status = "submitted" if public_listing_url else "submitted-pending-public-review"
                else:
                    status = "prepared"
                    final_url = page.url

                screenshot = self._screenshot(page, f"deck-publish-{status}")
                if not plan.submit and not self.headless:
                    _pause_for_review()
            except Exception as exc:
                raise self._failure("browser deck publish failed", exc, page, "deck-publish-failed") from exc
            finally:
                _close_context(context)
        return BrowserPublishResult(status=status, final_url=final_url, screenshot=screenshot)

    def _screenshot(self, page: object, stem: str) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = self.diagnostics_dir / f"{stem}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    def _html(self, page: object, stem: str) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = self.diagnostics_dir / f"{stem}.html"
        path.write_text(page.content(), encoding="utf-8")
        return path

    def _failure(self, message: str, exc: Exception, page: object | None, stem: str) -> PublishError:
        diagnostics = []
        if page is not None:
            screenshot = self._screenshot(page, stem)
            html = self._html(page, stem)
            if screenshot is not None:
                diagnostics.append(f"screenshot={screenshot}")
            if html is not None:
                diagnostics.append(f"html={html}")
        suffix = f" ({', '.join(diagnostics)})" if diagnostics else ""
        return PublishError(f"{message}: {exc}{suffix}")


def _sync_playwright() -> object:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise PublishError(
            "Playwright is required for browser publishing. "
            "Install with: pipx install 'anki-addon-release[browser]' "
            "or: python -m pip install '.[browser]'"
        ) from exc

    globals()["PlaywrightTimeoutError"] = PlaywrightTimeoutError
    return sync_playwright


def _close_context(context: object | None) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


def _ensure_upload_form(page: object) -> None:
    _raise_known_publish_blockers(page)

    if _file_input_count(page) > 0:
        return

    add_branch_button = page.get_by_role("button", name=re.compile("add new branch", re.IGNORECASE))
    if _count(add_branch_button) > 0:
        add_branch_button.first.click()
        _raise_known_publish_blockers(page)
        if _file_input_count(page) > 0:
            return

    upload_link = page.get_by_role("link", name=re.compile("upload", re.IGNORECASE))
    upload_button = page.get_by_role("button", name=re.compile("upload", re.IGNORECASE))
    for locator in (upload_link, upload_button):
        if _count(locator) > 0:
            locator.first.click()
            page.wait_for_load_state("domcontentloaded")
            _raise_known_publish_blockers(page)
            if _file_input_count(page) > 0:
                return

    _raise_known_publish_blockers(page)
    raise PublishError("could not find an upload form or upload button")


def _raise_known_publish_blockers(page: object) -> None:
    try:
        title = page.title()
        body = page.locator("body").inner_text(timeout=1_000)
    except Exception:
        return

    content = f"{title}\n{body}".lower()
    if "account is too new" in content:
        raise PublishError(
            "AnkiWeb refused publishing because this account is too new; "
            "use an older dedicated publishing account or wait until AnkiWeb allows sharing"
        )


def _wait_for_frontend(page: object) -> None:
    try:
        page.wait_for_load_state("networkidle")
    except Exception:
        pass


def _looks_logged_in(page: object) -> bool:
    logout_link = page.get_by_role("link", name=re.compile("log out|logout", re.IGNORECASE))
    if _count(logout_link) > 0:
        return True
    return _count(page.locator('a[href*="/account/logout"]')) > 0


def _logout(page: object, login_url: str) -> None:
    page.goto(urljoin(login_url, "/account/logout"))
    _wait_for_frontend(page)


def _wait_for_login_result(page: object) -> None:
    try:
        page.wait_for_function(
            """
            () => document.querySelector('a[href*="/account/logout"]')
                || document.querySelector('.alert-danger')
                || document.querySelector('[role="alert"]')
            """
        )
    except Exception:
        pass
    _wait_for_frontend(page)
    if _looks_logged_in(page):
        return

    message = _login_error_text(page)
    suffix = f": {message}" if message else ""
    raise PublishError(f"login did not complete{suffix}")


def _login_error_text(page: object) -> str:
    for selector in (".alert-danger", '[role="alert"]'):
        locator = page.locator(selector)
        if _count(locator) > 0:
            try:
                return locator.first.inner_text(timeout=1_000).strip()
            except Exception:
                return ""
    return ""


def _fill_login_form(page: object, credentials: LoginCredentials) -> None:
    if not _fill_text_after_wait(page, _EMAIL_CANDIDATES, credentials.email):
        raise PublishError("could not find login email field")
    if not _fill_text_after_wait(page, _PASSWORD_CANDIDATES, credentials.password):
        raise PublishError("could not find login password field")


def _click_login_submit(page: object) -> None:
    button = page.get_by_role("button", name=re.compile("log in|login|sign in", re.IGNORECASE))
    if _count(button) > 0:
        button.first.click()
        return

    submit = page.locator('button[type="submit"], input[type="submit"]').first
    if _count(submit) > 0:
        submit.click()
        return

    raise PublishError("could not find login submit button")


def _fill_form(page: object, plan: PublishPlan, *, include_artifact: bool) -> None:
    if include_artifact:
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(str(plan.artifact_path))

    _fill_optional_text(page, _TITLE_CANDIDATES, plan.title)
    if plan.support_url is not None:
        _fill_optional_text(page, _SUPPORT_URL_CANDIDATES, plan.support_url)
    _fill_version_range(page, plan.branch_min_version, plan.branch_max_version)
    if plan.description is not None:
        _fill_optional_text(page, _DESCRIPTION_CANDIDATES, plan.description)
    if plan.changelog is not None:
        _fill_optional_text(page, _CHANGELOG_CANDIDATES, plan.changelog)
    if plan.addon_id is not None:
        _fill_optional_text(page, _ADDON_ID_CANDIDATES, plan.addon_id)


def _fill_deck_share_form(page: object, plan: DeckPublishPlan) -> None:
    _fill_required_text(page, _TITLE_CANDIDATES, plan.title, field_name="deck title")
    if plan.tags is not None:
        _fill_optional_text(page, _TAGS_CANDIDATES, plan.tags)
    if plan.support_url is not None:
        _fill_optional_text(page, _SUPPORT_URL_CANDIDATES, plan.support_url)
    _fill_required_text(page, _DESCRIPTION_CANDIDATES, plan.description, field_name="deck description")
    if plan.copyright_confirmed:
        _check_first_checkbox(page)


def _save_update_metadata(page: object, plan: PublishPlan) -> None:
    page.goto(plan.upload_url)
    _wait_for_frontend(page)
    _ensure_upload_form(page)
    _fill_form(page, plan, include_artifact=False)
    _click_submit(page)
    _wait_for_addon_submission(page)


def _wait_for_addon_submission(page: object) -> None:
    try:
        page.wait_for_function(
            """
            () => location.pathname.startsWith('/shared/info/')
                || !document.querySelector('input[type="file"]')
            """
        )
        _wait_for_frontend(page)
    except Exception as exc:
        try:
            body = page.locator("body").inner_text(timeout=1_000).strip()
        except Exception:
            body = ""
        detail = f": {body[:500]}" if body else ""
        raise PublishError(f"AnkiWeb did not confirm the add-on submission{detail}") from exc


def _wait_for_deck_submission(page: object, plan: DeckPublishPlan) -> None:
    try:
        page.wait_for_function(
            """
            (shareUrl) => {
                const form = document.querySelector('form');
                const field = form?.querySelector('textarea');
                return !field || location.href !== shareUrl;
            }
            """,
            arg=plan.share_url,
        )
        _wait_for_frontend(page)
    except Exception as exc:
        try:
            body = page.locator("body").inner_text(timeout=1_000).strip()
        except Exception:
            body = ""
        detail = f": {body[:500]}" if body else ""
        raise PublishError(
            "AnkiWeb did not confirm the deck submission; the share form remained open after submit"
            f"{detail}"
        ) from exc


def _wait_for_deck_share_completion(page: object, *, timeout_ms: int) -> None:
    """Wait for AnkiWeb's asynchronous deck-share worker when it is present."""
    if "/decks/share/pending" not in page.url:
        return

    try:
        page.wait_for_function(
            """
            () => document.body?.innerText.includes('Completed successfully')
            """,
            timeout=timeout_ms,
        )
        _wait_for_frontend(page)
    except Exception as exc:
        try:
            body = page.locator("body").inner_text(timeout=1_000).strip()
        except Exception:
            body = ""
        detail = f": {body[:500]}" if body else ""
        raise PublishError(f"AnkiWeb did not complete the deck share{detail}") from exc


def _deck_owner_items(page: object, plan: DeckPublishPlan) -> dict[str, str]:
    """Read the authenticated catalog, including each listing's assigned ID."""
    page.goto(urljoin(plan.base_url.rstrip("/") + "/", "shared/mine"))
    _wait_for_frontend(page)
    _raise_known_publish_blockers(page)
    page.wait_for_function(
        "() => document.querySelector('table') && document.body.innerText.includes('Your Shared Items')"
    )
    rows = page.locator("table tr").evaluate_all(
        """rows => rows.flatMap(row => {
            const cells = row.querySelectorAll('td');
            const link = row.querySelector('a[href*="/shared/info/"]');
            const match = link?.getAttribute('href')?.match(/\\/shared\\/info\\/(\\d+)(?:[?#]|$)/);
            return match && cells.length >= 2
                ? [[match[1], cells[1].innerText.trim()]] : [];
        })"""
    )
    return dict(rows)


def _discover_new_deck_listing(
    page: object, plan: DeckPublishPlan, previous_items: dict[str, str], *, timeout_ms: int
) -> str:
    """Require one newly assigned owner ID; worker completion alone is insufficient."""
    deadline = monotonic() + timeout_ms / 1_000
    while monotonic() < deadline:
        items = _deck_owner_items(page, plan)
        matches = [item for item, title in items.items()
                   if item not in previous_items and title == plan.title]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise PublishError("multiple new owner listings match the submitted deck title; refusing to guess")
        remaining_ms = int((deadline - monotonic()) * 1_000)
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(_DECK_PUBLIC_LISTING_POLL_MS, remaining_ms))
    raise PublishError(
        "AnkiWeb share worker completed, but no new owner listing matched the submitted title; "
        "inspect the owner catalog before retrying"
    )


def _verify_deck_owner_listing(page: object, plan: DeckPublishPlan, *, timeout_ms: int) -> str:
    """Require the signed-in owner's item page to reflect submitted metadata."""
    if not plan.shared_id:
        raise PublishError("cannot verify a deck listing without ankiweb.shared_id")

    deadline = monotonic() + timeout_ms / 1_000
    listing_url = urljoin(plan.base_url.rstrip("/") + "/", f"shared/info/{plan.shared_id}")
    last_mismatches = ["the public listing did not load"]

    while monotonic() < deadline:
        cache_busted_url = f"{listing_url}?cb={int(time() * 1_000)}"
        try:
            page.goto(cache_busted_url, wait_until="domcontentloaded")
            remaining_ms = max(1, int((deadline - monotonic()) * 1_000))
            page.wait_for_function(
                "() => document.querySelector('h1')?.innerText?.trim()",
                timeout=min(5_000, remaining_ms),
            )
            title, description, image_sources = _public_listing_snapshot(page)
            mismatches = _public_listing_mismatches(
                plan,
                title=title,
                description=description,
                image_sources=image_sources,
            )
            if not mismatches:
                return cache_busted_url
            last_mismatches = mismatches
        except Exception as exc:
            last_mismatches = [f"the public listing did not load ({type(exc).__name__})"]

        remaining_ms = int((deadline - monotonic()) * 1_000)
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(_DECK_PUBLIC_LISTING_POLL_MS, remaining_ms))

    detail = "; ".join(last_mismatches)
    raise PublishError(
        "AnkiWeb share worker completed, but the signed-in listing did not match the submitted metadata "
        f"within {timeout_ms / 1_000:g} seconds: {detail}"
    )


def _verify_deck_public_listing(playwright: object, plan: DeckPublishPlan, *, timeout_ms: int) -> str | None:
    """Verify a listing from a cookie-free browser, or report AnkiWeb's review hold."""
    browser = None
    context = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        listing_url = urljoin(plan.base_url.rstrip("/") + "/", f"shared/info/{plan.shared_id}")
        cache_busted_url = f"{listing_url}?cb={int(time() * 1_000)}"
        page.goto(cache_busted_url, wait_until="domcontentloaded")
        # AnkiWeb's Svelte page may have an empty body immediately after DOM
        # content loads. Wait for either the review-hold message or a listing
        # heading before deciding which state the anonymous page represents.
        page.wait_for_function(
            f"""
            () => {{
                const body = document.body?.innerText || '';
                return body.includes({_SHARED_ITEM_UNAVAILABLE_TEXT!r})
                    || Boolean(document.querySelector('h1')?.innerText?.trim());
            }}
            """,
            timeout=timeout_ms,
        )
        body = page.locator("body").inner_text(timeout=timeout_ms)
        if _SHARED_ITEM_UNAVAILABLE_TEXT in body:
            return None

        title, description, image_sources = _public_listing_snapshot(page)
        mismatches = _public_listing_mismatches(
            plan,
            title=title,
            description=description,
            image_sources=image_sources,
        )
        if mismatches:
            raise PublishError(
                "AnkiWeb share worker completed, but the anonymous public listing did not match "
                f"the submitted metadata: {'; '.join(mismatches)}"
            )
        return cache_busted_url
    except PublishError:
        raise
    except Exception as exc:
        raise PublishError("could not verify the anonymous public listing") from exc
    finally:
        _close_context(context)
        if browser is not None:
            browser.close()


def _public_listing_snapshot(page: object) -> tuple[str, str, tuple[str, ...]]:
    title = page.locator("h1").first.inner_text(timeout=1_000).strip()
    description = page.locator("body").inner_text(timeout=1_000)
    try:
        raw_sources = page.locator("img").evaluate_all(
            "(images) => images.map((image) => image.getAttribute('src') || '')"
        )
    except Exception:
        raw_sources = []
    image_sources = tuple(source.strip() for source in raw_sources if isinstance(source, str) and source.strip())
    return title, description, image_sources


def _public_listing_mismatches(
    plan: DeckPublishPlan,
    *,
    title: str,
    description: str,
    image_sources: tuple[str, ...],
) -> list[str]:
    mismatches = []
    if _normalize_listing_text(title) != _normalize_listing_text(plan.title):
        mismatches.append("title")

    public_description = _normalize_listing_text(description)
    for index, marker in enumerate(_description_markers(plan.description), start=1):
        if marker not in public_description:
            mismatches.append(f"description paragraph {index}")

    for expected_source in _description_image_sources(plan.description):
        if not any(_same_image_source(expected_source, actual_source) for actual_source in image_sources):
            mismatches.append(f"screenshot {expected_source}")
    return mismatches


def _description_markers(markdown: str) -> tuple[str, ...]:
    without_images = _HTML_IMAGE_RE.sub("", _MARKDOWN_IMAGE_RE.sub("", markdown))
    visible_text = _MARKDOWN_LINK_RE.sub(r"\1", without_images)
    visible_text = re.sub(r"</?[^>]+>", "", visible_text)
    # Limit indentation to spaces/tabs. ``\s`` would consume paragraph
    # newlines before a heading or list item and silently merge markers.
    visible_text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", visible_text)
    visible_text = re.sub(r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", "", visible_text)
    visible_text = re.sub(r"`([^`]*)`", r"\1", visible_text)
    visible_text = visible_text.replace("**", "").replace("__", "").replace("~~", "")
    return tuple(
        marker
        for block in re.split(r"\n\s*\n", visible_text)
        if (marker := _normalize_listing_text(block))
    )


def _description_image_sources(markdown: str) -> tuple[str, ...]:
    sources = [match.group("url") for match in _MARKDOWN_IMAGE_RE.finditer(markdown)]
    sources.extend(match.group("url") for match in _HTML_IMAGE_RE.finditer(markdown))
    return tuple(_normalize_image_source(source) for source in sources)


def _normalize_listing_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _normalize_image_source(value: str) -> str:
    return unescape(value).strip().strip("<>")


def _same_image_source(expected: str, actual: str) -> bool:
    expected_source = _normalize_image_source(expected)
    actual_source = _normalize_image_source(actual)
    return expected_source == actual_source or expected_source.split("?", 1)[0] == actual_source.split("?", 1)[0]


def _pause_for_review() -> None:
    try:
        input("Review AnkiWeb form in the browser, then press Enter here to close: ")
    except EOFError as exc:
        raise PublishError(
            "prepared AnkiWeb form needs an interactive terminal for review; "
            "rerun from a TTY, use --submit when ready, or use --dry-run/--preview-description"
        ) from exc


def _fill_version_range(page: object, min_version: str | None, max_version: str | None) -> None:
    version_inputs = page.locator('input[maxlength="9"]')
    if min_version is not None and _count(version_inputs) >= 1:
        _fill_locator(version_inputs.nth(0), min_version)
    if max_version is not None and _count(version_inputs) >= 2:
        _fill_locator(version_inputs.nth(1), max_version)


def _click_submit(page: object) -> None:
    for pattern in (
        r"^(save|submit|publish)$",
        r"^(save changes|submit add-on|publish add-on)$",
        r"\b(save|submit|publish)\b",
    ):
        role_button = page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
        if _count(role_button) > 0:
            role_button.last.click()
            return

    submit = page.locator('button[type="submit"], input[type="submit"]').last
    if _count(submit) > 0:
        submit.click()
        return

    raise PublishError("could not find a submit button")


def _click_deck_submit(page: object) -> None:
    button = page.get_by_role("button", name=re.compile("share|submit|save|publish", re.IGNORECASE))
    if _count(button) > 0:
        button.first.click()
        return

    submit = page.locator('button[type="submit"], input[type="submit"]').last
    if _count(submit) > 0:
        submit.click()
        return

    raise PublishError("could not find a deck share submit button")


def _fill_optional_text(page: object, candidates: tuple[str, ...], value: str) -> bool:
    for selector in candidates:
        locator = page.locator(selector)
        if _count(locator) > 0:
            _fill_locator(locator.first, value)
            return True
    return False


def _fill_required_text(page: object, candidates: tuple[str, ...], value: str, *, field_name: str) -> None:
    if not _fill_text_after_wait(page, candidates, value):
        raise PublishError(f"could not find {field_name} field")


def _check_first_checkbox(page: object) -> None:
    checkbox = page.locator('input[type="checkbox"]').first
    if _count(checkbox) == 0:
        raise PublishError("could not find copyright confirmation checkbox")
    try:
        checkbox.check()
    except Exception:
        checkbox.click()


def _fill_locator(locator: object, value: str) -> None:
    locator.fill(value)
    try:
        locator.evaluate("(element) => element.dispatchEvent(new Event('change', { bubbles: true }))")
    except Exception:
        pass
    try:
        locator.evaluate(
            """(element) => {
                if (typeof element.scrollTop === "number") {
                    element.scrollTop = 0;
                }
                if (typeof element.setSelectionRange === "function") {
                    element.setSelectionRange(0, 0);
                }
            }"""
        )
    except Exception:
        pass


def _fill_text_after_wait(page: object, candidates: tuple[str, ...], value: str) -> bool:
    locator = page.locator(", ".join(candidates)).first
    try:
        locator.wait_for(state="visible")
        _fill_locator(locator, value)
        return True
    except Exception:
        return _fill_optional_text(page, candidates, value)


def _file_input_count(page: object) -> int:
    return _count(page.locator('input[type="file"]'))


def _count(locator: object) -> int:
    try:
        return locator.count()
    except Exception:
        return 0


_TITLE_CANDIDATES = (
    'input[name="title"]',
    'input[placeholder="Title"]',
    'input[name="name"]',
    'input[id*="title" i]',
    'input[id*="name" i]',
)
_TAGS_CANDIDATES = (
    'input[name="tags"]',
    'input[placeholder="Tags"]',
    'input[id*="tag" i]',
)
_DESCRIPTION_CANDIDATES = (
    'textarea[name="description"]',
    'textarea[rows="15"]',
    "form textarea",
    'textarea[name="desc"]',
    'textarea[id*="description" i]',
    'textarea[id*="desc" i]',
)
_SUPPORT_URL_CANDIDATES = (
    'input[name="support_url"]',
    'input[name="supportUrl"]',
    'input[placeholder="Support Page"]',
    'input[id*="support" i]',
)
_CHANGELOG_CANDIDATES = (
    'textarea[name="changes"]',
    'textarea[name="changelog"]',
    'textarea[name="release_notes"]',
    'textarea[id*="change" i]',
)
_ADDON_ID_CANDIDATES = (
    'input[name="addon_id"]',
    'input[name="id"]',
    'input[id*="addon" i]',
)
_EMAIL_CANDIDATES = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[id*="email" i]',
)
_PASSWORD_CANDIDATES = (
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[id*="password" i]',
)

_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<url><https?://[^>\s]+>|https?://[^\s)]+)(?:\s+\"[^\"]*\")?\s*\)",
    re.IGNORECASE,
)
_HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*[\"'](?P<url>[^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[([^\]]+)\]\(\s*(?:<[^>\s]+>|(?:[^()\s]+|\([^()\s]*\))+)(?:\s+\"[^\"]*\")?\s*\)",
)
