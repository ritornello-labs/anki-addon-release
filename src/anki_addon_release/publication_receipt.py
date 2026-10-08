"""Check exact release bytes and emit a public receipt with a fixed field schema.

Run immediately before uploading; changing the artifact invalidates its receipt.
Visual-review hashes must come from the approved image, not automatic capture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .publication import content_reasons, requires_visual_review


def prepare(artifact: Path, reviewed: set[str]) -> dict:
    data = artifact.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    reasons = content_reasons(artifact.name, data)
    if requires_visual_review(artifact.name, data) and digest not in reviewed:
        reasons.add("visual-review-required")
    if reasons:
        raise ValueError("Release artifact rejected; run the local publication checker for details")
    # No source report fields, local paths, note/card IDs, or collection state are
    # copied. Byte size and hash are derived directly from the checked artifact.
    return {
        "schema": "anki-publication-v1",
        "sha256": digest,
        "bytes": len(data),
        "publication_check": "passed",
        "visual_review": "approved" if requires_visual_review(artifact.name, data) else "not-applicable",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--reviewed-media-sha256", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.receipt.resolve() == args.artifact.resolve():
            raise ValueError("Receipt must not overwrite the artifact")
        args.receipt.unlink(missing_ok=True)
        receipt = prepare(args.artifact, set(args.reviewed_media_sha256))
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        print("Exact release bytes checked; receipt written.")
    except Exception:
        raise SystemExit("Release preparation failed; no publication is permitted.") from None


if __name__ == "__main__":
    main()
