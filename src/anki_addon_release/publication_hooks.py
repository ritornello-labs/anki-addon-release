"""Install local publication hooks without replacing unrelated hook integrations."""
from __future__ import annotations

import argparse
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
    source = str(Path(__file__).resolve().parents[1])
    for name, args in (("pre-commit", "--staged"), ("pre-push", '--pre-push "$2"')):
        path = hooks / name
        path.write_text(
            "#!/bin/sh\nset -eu\nexport PYTHONPATH=" + shlex.quote(source) + "\nexec "
            + shlex.quote(sys.executable) + " -m anki_addon_release.publication " + args + " --details\n"
        )
        path.chmod(0o755)
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
    print("Publication hooks installed for all linked worktrees; retain this tool installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
