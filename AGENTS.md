# AGENTS.md

## Scope

These instructions apply to the `anki-addon-release` project.

## Project Shape

This project is a standalone Python package for building, checking, and eventually publishing Anki add-on releases.

Keep the package independent from `anki-addon-workbench`. The two tools should compose through CLIs, Makefiles, and optional hooks, but this project should not depend on the workbench for its core release workflow.

## Design Principles

- Prefer deterministic local packaging and validation before any browser automation.
- Treat AnkiWeb publishing as browser automation, not as a stable API, unless AnkiWeb later exposes a supported public API.
- Keep browser drivers optional so the core package remains usable in simple Python environments.
- Avoid storing AnkiWeb credentials. Prefer persistent browser profiles and interactive login when needed.
- Make dry runs and inspection commands first-class.
- Keep Study Triage as the first real-world target, but avoid Study Triage-specific assumptions in the library.

## Python

- Runtime code should avoid external dependencies unless they materially improve the release workflow.
- Tests should use `unittest` unless a stronger test dependency becomes worthwhile.
- When adding dependencies, keep the 7-day release-age policy in `pyproject.toml` and re-resolve any lockfile.


## Publication safety

Follow `docs/PUBLICATION_PROCESS.md`. Keep live snapshots, recovery packages and
audit reports outside Git. Install the shared local hooks before public pushes,
check exact upload bytes, and keep public CI output generic. Unsupported formats
block publication until inspection support and regression tests exist.
