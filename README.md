# scan-ingest

Ingest pipeline for herbarium sheets scanned in-house on a BookEye 4 at the
Gothenburg herbarium (GB). Each scan is identified by its QR code,
converted to JP2, made viewable in the IIIF viewer, and given an empty
record in FileMaker, ready for staff to register (transcribe) the label
from the image.

```
BookEye  ──►  scan-ingest  ──►  JP2 archive  ──►  IIIF viewer
 (TIFF)       (QR, convert,  │   (lossy view +     (shard index)
               validate)     │    lossless copy)
                             └──►  FileMaker  (empty record per sheet)
```

A **Folder-ID label** (`GB-Folder_<digits>`) is scanned first in each
physical folder's batch; every **sheet** (`GB-<digits>`) scanned after it
belongs to that folder until the next label. Only the folder ID is
recorded; the species is transcribed from the scanned folder during
registration. The FileMaker record links to both the sheet and folder
images in the IIIF viewer (herbarium-platform).

The image server and viewer are managed separately in
**[herbarium-platform](https://github.com/herbarium-gb/herbarium-platform)**,
the GBIF publication pipeline in
**[herbarium-data](https://github.com/herbarium-gb/herbarium-data)**.

## Quick start

1. Create and activate the environment:
   `conda env create -f environment.yml && conda activate scan-ingest`
2. Copy `.env.template` to `.env` and fill it in.
3. Put TIFFs in the inbox and run `python ingest.py`.

## Pipeline

`ingest.py` processes the inbox in capture order (the timestamp in
BookEye's filename). Rationale for each step is in the module docstrings.

| Step | Module | What it does |
|------|--------|---------------|
| Read QR | `steps/qr_read.py` | Reads the QR code (and any barcodes); Folder-ID label or sheet? |
| Check not already archived | `steps/iiif_index.py` | Stops a scan whose ID is already archived, or whose destination file exists. |
| Convert | `steps/convert.py` | TIFF → lossy JP2, plus a lossless one for sheets. |
| Validate | `steps/validate.py` | Checks resolution and file size. |
| Log | `steps/register.py` | Adds a row to the TSV log and the FileMaker-format CSV. |
| Track folder | `steps/state.py` | Remembers the current folder across runs. |
| File away | `ingest.py` | Moves the JP2s into date folders, deletes the TIFF (unless `tiff.keep`). |
| Create FileMaker record | `steps/filemaker.py` | After the run: an empty record per sheet, in Löpnr order; existing records untouched. |
| Update shards | `ingest.py` | After the run: updates herbarium-platform's shard index so new images show in the viewer. |

The last two are skipped if not configured, and never stop the run.

## Running nightly

Silent when all went well. If anything needs a look — files moved to
`error/`, a failed FileMaker record or shard update, a crash — it exits
with code 1 and sends an alert email (configured in `.env`). The email
only says that something went wrong; the details are in `logs/cron.log`
and, per file, `errors_<date>.log` (see [Outputs](#outputs)). Decide for
each file in `error/`: rescan it, delete it, or move it back to the inbox.

Crontab line (`crontab -e`), adjusting the paths:

```
0 2 * * * flock -n /tmp/scan-ingest.lock nice -n 10 ionice -c 2 -n 7 ~/miniforge3/envs/scan-ingest/bin/python ~/scan-ingest/ingest.py >> /path/to/scan-ingest-state/logs/cron.log 2>&1
```

`flock -n` skips the night's run if the previous one is still going;
`nice`/`ionice` keep it from competing with the image server. Call the
environment's own `python` (cron doesn't activate conda).

## Requirements

Everything, including `zbar` and `openjpeg`, is in `environment.yml`
(conda-forge).

## Configuration

- **`config.yml`** — JP2 encoding and validation settings, and `tiff.keep`.
- **`.env`** (git-ignored) — paths, herbarium-platform, FileMaker and the
  notification email; copy `.env.template`, where each setting is explained.

## Scripts

- `scripts/reset_local_test_output.py` — empties a local test sandbox
  (marked by a `.test-sandbox` file) and refills its inbox from `TEST_TIFF_DIR`.
- `scripts/filemaker_delete_test_records.py GB-… GB-…` — deletes records in
  the FileMaker test database (`*_test` only).
- `scripts/filemaker_test.py` — create/read/delete round-trip against the
  FileMaker Data API.
- `scripts/compression_quality_test.py TIFF…` — size/PSNR at several JP2
  rates, for choosing the encoding rate.

## Outputs

```
Delivery/<YYYY/MM/DD>/   lossy JP2s, served by IIIF   (default: done/jp2/)
archive/<YYYY/MM/DD>/    lossless JP2s, sheets only   (default: done/jp2_lossless/)
error/                   files that failed a step
logs/
  cron.log                       console output of the nightly runs
  errors_<date>.log              why each file is in error/
  ingest_<date>.tsv              per-run log
  filemaker_import_<date>.csv    Picturae-format rows
  filemaker_warnings_<date>.log  FileMaker records not created
  shard_warnings_<date>.log      failed shard updates
  current_folder_state.txt       current folder ID
```
