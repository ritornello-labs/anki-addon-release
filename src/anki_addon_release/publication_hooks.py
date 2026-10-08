"""Install local publication hooks without replacing unrelated hook integrations."""
from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path
import shlex
import subprocess
import sys

from .private_artifacts import validate_private_path


def install(private_dir: Path | None = None) -> Path:
    common = Path(subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    hooks = common / "publication-hooks"
    old = subprocess.run(["git", "config", "--get", "core.hooksPath"], capture_output=True, text=True)
    if old.returncode == 0 and Path(old.stdout.strip()).resolve() != hooks:
        raise ValueError("Existing hooksPath requires integration; it was preserved")
    for name in ("pre-commit", "pre-push"):
        if (common / "hooks" / name).exists() and old.returncode != 0:
            raise ValueError("Existing Git hooks require integration; they were preserved")
    if private_dir is not None:
        directory = validate_private_path(private_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        subprocess.run(["git", "config", "--local", "publication.privateDirectory", str(directory)], check=True)
    hooks.mkdir(exist_ok=True)
    # Store an immutable source snapshot in Git metadata. Uncommitted edits or
    # later upgrades of the tool must not silently change another repo's guard.
    package = Path(__file__).resolve().parent
    files = {name: (package / name).read_bytes() for name in
             ("__init__.py", "publication.py", "private_artifacts.py")}
    digest = hashlib.sha256(b"".join(name.encode() + data for name, data in files.items())).hexdigest()
    snapshots = common / "publication-tools"
    snapshots.mkdir(exist_ok=True)
    frozen = snapshots / digest
    if not frozen.exists():
        with tempfile.TemporaryDirectory(dir=snapshots) as temporary:
            destination = Path(temporary) / "tool"
            copied_package = destination / "anki_addon_release"
            copied_package.mkdir(parents=True)
            for name, data in files.items():
                (copied_package / name).write_bytes(data)
            destination.rename(frozen)
    for name, data in files.items():
        if (frozen / "anki_addon_release" / name).read_bytes() != data:
            raise ValueError("Installed checker snapshot is inconsistent")
    source = str(frozen)
    for name, args in (("pre-commit", "--staged"), ("pre-push", '--pre-push "$2"')):
        path = hooks / name
        temporary_hook = hooks / ("." + name + ".new")
        temporary_hook.write_text(
            "#!/bin/sh\nset -eu\nexport PYTHONPATH=" + shlex.quote(source) + "\nexec "
            + shlex.quote(sys.executable) + " -m anki_addon_release.publication " + args + " --details\n"
        )
        temporary_hook.chmod(0o755)
        temporary_hook.replace(path)
    subprocess.run(["git", "config", "--local", "core.hooksPath", str(hooks)], check=True)
    return hooks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        install(args.private_dir)
    except Exception:
        print("Hook installation failed; existing integrations were preserved.", file=sys.stderr)
        return 1
    print("Frozen publication hooks installed for all linked worktrees; reinstall after a reviewed tool upgrade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
