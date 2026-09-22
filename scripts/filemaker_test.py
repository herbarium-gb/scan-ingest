#!/usr/bin/env python3
"""One-off test: verify the Scan-importer account can create/read/delete a
record in Herbariet_databas_test via the FileMaker Data API.

Creates one record with a clearly-marked test value in AccessionNo, reads it
back to confirm the round-trip, then deletes it and logs out — leaves nothing
behind if it succeeds. Meant as the seed of the real Postgres -> FileMaker
sync (see notes/scan-ingest-pipeline.txt), not a throwaway.

Reads all connection details from environment variables — never hardcode
credentials here, never put real values in this file or commit a filled-in
.env:

  FM_BASE_URL   e.g. https://filemaker.example.org  (no trailing slash)
  FM_DATABASE   e.g. Herbariet_databas_test
  FM_LAYOUT     e.g. Scan_import
  FM_USER       e.g. Scan-importer
  FM_PASSWORD

Run:
  FM_BASE_URL=https://... FM_DATABASE=Herbariet_databas_test \
  FM_LAYOUT=Scan_import FM_USER=Scan-importer FM_PASSWORD=... \
  python scripts/filemaker_test.py

Needs `requests` (not yet in environment.yml — `pip install requests` first,
or add it there if this becomes permanent).
"""

import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing required env var: {name}")
    return value


def fm_check(r: requests.Response) -> dict:
    """Raise with FileMaker's own error code/text, not just the bare HTTP status.

    The Data API always wraps its real error in the JSON body's "messages"
    array (e.g. {"messages": [{"code": "500", "text": "Date value does not
    meet validation..."}], "response": {}}) even when the HTTP status is a
    generic "500 FileMaker Data API Engine Error" — that HTTP-level message
    alone never tells you why.
    """
    try:
        body = r.json()
    except ValueError:
        body = None
    if not r.ok:
        if body and body.get("messages"):
            detail = "; ".join(f"code {m.get('code')}: {m.get('message')}" for m in body["messages"])
            print(f"  FileMaker error — {detail}", file=sys.stderr)
        if body is not None:
            # Full body, in case there's more than code/text (e.g. which
            # field actually tripped a validation on a create with only one
            # field set — code/text alone hasn't been enough to tell so far).
            print(f"  Full response body: {body}", file=sys.stderr)
        else:
            print(f"  No JSON error body; raw response: {r.text[:500]!r}", file=sys.stderr)
        r.raise_for_status()
    return body or {}


def main() -> None:
    base = env("FM_BASE_URL").rstrip("/")
    database = env("FM_DATABASE")
    layout = env("FM_LAYOUT")
    user = env("FM_USER")
    password = env("FM_PASSWORD")

    api = f"{base}/fmi/data/v1/databases/{database}"

    print(f"Logging in to {database!r} as {user!r}...")
    r = requests.post(f"{api}/sessions", auth=(user, password), json={})
    body = fm_check(r)
    token = body["response"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("  OK, got session token.")

    try:
        # GB-<7 digits>, matching the real accession-ID format used throughout
        # this collection. "99" + time-based digits keeps it obviously outside
        # any real allocated range (currently up around GB-115xxxx) while
        # varying per run so a leftover record from a crashed earlier run
        # can't collide via a uniqueness validation rule.
        test_value = f"GB-99{int(datetime.now().timestamp()) % 100000:05d}"
        # Löpnr is required and must equal AccessionNo without the "GB-" prefix.
        lopnr = test_value.removeprefix("GB-")
        print(f"Creating test record ({layout}.AccessionNo = {test_value!r}, "
              f"Löpnr = {lopnr!r})...")
        r = requests.post(
            f"{api}/layouts/{layout}/records",
            headers=headers,
            json={"fieldData": {"AccessionNo": test_value, "Löpnr": lopnr}},
        )
        body = fm_check(r)
        record_id = body["response"]["recordId"]
        print(f"  OK, created recordId={record_id}.")

        print("Reading it back...")
        r = requests.get(f"{api}/layouts/{layout}/records/{record_id}", headers=headers)
        body = fm_check(r)
        field_data = body["response"]["data"][0]["fieldData"]
        got = field_data.get("AccessionNo")
        assert got == test_value, f"Read back {got!r}, expected {test_value!r}"
        print(f"  OK, AccessionNo round-tripped correctly: {got!r}")

        print("Deleting test record...")
        r = requests.delete(f"{api}/layouts/{layout}/records/{record_id}", headers=headers)
        fm_check(r)
        print("  OK, deleted. No test data left behind.")

        print("\nSUCCESS: Scan-importer can create, read, and delete via the Data API.")

    except Exception:
        print(f"\nFAILED — if a record was created (see recordId above, if any),"
              f" it may need manual cleanup in {database}/{layout}.", file=sys.stderr)
        raise

    finally:
        print("Logging out...")
        requests.delete(f"{api}/sessions/{token}", headers=headers)


if __name__ == "__main__":
    main()
