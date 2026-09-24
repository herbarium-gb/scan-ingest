#!/usr/bin/env python3
"""Reset a local test sandbox for a clean re-run of ingest.py.

Empties every output directory ingest.py writes to (lossy and lossless
JP2s, TIFFs, error/, logs/ — including the persisted current-folder state)
plus any TIFFs left in the inbox, resolving them from the same config.yml +
.env settings ingest.py uses. If TEST_TIFF_DIR is set, then copies the TIFFs
from there into the inbox, leaving the originals in place.

Safety: refuses to touch anything unless every one of those directories
lies inside a directory containing a file named ".test-sandbox". Create
that (empty) marker once in the root of a local sandbox; a production
server never has one, so this script can't empty real data there.

Run (from the repo root):  python scripts/reset_local_test_output.py
"""

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from ingest import load_config  # noqa: E402  (also loads .env)

MARKER = ".test-sandbox"
DIR_KEYS = ["inbox_dir", "done_jp2_dir", "done_jp2_lossless_dir",
            "done_tif_dir", "error_dir", "log_dir"]


def in_sandbox(d: Path) -> bool:
    return any((p / MARKER).is_file() for p in [d, *d.parents])


def main() -> None:
    config = load_config(REPO_ROOT / "config.yml")
    dirs = {k: (REPO_ROOT / config[k]).resolve() for k in DIR_KEYS}

    outside = [k for k, d in dirs.items() if not in_sandbox(d)]
    if outside:
        sys.exit(f"Refusing: not inside a directory marked with {MARKER}: "
                 f"{', '.join(outside)}. Nothing was changed.")

    for key, d in dirs.items():
        if not d.exists():
            continue
        if key == "inbox_dir":
            items = [p for p in d.iterdir() if p.suffix.lower() in (".tif", ".tiff")]
        else:
            items = list(d.iterdir())
        for p in items:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        print(f"Cleared {key} ({len(items)} item(s))")

    source = os.getenv("TEST_TIFF_DIR")
    if not source:
        print("TEST_TIFF_DIR not set — inbox left empty.")
        return
    tiffs = sorted(p for p in Path(source).iterdir()
                   if p.suffix.lower() in (".tif", ".tiff"))
    dirs["inbox_dir"].mkdir(parents=True, exist_ok=True)
    for p in tiffs:
        shutil.copy2(p, dirs["inbox_dir"] / p.name)
    print(f"Copied {len(tiffs)} TIFF(s) from TEST_TIFF_DIR into the inbox.")


if __name__ == "__main__":
    main()
