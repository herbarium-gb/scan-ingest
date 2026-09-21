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
sorting by filename can interleave different days' files; sorting by file
modification time avoids that. This mirrors how Picturae's own 2023 delivery
data for this collection groups sheets under a folder purely by capture-time
adjacency (see notes/species-tagging-options.txt in 260601-Resumed-scanning),
not by anything read off the scanner in real time.

See notes/species-tagging-options.txt (260601-Resumed-scanning project
folder) for the full design discussion behind this Folder-ID grouping.
"""

import os
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

    # DATA_DIR sets all six paths at once, under this fixed layout. Any of
    # the individual *_DIR env vars below still takes precedence over it, so
    # a single directory can be redirected without abandoning DATA_DIR for
    # the rest.
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        base = Path(data_dir)
        config["inbox_dir"] = str(base / "upload")
        config["done_jp2_dir"] = str(base / "done" / "jp2")
        config["done_tif_dir"] = str(base / "done" / "tif")
        config["error_dir"] = str(base / "error")
        config["log_dir"] = str(base / "logs")

    env_overrides = {
        "inbox_dir":     "INBOX_DIR",
        "done_jp2_dir":  "DONE_JP2_DIR",
        "done_tif_dir":  "DONE_TIF_DIR",
        "error_dir":     "ERROR_DIR",
        "log_dir":       "LOG_DIR",
    }
    for key, env_var in env_overrides.items():
        if os.getenv(env_var):
            config[key] = os.getenv(env_var)
    return config


def setup_dirs(config: dict) -> dict[str, Path]:
    dirs = {
        "inbox":    Path(config["inbox_dir"]),
        "done_jp2": Path(config["done_jp2_dir"]),
        "done_tif": Path(config["done_tif_dir"]),
        "error":    Path(config["error_dir"]),
        "logs":     Path(config["log_dir"]),
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
    notes/species-tagging-options.txt).
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

        final_tiff_path = dirs["done_tif"] / named_tiff.name
        final_jp2_path = dirs["done_jp2"] / jp2_path.name

        register_folder_marker(
            folder_id, jp2_path, log_path, csv_path,
            capture_time, final_jp2_path.as_posix(), barcodes=barcodes,
        )
        shutil.move(str(named_tiff), final_tiff_path)
        shutil.move(str(jp2_path), final_jp2_path)

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
    """Handle an ordinary specimen sheet: convert, validate, register, file away."""
    jp2_path = tiff_path.with_suffix(".jp2")
    try:
        print("  Converting to JP2...")
        tiff_to_jp2(tiff_path, jp2_path, config)

        named_jp2 = jp2_path.with_name(f"{accession_id}.jp2")
        jp2_path.rename(named_jp2)
        jp2_path = named_jp2
        print(f"  Renamed to: {jp2_path.name}")

        print("  Validating...")
        validate_jp2(jp2_path, config)

        folder_id = state.current_folder_id
        final_jp2_path = dirs["done_jp2"] / jp2_path.name
        print(f"  Registering (folder: {folder_id})...")
        register_sheet(
            accession_id, folder_id, jp2_path, log_path, csv_path,
            capture_time, final_jp2_path.as_posix(), barcodes=barcodes,
        )

        # Sheets keep the four-letter .tiff extension so they're visually
        # distinct from Folder-ID label images (.tif) at a glance — see
        # notes/species-tagging-options.txt.
        named_tiff = tiff_path.with_name(f"{accession_id}.tiff")
        tiff_path.rename(named_tiff)
        shutil.move(str(jp2_path), final_jp2_path)
        shutil.move(str(named_tiff), dirs["done_tif"] / named_tiff.name)
        print(f"  Done: {accession_id}")
        return True

    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        for leftover in (jp2_path, tiff_path, tiff_path.with_name(f"{accession_id}.tiff")):
            if leftover.exists():
                shutil.move(str(leftover), dirs["error"] / leftover.name)
        return False


def process_file(tiff_path: Path, dirs: dict[str, Path], state: FolderState,
                 log_path: Path, csv_path: Path, config: dict) -> bool:
    print(f"Processing: {tiff_path.name}")
    # Capture time = the TIFF's own file time, i.e. when BookEye actually
    # wrote it — not when ingest happens to process it later. Read this
    # before any renaming, and reuse it as the CSV's Creation Time (matching
    # what that column means in Picturae's own data).
    capture_time = datetime.fromtimestamp(tiff_path.stat().st_mtime)

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
    # Sort by capture (modification) time, not filename — BookEye's default
    # filenames start with a per-job counter that resets each session, so
    # filename order does not reliably reflect scan order across sessions.
    tiffs.sort(key=lambda p: p.stat().st_mtime)

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
