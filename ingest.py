#!/usr/bin/env python3
"""Herbarium GB scan ingest pipeline.

Processes TIFF files dropped in the inbox folder. Each file's QR code is read
and classified as either:

  - a Folder-ID label (e.g. "GB-Folder_0036571") — the first image scanned
    for a physical folder's batch. It goes through the same convert-to-JP2 +
    validate steps as a sheet, then is filed away; every SHEET processed
    after it inherits its folder_id, until the next Folder-ID label is seen.
  - an ordinary specimen sheet (e.g. "GB-0523177"), which goes through the
    full pipeline: convert TIFF -> JP2, validate, register (TSV log +
    FileMaker-import CSV), move to done/.

Both kinds get a row in a FileMaker-import CSV that reuses Picturae's own
column names (see steps/register.py) — most columns stay blank for now;
species gets joined onto Folder QR later, by an expert.

Files are processed in scan (capture) order, not filename order — BookEye's
default filenames start with a per-job counter that resets each session, so
sorting by filename can interleave different days' files; sorting by
capture time (see capture_time_of()) avoids that. This mirrors how
Picturae's own 2023 delivery data for this collection groups sheets under a
folder purely by capture-time adjacency, not by anything read off the
scanner in real time.

See notes/species-tagging-options.txt for the full design discussion behind
this Folder-ID grouping.
"""

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from steps.convert import tiff_to_jp2
from steps.qr_read import decode_codes, classify_code, FOLDER, SHEET
from steps.register import register_sheet, register_folder_marker
from steps.state import FolderState
from steps.validate import validate_jp2

load_dotenv()


def load_config(config_path: Path = Path("config.yml")) -> dict:
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # IMAGE_STORAGE_DIR sets just the two paths that actually belong
    # together on the image storage server (done_jp2_dir is herbarium-
    # platform's own IMAGE_DATA_PATH tree, named Delivery there too;
    # done_jp2_lossless_dir is a separate top-level folder there for the
    # archival copy). The individual *_DIR env vars below still take
    # precedence over this, for a one-off override.
    image_storage_dir = os.getenv("IMAGE_STORAGE_DIR")
    if image_storage_dir:
        base = Path(image_storage_dir)
        config["done_jp2_dir"] = str(base / "Delivery")
        config["done_jp2_lossless_dir"] = str(base / "archive")

    # LOCAL_STATE_DIR sets the three paths that belong together on whatever
    # machine ingest.py itself runs on (TIFF is temporary, not the
    # long-term archive; error/log are operational state tied to the
    # running code, not the image archive). Same precedence as
    # IMAGE_STORAGE_DIR above.
    local_state_dir = os.getenv("LOCAL_STATE_DIR")
    if local_state_dir:
        base = Path(local_state_dir)
        config["done_tif_dir"] = str(base / "tif")
        config["error_dir"] = str(base / "error")
        config["log_dir"] = str(base / "logs")

    env_overrides = {
        "inbox_dir":              "INBOX_DIR",
        "done_jp2_dir":           "DONE_JP2_DIR",
        "done_jp2_lossless_dir":  "DONE_JP2_LOSSLESS_DIR",
        "done_tif_dir":           "DONE_TIF_DIR",
        "error_dir":              "ERROR_DIR",
        "log_dir":                "LOG_DIR",
    }
    for key, env_var in env_overrides.items():
        if os.getenv(env_var):
            config[key] = os.getenv(env_var)
    return config


_FILENAME_TIMESTAMP_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")


def capture_time_of(tiff_path: Path) -> datetime:
    """When BookEye actually wrote this file — prefer the timestamp
    embedded in its own filename (e.g. "..._2026-09-21_11-11-18.tif") over
    file mtime, since mtime doesn't survive every copy/backup/sync, but a
    filename does. Falls back to mtime if the filename doesn't have this
    pattern (not guaranteed for every BookEye naming/Job Mode setting)."""
    m = _FILENAME_TIMESTAMP_RE.search(tiff_path.stem)
    if m:
        year, month, day, hour, minute, second = map(int, m.groups())
        return datetime(year, month, day, hour, minute, second)
    return datetime.fromtimestamp(tiff_path.stat().st_mtime)


def dated_path(base_dir: Path, capture_time: datetime, filename: str) -> Path:
    """base_dir/YYYY/MM/DD/filename, by capture date — creates the date
    subdirectory if needed. Keeps any one directory from growing unbounded
    as the collection scales, and matches how IMAGE_DATA_PATH is organized
    for IIIF serving (see notes/scan-ingest-pipeline.txt)."""
    subdir = base_dir / f"{capture_time:%Y}" / f"{capture_time:%m}" / f"{capture_time:%d}"
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / filename


def setup_dirs(config: dict) -> dict[str, Path]:
    dirs = {
        "inbox":             Path(config["inbox_dir"]),
        "done_jp2":          Path(config["done_jp2_dir"]),
        "done_jp2_lossless": Path(config["done_jp2_lossless_dir"]),
        "done_tif":          Path(config["done_tif_dir"]),
        "error":             Path(config["error_dir"]),
        "logs":              Path(config["log_dir"]),
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def process_folder_marker(tiff_path: Path, folder_id: str, dirs: dict[str, Path],
                          state: FolderState, log_path: Path, csv_path: Path,
                          capture_time: datetime, config: dict, barcodes: list[str]) -> bool:
    """Handle a Folder-ID label image: convert to JP2, file away, update state.

    Folder markers go through the same convert+validate steps as sheets —
    they're real scanned images too, matching how Picturae's own delivery
    data for this collection treats Folder rows (see
    notes/species-tagging-options.txt). Lossy view only, no lossless
    archival copy, and the TIFF is discarded rather than kept in done_tif:
    a folder cover is an administrative label, not the specimen being
    preserved, so there's nothing worth keeping full-resolution once the
    IIIF view JP2 exists — see steps/convert.py for the lossless path
    sheets use, and their own full done_tif retention.
    """
    named_tiff = tiff_path.with_name(f"{folder_id}.tif")
    jp2_path = tiff_path.with_suffix(".jp2")
    try:
        tiff_path.rename(named_tiff)

        print("  Converting to JP2...")
        tiff_to_jp2(named_tiff, jp2_path, config)
        named_jp2 = jp2_path.with_name(f"{folder_id}.jp2")
        jp2_path.rename(named_jp2)
        jp2_path = named_jp2

        print("  Validating...")
        validate_jp2(jp2_path, config)

        final_jp2_path = dated_path(dirs["done_jp2"], capture_time, jp2_path.name)

        register_folder_marker(
            folder_id, jp2_path, log_path, csv_path,
            capture_time, final_jp2_path.as_posix(), barcodes=barcodes,
        )
        shutil.move(str(jp2_path), final_jp2_path)
        # Only discard the TIFF once the JP2 is safely in its final place —
        # never delete it as a side effect of a failed/partial conversion.
        named_tiff.unlink()

        state.set_current_folder(folder_id)
        print(f"  Folder marker: {folder_id} "
              f"(subsequent sheets will be tagged with this folder)")
        return True
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        for leftover in (tiff_path, named_tiff, jp2_path):
            if leftover.exists():
                shutil.move(str(leftover), dirs["error"] / leftover.name)
        return False


def process_sheet(tiff_path: Path, accession_id: str, dirs: dict[str, Path],
                  state: FolderState, log_path: Path, csv_path: Path,
                  capture_time: datetime, config: dict, barcodes: list[str]) -> bool:
    """Handle an ordinary specimen sheet: convert, validate, register, file away.

    Produces two JP2s: a lossy view (done_jp2_dir, the IIIF-served copy) and a
    lossless archival copy (done_jp2_lossless_dir) — see steps/convert.py.
    """
    jp2_path = tiff_path.with_suffix(".jp2")
    lossless_path = tiff_path.with_name(f"{tiff_path.stem}_lossless.jp2")
    try:
        print("  Converting to JP2 (lossy)...")
        tiff_to_jp2(tiff_path, jp2_path, config)
        named_jp2 = jp2_path.with_name(f"{accession_id}.jp2")
        jp2_path.rename(named_jp2)
        jp2_path = named_jp2

        print("  Converting to JP2 (lossless)...")
        tiff_to_jp2(tiff_path, lossless_path, config, lossless=True)
        named_lossless = lossless_path.with_name(f"{accession_id}_lossless.jp2")
        lossless_path.rename(named_lossless)
        lossless_path = named_lossless
        print(f"  Renamed to: {jp2_path.name} / {lossless_path.name}")

        print("  Validating...")
        validate_jp2(jp2_path, config)
        lossless_cfg = {**config, "validation": config.get("validation_lossless", config["validation"])}
        validate_jp2(lossless_path, lossless_cfg)

        folder_id = state.current_folder_id
        final_jp2_path = dated_path(dirs["done_jp2"], capture_time, jp2_path.name)
        final_lossless_path = dated_path(dirs["done_jp2_lossless"], capture_time, f"{accession_id}.jp2")
        print(f"  Registering (folder: {folder_id})...")
        register_sheet(
            accession_id, folder_id, jp2_path, log_path, csv_path,
            capture_time, final_jp2_path.as_posix(), barcodes=barcodes,
        )

        # Sheets keep the four-letter .tiff extension so they're visually
        # distinct from Folder-ID label images (.tif) at a glance, e.g. in
        # error/ if something fails — see notes/species-tagging-options.txt.
        named_tiff = tiff_path.with_name(f"{accession_id}.tiff")
        tiff_path.rename(named_tiff)
        shutil.move(str(jp2_path), final_jp2_path)
        shutil.move(str(lossless_path), final_lossless_path)
        # Only discard the TIFF once both JP2s are safely in their final
        # place — never delete it as a side effect of a failed/partial
        # conversion. Kept by default (tiff.keep in config.yml); the
        # lossless JP2 is pixel-identical to it, so once that's confirmed
        # written, the TIFF is redundant to actually delete.
        if config.get("tiff", {}).get("keep", True):
            shutil.move(str(named_tiff), dated_path(dirs["done_tif"], capture_time, named_tiff.name))
        else:
            named_tiff.unlink()
        print(f"  Done: {accession_id}")
        return True

    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        for leftover in (jp2_path, lossless_path, tiff_path, tiff_path.with_name(f"{accession_id}.tiff")):
            if leftover.exists():
                shutil.move(str(leftover), dirs["error"] / leftover.name)
        return False


def process_file(tiff_path: Path, dirs: dict[str, Path], state: FolderState,
                 log_path: Path, csv_path: Path, config: dict) -> bool:
    print(f"Processing: {tiff_path.name}")
    # Capture time = when BookEye actually wrote this file, not when ingest
    # happens to process it later — see capture_time_of(). Read this before
    # any renaming, and reuse it as the CSV's Creation Time (matching what
    # that column means in Picturae's own data).
    capture_time = capture_time_of(tiff_path)

    try:
        code, barcodes = decode_codes(tiff_path)
        kind = classify_code(code)
    except ValueError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        shutil.move(str(tiff_path), dirs["error"] / tiff_path.name)
        return False

    if barcodes:
        print(f"  Barcode(s) found: {', '.join(barcodes)}")

    if kind == FOLDER:
        return process_folder_marker(tiff_path, code, dirs, state, log_path,
                                      csv_path, capture_time, config, barcodes)
    else:
        assert kind == SHEET
        return process_sheet(tiff_path, code, dirs, state, log_path, csv_path,
                              capture_time, config, barcodes)


def main() -> None:
    config = load_config()
    dirs = setup_dirs(config)
    date_str = datetime.now().strftime("%Y%m%d")
    log_path = dirs["logs"] / f"ingest_{date_str}.tsv"
    csv_path = dirs["logs"] / f"filemaker_import_{date_str}.csv"
    state = FolderState(dirs["logs"] / "current_folder_state.txt")

    tiffs = list(dirs["inbox"].glob("*.tif")) + list(dirs["inbox"].glob("*.tiff"))
    # Sort by capture time (see capture_time_of()), not filename — BookEye's
    # default filenames start with a per-job counter that resets each
    # session, so filename order does not reliably reflect scan order
    # across sessions.
    tiffs.sort(key=capture_time_of)

    if not tiffs:
        print("No TIFF files found in inbox.")
        return

    print(f"Found {len(tiffs)} file(s) to process. "
          f"Starting folder: {state.current_folder_id}\n")
    ok = fail = 0
    for tiff in tiffs:
        if process_file(tiff, dirs, state, log_path, csv_path, config):
            ok += 1
        else:
            fail += 1

    print(f"\nDone. {ok} succeeded, {fail} failed. "
          f"Ending folder: {state.current_folder_id}")
    if fail:
        print(f"Failed files moved to: {dirs['error']}")


if __name__ == "__main__":
    main()
