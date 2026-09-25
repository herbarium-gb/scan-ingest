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
column names (see steps/register.py) — most columns stay blank; taxonomy
is transcribed from the scanned folder during registration.

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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from steps.convert import tiff_to_jp2
from steps.filemaker import FileMakerClient, FileMakerError, CREATED
from steps.iiif_index import idx_dir, published_dir
from steps.paths import REPO_ROOT, repo_path
from steps.qr_read import decode_codes, classify_code, FOLDER, SHEET
from steps.register import register_sheet, register_folder_marker
from steps.state import FolderState, UNASSIGNED
from steps.validate import validate_jp2

load_dotenv(REPO_ROOT / ".env")


def load_config(config_path: Path = REPO_ROOT / "config.yml") -> dict:
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
    for key in env_overrides:
        config[key] = str(repo_path(config[key]))
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
                          capture_time: datetime, config: dict, barcodes: list[str],
                          error_log: Path) -> bool:
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
        log_error(error_log, tiff_path.name, str(e))
        for leftover in (tiff_path, named_tiff, jp2_path):
            if leftover.exists():
                move_to_error(leftover, dirs, error_log)
        return False


def process_sheet(tiff_path: Path, accession_id: str, dirs: dict[str, Path],
                  state: FolderState, log_path: Path, csv_path: Path,
                  capture_time: datetime, config: dict, barcodes: list[str],
                  error_log: Path) -> bool:
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
        log_error(error_log, tiff_path.name, str(e))
        for leftover in (jp2_path, lossless_path, tiff_path, tiff_path.with_name(f"{accession_id}.tiff")):
            if leftover.exists():
                move_to_error(leftover, dirs, error_log)
        return False


def log_error(error_log: Path, filename: str, msg: str) -> None:
    """Print a file's failure reason and keep it in error_log — the file
    itself just lands in error/, which doesn't say why."""
    print(f"  ERROR: {msg}", file=sys.stderr)
    try:
        with open(error_log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{filename}\t{msg}\n")
    except OSError as e:  # e.g. the log's disk is full; the message is printed above
        print(f"  (could not write {error_log.name}: {e})", file=sys.stderr)


def move_to_error(path: Path, dirs: dict[str, Path], error_log: Path) -> None:
    """Move a failed file into error/, never overwriting an earlier one
    with the same name (BookEye's per-job counter repeats filenames). If
    the move itself fails — e.g. a full disk when error/ is on another
    filesystem than the inbox, so the move is really a copy — the file
    stays where it is, any partial copy is removed, and the run goes on."""
    dest = dirs["error"] / path.name
    n = 1
    while dest.exists():
        dest = dirs["error"] / f"{path.stem}_{n}{path.suffix}"
        n += 1
    try:
        shutil.move(str(path), dest)
    except OSError as e:
        if path.exists() and dest.exists():
            dest.unlink()
        log_error(error_log, path.name, f"could not move to error/ ({e}); left at {path}")


def publish_conflict(image_id: str, kind: str, dirs: dict[str, Path],
                     capture_time: datetime) -> str | None:
    """Why filing this image would replace something already there, or
    None if it's safe. Checked before any conversion work.

    Two cases: the ID is already published in the IIIF viewer under a
    different date folder (e.g. a rescan of a sheet Picturae delivered —
    see steps/iiif_index.py), or a file with this name already sits in
    the destination folder. Either way nothing is overwritten; the TIFF
    goes to error/ for a person to decide about."""
    rel_dir = f"{capture_time:%Y}/{capture_time:%m}/{capture_time:%d}"
    try:
        existing = published_dir(image_id)
    except (OSError, ValueError) as e:  # unreadable/corrupt shard file
        return f"could not check the IIIF index for {image_id}: {e}"
    if existing is not None and existing != rel_dir:
        return f"A scan of {image_id} is already archived (under {existing}) — not replacing it"

    targets = [dirs["done_jp2"] / rel_dir / f"{image_id}.jp2"]
    if kind == SHEET:
        targets.append(dirs["done_jp2_lossless"] / rel_dir / f"{image_id}.jp2")
    for t in targets:
        if t.exists():
            return f"{t} already exists — not overwriting it"
    return None


def process_file(tiff_path: Path, dirs: dict[str, Path], state: FolderState,
                 log_path: Path, csv_path: Path, config: dict,
                 filed_sheets: list[tuple[str, str]], error_log: Path) -> bool:
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
        log_error(error_log, tiff_path.name, str(e))
        move_to_error(tiff_path, dirs, error_log)
        return False

    conflict = publish_conflict(code, kind, dirs, capture_time)
    if conflict:
        if kind == FOLDER:
            # The label still says which folder the following sheets
            # belong to — only its image isn't published again.
            state.set_current_folder(code)
            conflict += f"; {code} set as current folder anyway"
        log_error(error_log, tiff_path.name, conflict)
        move_to_error(tiff_path, dirs, error_log)
        return False

    if barcodes:
        print(f"  Barcode(s) found: {', '.join(barcodes)}")

    if kind == FOLDER:
        return process_folder_marker(tiff_path, code, dirs, state, log_path,
                                      csv_path, capture_time, config, barcodes,
                                      error_log)
    else:
        assert kind == SHEET
        ok = process_sheet(tiff_path, code, dirs, state, log_path, csv_path,
                           capture_time, config, barcodes, error_log)
        if ok:
            filed_sheets.append((code, state.current_folder_id))
        return ok


def create_filemaker_records(fm: FileMakerClient, sheets: list[tuple[str, str]],
                             warn_log: Path) -> None:
    """Best-effort: create each filed sheet's skeleton record in FileMaker
    (see steps/filemaker.py), in ascending Löpnr order rather than scan
    order — FileMaker shows unsorted records in creation order, and sorting
    the whole register on open is too slow, so the order has to be right
    from the start. (Only within one run: a later run's lower numbers still
    land after an earlier run's higher ones.)

    Never fails the run — the images are already safely in place, and a
    missing record can be created later, so a FileMaker/network error is
    logged to warn_log instead."""
    ordered = sorted(sheets, key=lambda s: int(s[0].removeprefix("GB-")))
    print(f"\nCreating FileMaker records ({fm.database}) for {len(ordered)} sheet(s)...")
    for accession_id, folder_id in ordered:
        # Blank rather than the UNASSIGNED placeholder, so it's never
        # mistaken for a real Folder-ID in FileMaker.
        if folder_id == UNASSIGNED:
            folder_id = ""
        try:
            result = fm.create_skeleton(accession_id, folder_id)
            print(f"  {accession_id}: {'created' if result == CREATED else 'already exists'}")
        except Exception as e:
            msg = f"FileMaker record not created for {accession_id} ({fm.database}): {e}"
            print(f"  WARNING: {msg}", file=sys.stderr)
            with open(warn_log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{msg}\n")


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

    # Checked before touching any file: a half-filled FM_* config is a
    # setup mistake to fix first, not something to warn about per sheet.
    try:
        fm = FileMakerClient.from_env()
    except FileMakerError as e:
        sys.exit(f"FileMaker config error: {e}")
    if fm is None:
        print("FM_BASE_URL not set — skipping FileMaker records.")
    fm_warn_log = dirs["logs"] / f"filemaker_warnings_{date_str}.log"
    error_log = dirs["logs"] / f"errors_{date_str}.log"
    if idx_dir() is None:
        print("HERBARIUM_PLATFORM_DIR not set — can't check for images already "
              "published in IIIF; only existing files are protected.")

    print(f"Found {len(tiffs)} file(s) to process. "
          f"Starting folder: {state.current_folder_id}\n")
    ok = fail = 0
    processed_dates = set()
    filed_sheets = []
    try:
        for tiff in tiffs:
            capture_date = capture_time_of(tiff).date()
            if process_file(tiff, dirs, state, log_path, csv_path, config,
                            filed_sheets, error_log):
                ok += 1
                processed_dates.add(capture_date)
            else:
                fail += 1
    finally:
        # Also after a crash mid-run: sheets already filed still get their
        # records.
        if fm:
            if filed_sheets:
                create_filemaker_records(fm, filed_sheets, fm_warn_log)
            fm.close()

    print(f"\nDone. {ok} succeeded, {fail} failed. "
          f"Ending folder: {state.current_folder_id}")
    if fail:
        print(f"Failed files moved to: {dirs['error']} (reasons in {error_log.name})")

    if processed_dates:
        trigger_shard_build(processed_dates, dirs["done_jp2"], dirs["logs"], date_str)


def trigger_shard_build(dates: set, image_root: Path, log_dir: Path,
                        date_str: str) -> None:
    """Best-effort: ask herbarium-platform to update its shards for each
    date touched this run, so newly-filed images become viewable without
    a manual step. Never fails the ingest run itself over this — a missed
    update is safe to redo later by rerunning build_shards.py by hand.
    Skipped entirely if HERBARIUM_PLATFORM_DIR isn't configured.

    IMAGE_DATA_PATH is passed explicitly as our own done_jp2_dir, so
    build_shards.py indexes exactly the tree we just wrote to, whatever
    herbarium-platform's own .env says (python-dotenv never overrides a
    variable already set in the environment).
    """
    platform_dir = os.getenv("HERBARIUM_PLATFORM_DIR")
    if not platform_dir:
        print("\nHERBARIUM_PLATFORM_DIR not set — skipping shard update.")
        return

    platform_dir = repo_path(platform_dir)
    target = os.getenv("SHARD_TARGET", "prod")
    script = platform_dir / "viewer" / "scripts" / "build_shards.py"
    warn_log = log_dir / f"shard_warnings_{date_str}.log"

    env = {**os.environ, "IMAGE_DATA_PATH": str(image_root.resolve())}

    print(f"\nUpdating shards ({target}) for {len(dates)} date(s)...")
    for d in sorted(dates):
        subpath = f"{d:%Y}/{d:%m}/{d:%d}"
        try:
            result = subprocess.run(
                [sys.executable, str(script), target, subpath],
                cwd=platform_dir, env=env, capture_output=True, text=True,
            )
            error = result.stderr.strip() if result.returncode else None
        except OSError as e:  # e.g. HERBARIUM_PLATFORM_DIR doesn't exist
            error = str(e)
        if error is None:
            print(f"  {subpath}: OK")
        else:
            msg = f"Shard build failed for {subpath} ({target}): {error}"
            print(f"  WARNING: {msg}", file=sys.stderr)
            with open(warn_log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{msg}\n")


if __name__ == "__main__":
    main()
