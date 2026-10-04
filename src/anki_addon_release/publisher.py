from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid
import zipfile

from .errors import ReleaseError


ANKI_CONNECT_PACKAGE = "2055492159"
LOGIN_ADDON_PACKAGE = "zz_anki_addon_release_publisher_login"
DEFAULT_ANKI_CONNECT_URL = "http://127.0.0.1:8765"
DEFAULT_PUBLISHER_ANKI_CONNECT_PORT = 8766
DEFAULT_PUBLISHER_ANKI_CONNECT_URL = f"http://127.0.0.1:{DEFAULT_PUBLISHER_ANKI_CONNECT_PORT}"
DEFAULT_PROFILE = "Publisher"


@dataclass(frozen=True)
class PublisherPaths:
    base: Path
    profile: str

    @property
    def profile_dir(self) -> Path:
        return self.base / self.profile

    @property
    def collection_path(self) -> Path:
        return self.profile_dir / "collection.anki2"

    @property
    def media_dir(self) -> Path:
        return self.profile_dir / "collection.media"

    @property
    def anki_connect_dir(self) -> Path:
        return self.base / "addons21" / ANKI_CONNECT_PACKAGE

    @property
    def backup_dir(self) -> Path:
        return self.base / "backups"

    @property
    def login_addon_dir(self) -> Path:
        return self.base / "addons21" / LOGIN_ADDON_PACKAGE

    @property
    def anki_connect_config(self) -> Path:
        return self.anki_connect_dir / "config.json"


@dataclass(frozen=True)
class PublisherPrunePlan:
    keep_roots: tuple[str, ...]
    retained_decks: tuple[str, ...]
    delete_stages: tuple[tuple[str, ...], ...]
    missing_keep_deck_ids: tuple[str, ...]

    @property
    def delete_decks(self) -> tuple[str, ...]:
        return tuple(deck for stage in self.delete_stages for deck in stage)


def default_publisher_base() -> Path:
    configured = os.environ.get("ANKI_PUBLISHER_BASE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "anki-addon-release" / "publisher"


def default_publisher_profile() -> str:
    return os.environ.get("ANKI_PUBLISHER_PROFILE") or DEFAULT_PROFILE


def default_anki_bin() -> str:
    candidates = (
        Path("/Applications/Anki.app/Contents/Resources/.venv/bin/anki"),
        Path("/root/.local/share/AnkiProgramFiles/.venv/bin/anki"),
        Path("/usr/local/bin/anki"),
        Path("/usr/bin/anki"),
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("anki") or "anki"


def default_anki_python(anki_bin: str) -> str | None:
    candidate = Path(anki_bin).parent / "python"
    return str(candidate) if candidate.exists() else None


def default_anki_connect_source() -> Path:
    return Path.home() / "Library" / "Application Support" / "Anki2" / "addons21" / ANKI_CONNECT_PACKAGE


def initialize_publisher(
    paths: PublisherPaths,
    *,
    anki_python: str,
    anki_connect_source: Path,
    anki_connect_port: int = DEFAULT_PUBLISHER_ANKI_CONNECT_PORT,
) -> None:
    if paths.profile_dir.exists() or _base_has_other_profiles(paths):
        raise ReleaseError(
            f"publisher base already exists: {paths.base}; use publisher status or launch instead of reinitializing it"
        )
    if not anki_python:
        raise ReleaseError("could not locate Anki's bundled Python; pass --anki-python")
    if not anki_connect_source.is_dir() or not (anki_connect_source / "__init__.py").is_file():
        raise ReleaseError(
            "could not locate a usable AnkiConnect add-on; pass --anki-connect-source pointing at its add-on directory"
        )
    if paths.anki_connect_dir.exists():
        raise ReleaseError(f"publisher AnkiConnect directory already exists: {paths.anki_connect_dir}")

    paths.base.mkdir(parents=True, exist_ok=True)
    _seed_profile(paths, anki_python)
    paths.anki_connect_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        anki_connect_source,
        paths.anki_connect_dir,
        ignore=shutil.ignore_patterns("meta.json", "user_files", "__pycache__", ".DS_Store"),
    )
    configure_anki_connect(paths, port=anki_connect_port)


def publisher_status(paths: PublisherPaths) -> dict[str, object]:
    return {
        "base": str(paths.base),
        "profile": paths.profile,
        "profile_dir": str(paths.profile_dir),
        "initialized": (paths.base / "prefs21.db").is_file(),
        "collection_present": paths.collection_path.is_file(),
        "anki_connect_present": (paths.anki_connect_dir / "__init__.py").is_file(),
        "anki_connect_url": _configured_anki_connect_url(paths),
        "backup_dir": str(paths.backup_dir),
    }


def backup_publisher_collection(paths: PublisherPaths, *, output: Path | None = None) -> Path:
    if not paths.collection_path.is_file():
        raise ReleaseError(f"publisher collection not found: {paths.collection_path}")

    if output is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = paths.backup_dir / f"{paths.profile}-{timestamp}.zip"
    output = output.resolve()
    if output.exists():
        raise ReleaseError(f"backup already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="anki-addon-release-backup-") as temporary:
        snapshot = Path(temporary) / "collection.anki2"
        _snapshot_sqlite(paths.collection_path, snapshot)
        media_files = _media_files(paths.media_dir)
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile": paths.profile,
            "collection": "collection.anki2",
            "media_file_count": len(media_files),
        }
        # Backups are a safety checkpoint before moving or deleting decks.  Store
        # them directly so large media collections finish promptly and reliably.
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
            archive.write(snapshot, "collection.anki2")
            for file in media_files:
                archive.write(file, file.relative_to(paths.profile_dir).as_posix())
            archive.writestr("publisher-backup.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return output


def launch_publisher(
    paths: PublisherPaths,
    *,
    anki_bin: str,
    login_credentials: tuple[str, str] | None = None,
    login_credential_env_names: tuple[str, str] | None = None,
    check_database: bool = False,
    clean_media: bool = False,
    anki_connect_port: int = DEFAULT_PUBLISHER_ANKI_CONNECT_PORT,
) -> tuple[subprocess.Popen[str], list[str]]:
    if not publisher_status(paths)["initialized"]:
        raise ReleaseError(f"publisher profile is not initialized: {paths.base}; run publisher init first")
    if _publisher_port_in_use(anki_connect_port):
        raise ReleaseError(
            f"publisher port {anki_connect_port} is already in use; reuse the running Publisher instead of launching another"
        )
    configure_anki_connect(paths, port=anki_connect_port)

    anki_python = default_anki_python(anki_bin)
    if anki_python is None:
        raise ReleaseError("could not locate Anki's bundled Python next to its launcher; pass --anki-bin for a standard Anki install")
    command = [
        anki_python,
        str(Path(__file__).with_name("publisher_run_anki.py")),
        "-b",
        str(paths.base),
        "-p",
        paths.profile,
        "--lang",
        "en",
    ]
    env = os.environ.copy()
    instance = uuid.uuid5(uuid.NAMESPACE_URL, f"{paths.base.resolve()}#{paths.profile}")
    env["ANKI_SINGLE_INSTANCE_KEY"] = f"anki-addon-release-publisher-{instance.hex}"
    for name in login_credential_env_names or ():
        env.pop(name, None)
    if login_credentials is not None or check_database or clean_media:
        _ensure_login_addon(paths)
    if check_database:
        env["ANKI_ADDON_RELEASE_PUBLISHER_CHECK_DATABASE"] = "1"
    if clean_media:
        env["ANKI_ADDON_RELEASE_PUBLISHER_CLEAN_MEDIA"] = "1"
    if login_credentials is not None:
        env["ANKI_ADDON_RELEASE_PUBLISHER_LOGIN"] = "1"
        env["ANKI_ADDON_RELEASE_PUBLISHER_EMAIL"] = login_credentials[0]
        env["ANKI_ADDON_RELEASE_PUBLISHER_PASSWORD"] = login_credentials[1]
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process, command


def _publisher_port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise ReleaseError(f"cannot establish whether Publisher port {port} is available") from exc


def anki_connect_request(url: str, action: str, **params: object) -> object:
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            f"could not contact AnkiConnect at {url}; launch the publisher profile and wait for Anki to finish starting"
        ) from exc

    if not isinstance(response_data, dict):
        raise ReleaseError("AnkiConnect returned an unexpected response")
    if response_data.get("error"):
        raise ReleaseError(f"AnkiConnect {action} failed: {response_data['error']}")
    return response_data.get("result")


def publisher_decks(url: str) -> dict[str, str]:
    result = anki_connect_request(url, "deckNamesAndIds")
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise ReleaseError("AnkiConnect deckNamesAndIds returned an unexpected response")
    return {name: str(deck_id) for name, deck_id in result.items()}


def deck_id_for_name(url: str, deck_name: str) -> str:
    deck_id = publisher_decks(url).get(deck_name)
    if deck_id is None:
        raise ReleaseError(f"deck not found in publisher collection: {deck_name}")
    return deck_id


def build_publisher_prune_plan(decks: dict[str, str], *, keep_deck_ids: list[str]) -> PublisherPrunePlan:
    requested_ids = tuple(dict.fromkeys(deck_id.strip() for deck_id in keep_deck_ids if deck_id.strip()))
    if not requested_ids:
        raise ReleaseError("publisher prune requires at least one --keep-deck-id or --keep-deck-id-env value")
    if any(not deck_id.isdigit() for deck_id in requested_ids):
        raise ReleaseError("publisher prune keep deck ids must be numeric")

    id_to_name = {deck_id: name for name, deck_id in decks.items()}
    missing = tuple(sorted(deck_id for deck_id in requested_ids if deck_id not in id_to_name))
    keep_roots = tuple(sorted(id_to_name[deck_id] for deck_id in requested_ids if deck_id in id_to_name))
    protected = {
        name
        for name in decks
        if any(name == root or name.startswith(f"{root}::") for root in keep_roots)
    }
    remaining = dict(decks)
    stages: list[tuple[str, ...]] = []
    while True:
        leaves = tuple(
            sorted(
                name
                for name in remaining
                if name != "Default"
                and name not in protected
                and not any(other.startswith(f"{name}::") for other in remaining)
            )
        )
        if not leaves:
            break
        stages.append(leaves)
        for name in leaves:
            del remaining[name]

    return PublisherPrunePlan(
        keep_roots=keep_roots,
        retained_decks=tuple(sorted(remaining)),
        delete_stages=tuple(stages),
        missing_keep_deck_ids=missing,
    )


def apply_publisher_prune(url: str, plan: PublisherPrunePlan) -> int:
    if plan.missing_keep_deck_ids:
        missing = ", ".join(plan.missing_keep_deck_ids)
        raise ReleaseError(f"publisher prune keep deck ids were not found: {missing}")
    for stage in plan.delete_stages:
        anki_connect_request(url, "deleteDecks", decks=list(stage), cardsToo=True)
    return len(plan.delete_decks)


def export_deck(url: str, *, deck_name: str, output: Path, include_scheduling: bool = False) -> Path:
    output = output.resolve()
    if output.exists():
        raise ReleaseError(f"refusing to overwrite existing export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    anki_connect_request(
        url,
        "exportPackage",
        deck=deck_name,
        path=str(output),
        includeSched=include_scheduling,
    )
    if not output.is_file():
        raise ReleaseError("AnkiConnect reported a successful export but did not create an APKG")
    return output


def import_deck(url: str, package: Path) -> None:
    package = package.resolve()
    if not package.is_file():
        raise ReleaseError(f"APKG not found: {package}")
    anki_connect_request(url, "importPackage", path=str(package))


def start_sync(url: str) -> None:
    try:
        anki_connect_request(url, "sync")
    except ReleaseError as exc:
        if "Sync status 2" in str(exc):
            raise ReleaseError(
                "Anki requires a one-way sync. In the isolated Publisher profile, choose Upload to AnkiWeb only "
                "after confirming that this publisher copy is the intended source."
            ) from exc
        raise


def configure_anki_connect(paths: PublisherPaths, *, port: int) -> None:
    if not 1 <= port <= 65535:
        raise ReleaseError("AnkiConnect port must be between 1 and 65535")
    if not paths.anki_connect_config.is_file():
        raise ReleaseError(f"publisher AnkiConnect config not found: {paths.anki_connect_config}")
    try:
        config = json.loads(paths.anki_connect_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReleaseError("publisher AnkiConnect config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ReleaseError("publisher AnkiConnect config must be a JSON object")
    config["webBindPort"] = port
    paths.anki_connect_config.write_text(json.dumps(config, indent=4, sort_keys=True) + "\n", encoding="utf-8")


def register_deck_id(env_file: Path, *, variable: str, deck_id: str) -> None:
    valid_start = bool(variable) and (variable[0].isalpha() or variable[0] == "_")
    valid_rest = bool(variable) and variable.replace("_", "").isalnum()
    if not valid_start or not valid_rest:
        raise ReleaseError(f"invalid environment variable name: {variable!r}")
    if not deck_id.isdigit():
        raise ReleaseError("publisher deck ids must be numeric")

    previous = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    lines = previous.splitlines(keepends=True)
    replacement = f"{variable}={deck_id}\n"
    for index, line in enumerate(lines):
        if line.startswith(f"{variable}="):
            lines[index] = replacement
            break
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(replacement)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("".join(lines), encoding="utf-8")


def _seed_profile(paths: PublisherPaths, anki_python: str) -> None:
    command = [
        anki_python,
        str(Path(__file__).with_name("publisher_seed.py")),
        "--base",
        str(paths.base),
        "--profile",
        paths.profile,
    ]
    completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 0:
        raise ReleaseError(
            "could not create the isolated publisher profile with Anki's bundled Python:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection, sqlite3.connect(destination) as destination_connection:
        source_connection.backup(destination_connection)


def _media_files(media_dir: Path) -> list[Path]:
    if not media_dir.exists():
        return []
    return sorted(path for path in media_dir.rglob("*") if path.is_file())


def _base_has_other_profiles(paths: PublisherPaths) -> bool:
    if not paths.base.is_dir():
        return False
    reserved = {"addons21", "backups"}
    return any(entry.is_dir() and entry.name not in reserved for entry in paths.base.iterdir())


def _configured_anki_connect_url(paths: PublisherPaths) -> str | None:
    if not paths.anki_connect_config.is_file():
        return None
    try:
        config = json.loads(paths.anki_connect_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    port = config.get("webBindPort") if isinstance(config, dict) else None
    return f"http://127.0.0.1:{port}" if isinstance(port, int) else None


def _ensure_login_addon(paths: PublisherPaths) -> None:
    paths.login_addon_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("publisher_login_addon.py")
    destination = paths.login_addon_dir / "__init__.py"
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
