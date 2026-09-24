"""Create a skeleton registration record in FileMaker via the Data API.

For each sheet, ingest.py creates one record in Herbariet_databas's
LD_huvudregister table holding only AccessionNo ("GB-0577660") and Löpnr
(the same number without the "GB-" prefix and leading zeros, 577660) —
the two fields FileMaker requires, and the only two the Scan-importer
account may write. Everything else on the record (locality, collector,
determination, ...) is transcribed by staff later, from the image.

A record that already exists for that AccessionNo (a rescan, or a sheet
registered by hand before scanning) counts as done, not as an error — there
is already a record for staff to fill in.

Connection details come from FM_BASE_URL, FM_DATABASE, FM_LAYOUT, FM_USER
and FM_PASSWORD (see .env.template). Follows the recipe proven by
scripts/filemaker_test.py.
"""

import os

import requests

CREATED = "created"
EXISTS = "exists"

# FileMaker Data API message codes (returned in the JSON body, not as HTTP
# status — the HTTP status is a generic 4xx/500 either way).
_NO_RECORDS_MATCH = "401"
_INVALID_TOKEN = "952"

_TIMEOUT = 30  # seconds per request


class FileMakerError(Exception):
    pass


class FileMakerClient:
    """One Data API session, opened on first use and reused for the whole
    run. Call close() when done (logs out, freeing the server's session)."""

    def __init__(self, base_url: str, database: str, layout: str,
                 user: str, password: str):
        self.api = f"{base_url.rstrip('/')}/fmi/data/v1/databases/{database}"
        self.database = database
        self.layout = layout
        self._auth = (user, password)
        self._token = None

    @classmethod
    def from_env(cls) -> "FileMakerClient | None":
        """None if FM_BASE_URL isn't set (the step is optional); raises if
        it is set but any of the other required variables is missing."""
        if not os.getenv("FM_BASE_URL"):
            return None
        names = ["FM_BASE_URL", "FM_DATABASE", "FM_LAYOUT", "FM_USER", "FM_PASSWORD"]
        missing = [n for n in names if not os.getenv(n)]
        if missing:
            raise FileMakerError(f"FM_BASE_URL is set but missing: {', '.join(missing)}")
        return cls(*(os.environ[n] for n in names))

    # --- session ---------------------------------------------------------

    def _login(self) -> None:
        r = requests.post(f"{self.api}/sessions", auth=self._auth, json={},
                          timeout=_TIMEOUT)
        self._token = _check(r)["response"]["token"]

    def close(self) -> None:
        if self._token:
            try:
                requests.delete(f"{self.api}/sessions/{self._token}", timeout=_TIMEOUT)
            except requests.RequestException:
                pass  # the server expires idle sessions on its own anyway
            self._token = None

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Authenticated request; logs in on first use, and once more if the
        session token has expired (FileMaker drops idle sessions after ~15
        minutes)."""
        if not self._token:
            self._login()
        for attempt in range(2):
            r = requests.request(
                method, f"{self.api}/layouts/{self.layout}/{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT, **kwargs,
            )
            if attempt == 0 and _codes(r) == {_INVALID_TOKEN}:
                self._login()
                continue
            return r

    # --- records ---------------------------------------------------------

    def find_record_ids(self, accession_id: str) -> list[str]:
        """recordIds of every record whose AccessionNo is exactly this value
        ("==" — a plain find would also match e.g. GB-05776601)."""
        r = self._request("POST", "_find",
                          json={"query": [{"AccessionNo": f"=={accession_id}"}]})
        if _codes(r) == {_NO_RECORDS_MATCH}:
            return []
        return [rec["recordId"] for rec in _check(r)["response"]["data"]]

    def create_skeleton(self, accession_id: str) -> str:
        """Create the AccessionNo + Löpnr record, unless one already exists.
        Returns CREATED or EXISTS."""
        if self.find_record_ids(accession_id):
            return EXISTS
        # A plain number, no leading zeros: GB-0577660 -> 577660. FileMaker
        # would otherwise store the zero-padded text as typed.
        lopnr = str(int(accession_id.removeprefix("GB-")))
        r = self._request("POST", "records",
                          json={"fieldData": {"AccessionNo": accession_id, "Löpnr": lopnr}})
        _check(r)
        return CREATED

    def delete_record(self, record_id: str) -> None:
        _check(self._request("DELETE", f"records/{record_id}"))


def _codes(r: requests.Response) -> set[str]:
    try:
        return {str(m.get("code")) for m in r.json().get("messages", [])}
    except ValueError:
        return set()


def _check(r: requests.Response) -> dict:
    """The response body, or FileMakerError carrying FileMaker's own error
    code and message — the HTTP status alone never says what went wrong.
    (The JSON key is "message", not "text".)"""
    try:
        body = r.json()
    except ValueError:
        body = None
    if not r.ok or body is None:
        if body and body.get("messages"):
            detail = "; ".join(f"code {m.get('code')}: {m.get('message')}"
                               for m in body["messages"])
        else:
            detail = f"HTTP {r.status_code}: {r.text[:200]!r}"
        raise FileMakerError(detail)
    return body
