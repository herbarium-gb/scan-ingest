#!/usr/bin/env python3
"""Reset local dev/test output: clears done/, error/, and logs/.

Always operates on these fixed repo-relative directories — never reads
DATA_DIR or the individual *_DIR env vars — so it's safe to run regardless
of what a local .env points production paths at. Never touches inbox/.

Run:  python scripts/reset_local_test_output.py
"""

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIRS_TO_CLEAR = ["done", "error", "logs"]


def main() -> None:
    for name in DIRS_TO_CLEAR:
        d = REPO_ROOT / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        print(f"Cleared {name}/")


if __name__ == "__main__":
    main()
