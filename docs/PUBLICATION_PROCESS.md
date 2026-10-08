# Publication checks

The shared checker is shipped with anki-addon-release 0.2.5. Use a reviewed,
immutable tool commit in CI and install local hooks on each publishing machine.
A public CI check runs after data has already reached GitHub: local checks are
required before the first push, and before every artifact upload.

```
anki-publication-hooks --private-dir /absolute/private/recovery-directory
anki-publication-check --staged --details
anki-publication-check --artifact dist/example.apkg --details
anki-publication-receipt dist/example.apkg --receipt dist/example.publication.json
```

The source equivalents are `PYTHONPATH=/absolute/tool/src python3 -m
anki_addon_release.publication_hooks`, `.publication`, and `.publication_receipt`.
Hooks share the repository's local configuration across linked worktrees. The
installer freezes the checker in Git metadata so edits or upgrades to the shared
tool cannot silently change another repo's gate. Reinstall after reviewing and
testing an upgrade; hook files are replaced atomically. Existing hooks are preserved: integrate them before
installing. Repeat setup on every machine; cloning does not install hooks. Do not
bypass a hook, format error, or unavailable remote. Anki Publisher backups must
be outside every Git checkout. `ANKI_PUBLICATION_PRIVATE_DIR` or local Git config
`publication.privateDirectory` configures private recovery directories for callers.

The pre-commit check reads the actual staged Git objects. Pre-push checks every
outgoing commit and tag target against the actual destination's advertised refs,
including files added and deleted between commits. Unknown ancestry means more
scanning; failures block publication. Checks cover forbidden recovery paths,
recognizable renamed Anki snapshots, several credential signatures, personal home
paths, LFS pointers, symlinks/submodules, bounded ZIP/gzip/tar archives, and legacy
APKG databases. Review logs and non-new scheduling are rejected. Nonfragmented MP4 containers have bounded box validation and separate text
metadata/padding inspection; unknown boxes, brands and binary metadata fail closed.
Media samples require visual review. Recognizable image bytes are inspected even
if a legacy image URL has a mismatched image suffix. MP4 upload receipts require
the exact reviewed hash, just like images. Metadata-free MPEG-1/2 Layer III MP3 streams have bounded frame validation;
ID3/APE tags, trailing bytes and unsupported frames fail closed. Audio content
still needs media QA. See [RFC 3119](https://www.rfc-editor.org/rfc/rfc3119).
Modern compressed
Anki collections, unknown binary formats, corrupt or oversized archives fail
closed. This is a known-signature check, not a universal personal-data classifier.

Add-on packaging automatically checks the exact archive, deletes rejected output,
and writes an adjacent `.publication.json` receipt. Every release needs a fresh
check after rebuilding or exporting. Receipts contain only schema, SHA-256, byte
count, check result, and visual-review status. Before uploading, recheck the exact
bytes and match the uploaded digest. Receipts attest only those bytes. A failed
preparation removes stale success receipts. For listing images supply
`--reviewed-media-sha256` from the exact image approved by the user; the hash does
not perform visual review. Review archive-contained media and prose as part of QA.

Public Actions output is generic pass/fail. Do not enable `--details`, upload
reports, or print source snippets/paths/values in public logs. Local `--details`
prints paths and rule names, never matched values. `--private-report` rejects a
location inside any Git checkout. Required publication status checks stop merges;
GitHub credential push protection adds prevention for supported credentials.
Neither prevents arbitrary personal data from being exposed through an unguarded
push on another machine. Synthetic fixtures and disposable profiles are required
for public demos and tests.

If a real exposure is found, stop publication and preserve a private incident
record. Rotate exposed credentials. Identify affected refs, logs, attachments,
release assets and listings, then prepare a scoped cleanup plan. Removing a
current file does not remove historical exposure. Do not automatically rewrite
history or delete incident evidence. Audit history and release assets separately
from current-tree rollout scans.

Rollout decisions belong in the private workspace coverage register. Live-data
repos need gates before their next public push; artifact producers before their
next release; ordinary source/assets before their next substantive publication;
private repos before their first public push or visibility change. Extension is
triggered by that publication event, without waiting for a calendar date. Complete
private inventory, supported-format review, synthetic regression tests, local hook
installation and quiet required CI before marking coverage complete.
