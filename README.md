# scan-ingest

Ingest pipeline for herbarium sheets scanned in-house on a BookEye 4 at the
Gothenburg herbarium (GB). Each scanned TIFF is classified by its QR code,
converted to two JP2s (a lossy view + a lossless archival copy), and
registered for FileMaker import.

```
BookEye TIFF ──► classify QR ──► convert to JP2 x2 ──► validate ──► register ──► file away
 (upload/)        (Folder or        (lossy view +         (size/res)   (TSV log +   (done/)
                    Sheet)          lossless archive)                  FileMaker CSV)
```

A **Folder-ID label** (`GB-Folder_<digits>`) is the first image scanned in a
physical folder's batch — it goes through the same convert-to-JP2 step as a
sheet. Every **specimen sheet** (`GB-<digits>`) scanned after it inherits
that folder ID, by capture
order, until the next label appears. Species/taxonomy is never written here —
only the folder ID; an expert joins taxonomy in FileMaker later, keyed on
Folder QR.

The image server, IIIF viewer, and web platform are managed separately in
**herbarium-platform**. The Postgres/GBIF publication pipeline is managed
separately in **herbarium-data**.

## Quick start

1. Create the conda environment and activate it:
   ```
   conda env create -f environment.yml
   conda activate scan-ingest
   ```
2. Copy `.env.template` to `.env` and fill in the server-specific paths (and
   FileMaker credentials, if using `scripts/filemaker_test.py`).
3. Drop TIFF files into the inbox directory (`INBOX_DIR`, or `inbox/` for
   local dev).
4. Run:
   ```
   python ingest.py
   ```

Successfully processed files move to `done/`; anything that fails moves to
`error/` with the reason printed to stderr.

## Pipeline

`ingest.py` processes every `*.tif`/`*.tiff` in the inbox, in **capture
(file modification time) order** — not filename order, since BookEye's
per-job filename counter resets each session and would interleave different
days' scans.

| Step | Module | What it does |
|------|--------|---------------|
| Read QR | `steps/qr_read.py` | Decodes the QR code (and any barcodes) via pyzbar; classifies the QR as a Folder-ID label or a specimen accession. |
| Convert | `steps/convert.py` | TIFF → JP2 via `opj_compress`. Sheets get two encodes: a lossy view (`rate`-targeted, irreversible 9/7 wavelet) and a lossless archival copy (`lossless=True` — no `-r`/`-I`, reversible 5/3 wavelet, pixel-identical to the source). Folder-ID labels get the lossy view only — a folder cover is an administrative label, not the specimen being preserved, and its kraft-paper grain compresses disproportionately poorly (larger files for less archival value than a sheet's lossless copy). |
| Validate | `steps/validate.py` | Rejects a JP2 below the configured minimum resolution/file size, or above the maximum — separate thresholds for the lossy view (`validation`) and the much larger lossless copy (`validation_lossless`). |
| Register | `steps/register.py` | Appends a row to the daily TSV batch log and to a FileMaker-import CSV (Picturae's own column header, most taxonomy columns left blank). Only the lossy view's path is recorded — the lossless copy isn't part of that schema. |
| Track folder | `steps/state.py` | Persists the "current folder" ID across runs (`logs/current_folder_state.txt`), since a folder's label and its last sheets can land in different nightly runs. |
| File away | `ingest.py` | Moves the lossy JP2 to `done/jp2/` (sheets and Folder-ID labels together, matching Picturae's own flat delivery layout). A sheet's lossless JP2 goes to `done/jp2_lossless/`, and its TIFF is deleted once that copy validates (pixel-identical, so nothing is lost) — or moved to `done/tif/` instead if `tiff.keep` is `true` in `config.yml`. A Folder-ID label's TIFF is always deleted — no lossless copy exists to make keeping it worthwhile. Anything that raises along the way goes to `error/` instead. |
| Create FileMaker record | `steps/filemaker.py` | Best-effort, once all files are processed: creates a skeleton record in the FileMaker registration database via the Data API for each sheet filed away — only `AccessionNo` and `Löpnr`, the rest is transcribed by staff later. Created in ascending Löpnr order (not scan order), since FileMaker shows unsorted records in creation order. A record that already exists for that AccessionNo is left alone. Skipped if `FM_BASE_URL` isn't set; a failure is logged (`logs/filemaker_warnings_<date>.log`) but never fails the sheet. Folder-ID labels get no record. |
| Update shards | `ingest.py` | Best-effort, after the run: calls herbarium-platform's `build_shards.py` for each date touched, so new images become viewable without a manual step. Skipped if `HERBARIUM_PLATFORM_DIR` isn't set; a failure is logged (`logs/shard_warnings_<date>.log`) but never fails the ingest run — a missed update is safe to redo by hand later. |

A sheet's QR always identifies the sheet/image; a barcode (0 or more per
sheet) identifies one "kollekt" (field-collection event) mounted on it — see
the module docstring in `steps/qr_read.py` for the full QR-vs-barcode rule.

## Requirements

- Python 3.11 (see `environment.yml`: pillow, numpy, pyzbar, python-dotenv,
  pyyaml, requests)
- `zbar` and `openjpeg` (provides `opj_compress`/`opj_decompress`) — installed
  via conda-forge in `environment.yml`
- `requests` is used by `scripts/filemaker_test.py` only

## Configuration

Two layers, matching herbarium-data's split between repo-tracked config and
git-ignored secrets/paths:

- **`config.yml`** (committed) — JP2 encoding parameters (rate, resolutions,
  block/precinct/tile sizes) and validation thresholds. Encoding settings are
  matched to Picturae's own Kakadu recipe for this collection.
- **`.env`** (git-ignored — copy from `.env.template`) — server-specific
  paths that override `config.yml`'s relative defaults, plus FileMaker Data
  API credentials (`FM_BASE_URL`/`FM_DATABASE`/`FM_LAYOUT`/`FM_USER`/
  `FM_PASSWORD`). Three grouped path variables cover production:
  `INBOX_DIR` (where BookEye's SMB share lands), `IMAGE_STORAGE_DIR`
  (gives `Delivery/` — herbarium-platform's own `IMAGE_DATA_PATH` tree,
  named the same there — and `archive/`; only finished JP2s ever go here,
  and `archive/` is never read by RAIS/IIIF), and `LOCAL_STATE_DIR` (gives
  `tif/`/`error/`/`logs/`). Set `LOCAL_STATE_DIR` explicitly, off both the
  image catalogue and the code checkout — leaving it unset falls back to
  `config.yml`'s relative defaults, which resolve those three *inside*
  wherever `ingest.py` is run from, mixing operational state into the code
  directory. Any of the six individual `DONE_JP2_DIR`/`DONE_JP2_LOSSLESS_DIR`/
  `DONE_TIF_DIR`/`ERROR_DIR`/`LOG_DIR` vars still overrides its grouped
  parent for a one-off redirect.
- `FM_BASE_URL`/`FM_DATABASE`/`FM_LAYOUT`/`FM_USER`/`FM_PASSWORD` —
  optional; FileMaker Data API connection for the skeleton-record step.
  Unset `FM_BASE_URL` skips the step entirely.
- `HERBARIUM_PLATFORM_DIR`/`SHARD_TARGET` — optional; point at a
  herbarium-platform checkout to have `ingest.py` trigger a shard update
  after each run. Unset skips the step entirely.

## Scripts

Standalone dev/test tools, separate from the `ingest.py` entrypoint:

- `scripts/compression_quality_test.py` — encodes one or more TIFFs at several
  JP2 compression rates and reports size/PSNR against the source, to help
  pick an encoding rate matching Picturae's quality/size. Takes TIFF paths
  as arguments — no default sample bundled with the repo.
- `scripts/filemaker_test.py` — one-off round-trip test (create → read →
  delete) against the FileMaker Data API, verifying the configured
  account can write to the test database. Seed of a future
  Postgres → FileMaker sync.
- `scripts/filemaker_delete_test_records.py GB-… GB-…` — deletes the records
  with these AccessionNos, to re-test `ingest.py`'s FileMaker step from a
  known state. Refuses unless `FM_DATABASE` ends in `_test`; asks before
  deleting.
- `scripts/reset_local_test_output.py` — empties every directory
  `ingest.py` writes to (plus inbox TIFFs and the current-folder state) for
  a clean local re-run, then refills the inbox from `TEST_TIFF_DIR` if set.
  Uses the same `.env` paths as `ingest.py`, but refuses to run unless they
  all lie inside a directory containing a `.test-sandbox` marker file.

## Output layout

```
done/
  jp2/           lossy view JP2s (sheets + Folder-ID labels), renamed to accession/folder ID
  jp2_lossless/  lossless archival JP2s, sheets only — pixel-identical to the source TIFF
  tif/           TIFFs, sheets only, renamed to accession ID (.tiff) — only if
                 tiff.keep is true (default false); Folder-ID labels never
error/  anything that failed a step, left under its original/partial name
logs/
  ingest_<date>.tsv              per-run batch log
  filemaker_import_<date>.csv    new rows for FileMaker import
  current_folder_state.txt       persisted current-folder ID
  filemaker_warnings_<date>.log  sheets whose FileMaker record wasn't created, if any
  shard_warnings_<date>.log      failed shard updates, if any (safe to rerun by hand)
```
