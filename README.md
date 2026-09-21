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
| File away | `ingest.py` | Moves the lossy JP2 to `done/jp2/` and the TIFF to `done/tif/` (sheets and Folder-ID labels together in each, matching Picturae's own flat delivery layout — kept apart only by extension, `.tiff` vs `.tif`); a sheet's lossless JP2 also goes to `done/jp2_lossless/`. Anything that raises along the way goes to `error/` instead. |

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
  `FM_PASSWORD`). `DATA_DIR` points all six directories (`upload`,
  `done/jp2`, `done/jp2_lossless`, `done/tif`, `error`, `logs`) at once; set
  `INBOX_DIR`/`DONE_JP2_DIR`/`DONE_JP2_LOSSLESS_DIR`/`DONE_TIF_DIR`/
  `ERROR_DIR`/`LOG_DIR` instead (or in addition) to redirect a single one of
  those paths.

## Scripts

Standalone dev/test tools, separate from the `ingest.py` entrypoint:

- `scripts/psnr_test.py` — encodes a TIFF at several JP2 compression rates
  and reports size/PSNR against the source, to help pick an encoding rate
  matching Picturae's quality/size. Defaults to `previous/*.tif` if no file
  is given (not present in a fresh checkout — see the script's docstring).
- `scripts/filemaker_test.py` — one-off round-trip test (create → read →
  delete) against the FileMaker Data API, verifying the `Scan-importer`
  account can write to `Herbariet_databas_test`. Seed of a future
  Postgres → FileMaker sync.
- `scripts/reset_local_test_output.py` — clears `done/`, `error/`, and
  `logs/` for a clean local re-run of `ingest.py`. Always targets these
  repo-relative directories, ignoring `.env`/`DATA_DIR`.

## Output layout

```
done/
  jp2/           lossy view JP2s (sheets + Folder-ID labels), renamed to accession/folder ID
  jp2_lossless/  lossless archival JP2s, sheets only — pixel-identical to the source TIFF
  tif/           TIFFs (sheets + Folder-ID labels), renamed to accession ID (.tiff)
                 or folder ID (.tif) — the extension is the only thing telling them apart
error/  anything that failed a step, left under its original/partial name
logs/
  ingest_<date>.tsv              per-run batch log
  filemaker_import_<date>.csv    new rows for FileMaker import
  current_folder_state.txt       persisted current-folder ID
```
