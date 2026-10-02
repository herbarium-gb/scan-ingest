#!/usr/bin/env python3
"""Put a TIFF without a QR code in the inbox, to test the alert email.

The next run of ingest.py can't read a QR code from it, so it moves the
file to error/, reports one problem and sends the alert email (if
configured in .env). Nothing else happens: no JP2, no FileMaker record,
no shard update, and the current folder is unchanged. --clean removes the
test files from error/ afterwards.

Paths are resolved from the same config.yml + .env settings ingest.py uses.

Run (from the repo root):
  python scripts/make_alert_test_tiff.py          # add the test TIFF
  python ingest.py                                # expect one problem + email
  python scripts/make_alert_test_tiff.py --clean  # remove it from error/
"""

import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from ingest import load_config  # noqa: E402  (also loads .env)

PREFIX = "ALERT-TEST"


def main() -> None:
    config = load_config(REPO_ROOT / "config.yml")
    inbox, error_dir = Path(config["inbox_dir"]), Path(config["error_dir"])

    if "--clean" in sys.argv[1:]:
        found = sorted(error_dir.glob(f"{PREFIX}_*")) if error_dir.is_dir() else []
        for p in found:
            p.unlink()
        print(f"Removed {len(found)} test file(s) from {error_dir}")
        return

    others = [p for p in inbox.glob("*") if p.suffix.lower() in (".tif", ".tiff")
              and not p.name.startswith(PREFIX)] if inbox.is_dir() else []
    # The timestamp in BookEye's filename format, so ingest.py reads it as
    # the capture time (see capture_time_of() in ingest.py).
    path = inbox / f"{PREFIX}_{datetime.now():%Y-%m-%d_%H-%M-%S}.tif"
    inbox.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 300), "white").save(path)
    print(f"Created {path}")
    if others:
        print(f"Note: the inbox also holds {len(others)} other TIFF(s); "
              "the next run processes those too.")


if __name__ == "__main__":
    main()
