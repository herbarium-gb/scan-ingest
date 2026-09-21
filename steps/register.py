"""Register a processed image: a TSV batch log, and a CSV for FileMaker import.

The FileMaker CSV deliberately reuses Picturae's own column names (see
picturae/163-Gothenburg-GB-Herbarium-Digitization-*.csv in the
260601-Resumed-scanning project folder) so downstream tooling — FileMaker
import scripts, anything that already knows this header — handles both eras
of data the same way. Most taxonomy columns are left blank at ingest time:
species determination happens later, by an expert, joined on Folder QR (see
notes/species-tagging-options.txt) — only Object Type, Folder QR, the
Specimen QR fields, Original Barcode, Creation Time and Original File Pathway
are filled in.

This CSV is the FileMaker integration path (replaces the old unimplemented
FileMaker Data API placeholder) — matches the original plan in
notes/scan-ingest-pipeline.txt: "Append a row to a CSV for import into
FileMaker".
"""

import csv
from datetime import datetime
from pathlib import Path

_LOG_HEADER = ["Timestamp", "Kind", "ID", "FolderID", "Filename", "SizeMB", "Barcodes"]

_FILEMAKER_CSV_HEADER = [
    "Object Type", "Folder QR", "Specimen QR URL", "Specimen QR",
    "Original Barcode", "Creation Time", "Original File Pathway",
    "Powo Taxon ID", "Family", "Genus", "Species", "Rank 1", "Epithet 1",
    "Rank 2", "Epithet 2", "Hybrid", "Hybrid Level", "Hybrid Genus",
    "Hybrid Species", "Hybrid Rank 1", "Hybrid Epithet 1", "Authorship",
    "Geographic Region",
]


def register_sheet(accession_id: str, folder_id: str, jp2_path: Path, log_path: Path,
                    csv_path: Path, capture_time: datetime, file_pathway: str,
                    barcodes: list[str] | None = None) -> None:
    """Register a specimen sheet: TSV log row + FileMaker-import CSV row."""
    _append_log(log_path, kind="SHEET", ident=accession_id, folder_id=folder_id,
                filename=jp2_path.name, size_mb=_size_mb(jp2_path), barcodes=barcodes)
    _append_filemaker_csv(
        csv_path,
        object_type="Sheet",
        folder_qr=folder_id,
        # Specimen QR URL left blank: it's just "https://botmus.gu.se/" +
        # Specimen QR, so writing both would duplicate the same value twice
        # rather than add information. Derive it from Specimen QR if needed.
        specimen_qr_url="",
        specimen_qr=accession_id,
        creation_time=capture_time,
        file_pathway=file_pathway,
        barcodes=barcodes,
    )


def register_folder_marker(folder_id: str, tiff_path: Path, log_path: Path,
                            csv_path: Path, capture_time: datetime,
                            file_pathway: str, barcodes: list[str] | None = None) -> None:
    """Register a Folder-ID label image: TSV log row + FileMaker-import CSV row."""
    _append_log(log_path, kind="FOLDER", ident=folder_id, folder_id=folder_id,
                filename=tiff_path.name, size_mb=_size_mb(tiff_path), barcodes=barcodes)
    _append_filemaker_csv(
        csv_path,
        object_type="Folder",
        folder_qr=folder_id,
        specimen_qr_url="",
        specimen_qr="",
        creation_time=capture_time,
        file_pathway=file_pathway,
        barcodes=barcodes,
    )


def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _append_log(log_path: Path, *, kind: str, ident: str, folder_id: str,
                 filename: str, size_mb: float, barcodes: list[str] | None = None) -> None:
    write_header = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        if write_header:
            writer.writerow(_LOG_HEADER)
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            kind,
            ident,
            folder_id,
            filename,
            f"{size_mb:.1f}",
            _join_barcodes(barcodes),
        ])


def _append_filemaker_csv(csv_path: Path, *, object_type: str, folder_qr: str,
                           specimen_qr_url: str, specimen_qr: str,
                           creation_time: datetime, file_pathway: str,
                           barcodes: list[str] | None = None) -> None:
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)  # comma-delimited, matching Picturae's own export
        if write_header:
            writer.writerow(_FILEMAKER_CSV_HEADER)
        row = dict.fromkeys(_FILEMAKER_CSV_HEADER, "")
        row["Object Type"] = object_type
        row["Folder QR"] = folder_qr
        row["Specimen QR URL"] = specimen_qr_url
        row["Specimen QR"] = specimen_qr
        row["Original Barcode"] = _join_barcodes(barcodes)
        row["Creation Time"] = _format_creation_time(creation_time)
        row["Original File Pathway"] = file_pathway
        writer.writerow([row[h] for h in _FILEMAKER_CSV_HEADER])


def _join_barcodes(barcodes: list[str] | None) -> str:
    # "|"-joined (matches how the earlier qr-rename script recorded multiple
    # codes on one sheet). More than one barcode is a real, expected case: a
    # sheet can carry several "kollekt" mounted together, each with its own
    # barcode — see the QR-vs-barcode note in steps/qr_read.py.
    return "|".join(barcodes) if barcodes else ""


def _format_creation_time(dt: datetime) -> str:
    # Matches Picturae's own timestamp style exactly, e.g. "2023:01:23 16:20:03.785"
    return dt.strftime("%Y:%m:%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
