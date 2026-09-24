#!/usr/bin/env python3
"""Delete records from the FileMaker TEST database by AccessionNo, so
ingest.py's FileMaker step can be re-tested from a known state (record
missing -> created, record present -> left alone).

Refuses to run unless FM_DATABASE ends in "_test" — this is never meant
for the production database, and the production Scan-importer privilege
set has no Delete permission anyway. Lists what it found and asks for
confirmation before deleting anything.

Uses the same FM_* variables as ingest.py (see .env.template).

Run:  python scripts/filemaker_delete_test_records.py GB-0577660 GB-0577661 ...
(from the repo root)
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steps.filemaker import FileMakerClient  # noqa: E402

load_dotenv()


def main() -> None:
    accession_ids = sys.argv[1:]
    if not accession_ids:
        sys.exit(__doc__)

    fm = FileMakerClient.from_env()
    if fm is None:
        sys.exit("FM_BASE_URL not set.")
    if not fm.database.endswith("_test"):
        sys.exit(f"Refusing: FM_DATABASE is {fm.database!r}, not a *_test database.")

    try:
        found = {a: fm.find_record_ids(a) for a in accession_ids}
        print(f"In {fm.database}:")
        for a, ids in found.items():
            print(f"  {a}: {len(ids)} record(s)")
        to_delete = [(a, rid) for a, ids in found.items() for rid in ids]
        if not to_delete:
            print("Nothing to delete.")
            return

        if input(f"Delete these {len(to_delete)} record(s)? [y/N] ").strip().lower() != "y":
            print("Cancelled, nothing deleted.")
            return
        for a, rid in to_delete:
            fm.delete_record(rid)
            print(f"  deleted {a} (recordId {rid})")
    finally:
        fm.close()


if __name__ == "__main__":
    main()
