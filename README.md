# anki-addon-release

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://pypi.org/project/anki-addon-release/)
[![Source on GitHub](https://img.shields.io/badge/source-GitHub-24292f)](https://github.com/ritornello-labs/anki-addon-release)

`anki-addon-release` is a small release helper for Anki add-ons and shared decks.

For add-ons, it provides deterministic local release prep:

- read release config from `pyproject.toml`
- validate the add-on source tree and `manifest.json`
- build a clean `.ankiaddon` archive
- inspect archive contents before upload

For shared decks, it can drive AnkiWeb's deck sharing form without storing the
private source deck name or deck id in the public repository.

For AnkiWeb, it can either write a regular-browser handoff bundle for add-ons or
drive the relevant form with Playwright. Browser publishing is review-first by
default: the final AnkiWeb save/share button is clicked only when `--submit` is
passed.

## Status

Early public release. It has been dogfooded against the
[Study Triage](https://ankiweb.net/shared/info/1850611434) add-on release flow,
including AnkiWeb login, create/update form filling, support URL filling,
branch compatibility fields, and local browser-flow regression tests.
Deck publishing supports public/private config splitting: public repos can hold
the listing file, while `.anki-addon-release.local.toml` or `.env` holds the
private Anki collection deck reference. A submitted deck release waits for
AnkiWeb's worker, verifies the signed-in owner listing, then reloads the
cache-busted listing in a cookie-free browser. It reports `submitted` only when
the public title, description text, and expected screenshot image URLs match.
During AnkiWeb's normal copyright-review hold, it reports
`submitted-pending-public-review` instead of claiming public visibility.
For a first deck share, omit `ankiweb.shared_id`: the tool snapshots the owner's
catalog before submission, discovers the newly assigned ID by its exact title,
and verifies its listing. An existing title requires an explicit shared ID.
If verification fails after submission, inspect the owner catalog before retrying.
It is published on PyPI via Trusted Publishing.

## Install

```bash
uvx anki-addon-release --help
```

For browser publishing support, include the browser extra and install the
Playwright browser runtime:

```bash
uvx --from "anki-addon-release[browser]" playwright install chromium
uvx --from "anki-addon-release[browser]" anki-addon-release --help
```

For unreleased changes on `main`, run directly from GitHub:

```bash
uvx --from "anki-addon-release @ git+https://github.com/ritornello-labs/anki-addon-release.git" \
  anki-addon-release --help
```

## Install For Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

No runtime dependencies are required for the initial local package/check workflow.

For browser publishing:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Use `sfw` when installing from public registries in this workspace.

### Credentials

The framework never stores plain AnkiWeb credentials in config. Point project
config at environment variable names, and put either plain values or 1Password
secret references under those same names in a private `.env` file:

```bash
ANKIWEB_EMAIL=op://Vault/Item/username
ANKIWEB_PASSWORD=op://Vault/Item/password
```

`Vault` and `Item` are placeholders; replace them with the names from your
own 1Password account.

For local development, prefer resolving those references at the process
boundary with 1Password CLI:

```bash
op run --env-file=.env -- anki-addon-release publish
```

Ordinary environment values still work, so CI can inject credentials without
1Password. Direct `op://` values are also accepted for compatibility when the
CLI is run without `op run`.

Reference the variable names in project config:

```toml
[tool.anki-addon-release.ankiweb]
login_email_env = "ANKIWEB_EMAIL"
login_password_env = "ANKIWEB_PASSWORD"
```

`publish` uses these values automatically when they are present, logging into
the persistent browser profile before opening the add-on upload or deck share
form. If neither variable is present, `publish` keeps using the existing browser
session in the profile.

or pass them on the command line:

```bash
anki-addon-release login \
  --email-env ANKIWEB_EMAIL \
  --password-env ANKIWEB_PASSWORD \
  --submit-login
```

Without `--submit-login`, the login command fills the fields and leaves the form
for review. In `--headless` mode, `--submit-login` is required.

### Separate Publishing Account

For real publishing, prefer a dedicated AnkiWeb account used only for add-ons
and decks.
This is an isolation pattern, not a framework requirement: it keeps release
automation separate from your personal synced collection and makes it easier to
reason about which account a browser profile is logged into.

Two AnkiWeb account lifecycle rules matter for that pattern:

- New-account publishing guard: AnkiWeb may send very new accounts to its
  [Account Too New](https://ankiweb.net/shared/too-new) page when they try to
  share add-ons. That public page says new accounts must meet certain criteria
  and can continue once the account is older, but it does not publish an exact
  wait period. Create the publishing account before you need it, or use an
  older dedicated account.
- Inactivity expiry: AnkiWeb's
  [terms](https://ankiweb.net/account/terms) and
  [account-removal article](https://anki.tenderapp.com/kb/anki-ecosystem/ankiweb-account-removal)
  say account data may be deleted if the account is not accessed or synced in
  the last 6 months. Log in to the dedicated account at least every few months
  to keep it active. The account-removal article notes that shared add-ons are
  not subject to the usual data expiry, but keeping the publishing account
  active avoids losing account access or release ownership context.

### Isolated AnkiWeb Publisher Collection

An established AnkiWeb account can also act as the publisher identity without
being the account used by a daily Anki installation. The `publisher` commands
create a persistent, separate Anki base directory. Its `Publisher` profile has
its custom sync URL explicitly cleared, so it syncs with AnkiWeb rather than a
self-hosted sync server.

It is intentionally not a disposable `anki-addon-workbench` profile: it holds
the small collection of deck copies that AnkiWeb needs in order to share and
update them. It does not read, write, or reuse the daily Anki profile.

Initialize it once. The default macOS base is
`~/Library/Application Support/anki-addon-release/publisher`; override it with
`--publisher-base` or `ANKI_PUBLISHER_BASE` if desired.

```bash
anki-addon-release publisher init
anki-addon-release publisher status
anki-addon-release publisher launch
```

The initializer copies the local AnkiConnect add-on into the isolated base,
excluding its managed metadata and user files, and assigns it port `8766` so it
never collides with a daily Anki profile's usual `8765`. On first launch, sign in to the
existing AnkiWeb account and complete the initial sync interactively. For a
repeatable one-shot login that never writes the credentials to disk, resolve the
existing 1Password references only for the launch process:

```bash
op run --env-file=/path/to/private/.env -- \
  anki-addon-release publisher launch \
  --login-email-env ANKIWEB_EMAIL \
  --login-password-env ANKIWEB_PASSWORD
```

The launcher injects a small inert helper into the publisher profile. Only when
both login flags are supplied does it read the child process environment, log in
through Anki's own sync flow, and erase those values from its environment. If
Anki asks which side should win during the first sync, choose **Download**. Do
not remove any decks yet.

After that first sync, create a portable snapshot before any cleanup. The
database portion is captured with SQLite's online backup API; run this while
Anki is idle, not while it is syncing or changing media.

```bash
anki-addon-release publisher backup
```

The archive is deliberately uncompressed so large collections finish backing
up promptly. It contains `collection.anki2`, all collection media, and a small
manifest. It does not contain credentials.

If Anki requires a database check before a publisher sync, close the Publisher
window and relaunch only that isolated profile with:

```bash
anki-addon-release publisher launch --check-database
```

After pruning, reclaim removed decks' orphaned media with Anki's own Check
Media workflow. This permanently deletes only Publisher files no remaining
card references, so run it only after a completed Publisher backup:

```bash
anki-addon-release publisher launch --check-database --clean-media
```

After Anki finishes opening, retry `publisher sync`.
If Anki then requires a one-way sync, choose **Upload to AnkiWeb** only when the
isolated Publisher collection is the intended source of truth.

The repeatable deck-transfer lane is:

```bash
# With the source collection open (for example, the private sync profile on 8765):
anki-addon-release publisher export \
  --deck-name 'Source::Deck' \
  --out /private/tmp/source-deck.apkg

# Launch the isolated publisher profile, then import and register its local id.
anki-addon-release publisher launch
anki-addon-release publisher import /private/tmp/source-deck.apkg \
  --deck-name 'Source::Deck' \
  --register-env-file /path/to/project/.env \
  --register-env-var ANKIWEB_SOURCE_DECK_ID
anki-addon-release publisher sync
```

`publisher sync` starts Anki's own sync through AnkiConnect. Wait for Anki to
report its result before continuing. `publisher verify` lists the deck names and
ids visible to the open publisher profile on `8766`, and `publisher deck-id` returns one
id.

When the isolated collection has been validated, `publisher prune` can trim it
without touching the daily profile. It keeps explicitly selected publisher deck
IDs and all their children, plans all other deletion work leaf-first, and leaves
Anki's `Default` deck in place. The command is dry-run by default; `--apply`
requires a completed portable backup made while Publisher is closed, before it
uses Anki's own deck-removal API:

```bash
anki-addon-release publisher prune \
  --keep-deck-id 1234567890 \
  --keep-deck-id-env ANKIWEB_PUBLISHER_KEEP_DECK_IDS

anki-addon-release publisher prune \
  --keep-deck-id-env ANKIWEB_PUBLISHER_KEEP_DECK_IDS \
  --apply \
  --backup "$HOME/Library/Application Support/anki-addon-release/publisher/backups/Publisher-YYYYMMDDTHHMMSSZ.zip"
```

Store comma-separated IDs in a private environment file, for example
`ANKIWEB_PUBLISHER_KEEP_DECK_IDS=1234567890,1234567891`; never put private
collection paths in the public repository. Run `publisher backup` before
launching Publisher for the apply step; its SQLite snapshot is intentionally
not made while the open app holds the collection.

Once the ID is registered in the project's private `.env`, ordinary
`publish` continues to use the existing review-first AnkiWeb form automation.

For listings that are already shared, retain their existing publisher-side deck
copies and IDs until an update has been verified. Replacing a deck wholesale
before understanding AnkiWeb's update linkage can create a second listing
instead of updating the original one.

## Configure An Add-on

Add a section like this to the add-on repository's `pyproject.toml`:

```toml
[tool.anki-addon-release]
source_dir = "."
manifest = "manifest.json"
artifact_dir = "dist"
artifact_name = "study-triage.ankiaddon"
include = ["__init__.py", "manifest.json", "README.md"]
exclude = [
  ".git",
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".uv-cache",
  "dist",
  "tests",
  "user_files",
]

[tool.anki-addon-release.ankiweb]
# Omit addon_id for a first publish; set it for updates.
addon_id = "1234567890"
listing_file = "release/ankiweb.md"
changelog_file = "CHANGELOG.md"
login_email_env = "ANKIWEB_EMAIL"
login_password_env = "ANKIWEB_PASSWORD"
```

`include` is optional. When omitted, the whole `source_dir` is considered and `exclude` filters out development files.

## AnkiWeb Listing File

Put public-facing listing copy in a Markdown file so agents and humans can edit
it without turning TOML into a prose document. By default, `release/ankiweb.md`
is loaded when it exists:

```markdown
---
title: Geography Deck
tags: geography maps
support_url: https://github.com/example/geography-deck
---

GitHub: [https://github.com/example/geography-deck](https://github.com/example/geography-deck)

Markdown description for AnkiWeb.
```

You can point at another path with `listing_file`. Inline `title`, `tags`,
`support_url`, and `description` values still work for small projects; explicit
`title_file`, `tags_file`, `support_url_file`, and `description_file` overrides
are available for unusual cases.

When `support_url` is a GitHub repository, `check`, `publish --dry-run`, and
browser publishing warn unless that exact GitHub URL is present as a clickable
Markdown link with the URL as its visible label. Use visible link text such as
`GitHub: [https://github.com/example/geography-deck](https://github.com/example/geography-deck)`;
do not rely only on the support URL field, a plain URL, or a hidden Markdown
link target.

## Configure A Deck

AnkiWeb shares decks from the logged-in user's synced collection. To avoid
leaking private collection structure, keep the public listing metadata in
the listing file, and keep the collection deck id/name in local config or env.

Public `pyproject.toml`:

```toml
[tool.anki-addon-release]
target = "deck"

[tool.anki-addon-release.ankiweb]
# Required for --submit so the public listing can be verified after sharing.
shared_id = "1234567890"
listing_file = "release/ankiweb.md"
login_email_env = "ANKIWEB_EMAIL"
login_password_env = "ANKIWEB_PASSWORD"

[tool.anki-addon-release.deck]
# Safe to commit: this names the env var, not the private deck.
source_deck_id_env = "ANKIWEB_SOURCE_DECK_ID"
```

Private `.anki-addon-release.local.toml`:

```toml
[deck]
source_deck_id = "1650000000000"
copyright_confirmed = true
```

or private `.env`:

```bash
ANKIWEB_EMAIL=op://Vault/Item/username
ANKIWEB_PASSWORD=op://Vault/Item/password
ANKIWEB_SOURCE_DECK_ID=1650000000000
```

Both files are loaded automatically when present. Add them to every deck repo's
`.gitignore`. Existing environment variables win over `.env` values, which
lets `op run --env-file=.env -- ...` resolve secrets before the framework reads
project-local configuration.

If you prefer not to store the numeric deck id, use a private deck name instead:

```toml
[deck]
source_deck_name = "Private::Geography"
copyright_confirmed = true
```

Deck-name resolution uses AnkiConnect's `deckNamesAndIds`, so Anki must be open
with AnkiConnect running. Numeric `source_deck_id` is the most deterministic
release input.

For an initial listing, make the one-time AnkiWeb share manually, record the
assigned `shared_id`, then use `anki-addon-release publish --submit` for every
auditable update. The tool refuses an automated submit without that id rather
than declaring the worker message a release success it cannot verify.

## Commands

From an add-on repository:

```bash
anki-addon-release check
anki-addon-release package
anki-addon-release inspect dist/study-triage.ankiaddon
```

From a deck repository:

```bash
anki-addon-release check
anki-addon-release publish --dry-run
```

Prepare an AnkiWeb publish plan without opening a browser:

```bash
anki-addon-release publish --dry-run
```

For deck targets, dry-run output redacts the private source deck id.

Preview the exact AnkiWeb Markdown description without opening a browser:

```bash
anki-addon-release publish --preview-description
```

Build a regular-browser handoff bundle for Codex or a human:

```bash
anki-addon-release handoff
```

The handoff bundle is written to `.anki-addon-release/handoff/` by default and
contains:

- `release-handoff.json`: machine-readable release metadata
- `browser-checklist.md`: human browser checklist
- `codex-browser-prompt.md`: prompt for a Codex browser-operator session
- `description.txt` and `changelog.txt` when configured

This path does not require Playwright. Use it when you want Codex to operate
your regular logged-in Chrome session.

Open an AnkiWeb login page using a persistent project-local browser profile:

```bash
anki-addon-release login
```

Prepare an upload with Playwright without clicking the final submit button:

```bash
anki-addon-release publish --diagnostics-dir out/release-diagnostics
```

Click the final add-on submit button only when the flow is trusted:

```bash
anki-addon-release publish --submit --diagnostics-dir out/release-diagnostics
```

For deck targets, the final share button also requires an explicit copyright
confirmation, either in private local config or as a command flag:

```bash
anki-addon-release publish --submit --confirm-copyright --diagnostics-dir out/release-diagnostics
```

Use `--mode create` for first-publish testing and `--mode update` for updating a configured `addon_id`. `--mode auto` uses update when `ankiweb.addon_id` is present and create otherwise.

The final AnkiWeb save/submit button is not clicked unless `--submit` is passed.
That review-first behavior is the default. In headed browser mode, the prepared
form stays open until you press Enter in the terminal. If the command is not
attached to an interactive terminal, it fails loudly instead of briefly opening
and closing the prepared form.

Without installation, from this repository:

```bash
PYTHONPATH=src python -m anki_addon_release --project /path/to/addon check
```

## Development

```bash
make check
```

Browser-flow stress tests are opt-in because they need Playwright and a local fake AnkiWeb server:

```bash
ANKI_ADDON_RELEASE_BROWSER_TESTS=1 python -m unittest tests.test_browser_flows -v
```

or:

```bash
make test-browser
```

Those tests exercise separate create and update forms against a local HTTP server and verify that Playwright uploads the `.ankiaddon` artifact plus the expected metadata.

## Releasing

Releases publish to PyPI via GitHub Actions using **Trusted Publishing** (OIDC);
no API token is stored. To cut a release:

```bash
# 1. bump `version` in pyproject.toml, commit
# 2. tag and push -- the tag must match the version
git tag v0.2.3
git push origin v0.2.3
```

The [`release.yml`](.github/workflows/release.yml) workflow checks that the tag
matches `pyproject.toml`, builds the sdist + wheel with `uv build`, and publishes.

One-time setup on PyPI (Account -> Publishing -> Add a pending publisher):

- **PyPI Project Name:** `anki-addon-release`
- **Owner / Repository:** `elvis-sik/anki-addon-release`
- **Workflow name:** `release.yml`
- **Environment name:** `pypi`

## Roadmap

- Add saved HTML/screenshot diagnostics on every browser failure.
- Add public artifact verification by downloading/installing the published add-on/deck into a disposable Anki profile, likely composed with `anki-addon-workbench`.
- Expand real-world compatibility coverage across more AnkiWeb form variants.
