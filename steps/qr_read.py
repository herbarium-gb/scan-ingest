"""Read and classify QR codes from scanned images.

Two kinds of QR code can appear on a scanned TIFF:

- A Folder-ID label (e.g. "GB-Folder_0036571"), scanned as the first image of
  a physical folder's batch. See notes/species-tagging-options.txt in the
  260601-Resumed-scanning project folder for the full design rationale — this
  mirrors how Picturae's own 2023 delivery data for this collection is
  structured (Object Type "Folder" vs "Sheet", grouped by capture order).
- A specimen's own accession ID (e.g. "GB-0523177"), on an ordinary sheet.

QR vs. barcode meaning (Bengt, 2026-09-11): the QR code always identifies the
SHEET/IMAGE. A sheet can additionally carry one or more barcodes, and each
barcode identifies one "kollekt" (a specific field-collection event of a plant
specimen) mounted on that sheet — several collects can share one sheet, hence
several barcodes. If a sheet has NO barcode, its QR number does double duty:
it identifies both the sheet and the single collect on it. (Rare case: one
collect split across several physical sheets — each sheet still gets its own
QR; there is no shared code linking them.) Number ranges are a real content
signal, not just a label-format quirk: barcodes 1-499999, QR codes from
500000 up (upper barcode bound to be confirmed by Claes) — confirmed against
Picturae's real delivery CSV, where every non-empty "Original Barcode" value
is < 500000 and every "Specimen QR" value is >= 500000. Norwegian specimens
can carry a colliding number with a "GU[N]-" prefix instead — corrected
downstream by Roger's FileMaker script, not handled here.
"""

import re
from pathlib import Path

from PIL import Image
import pyzbar.pyzbar as pyzbar

FOLDER_ID_RE = re.compile(r"^GB-Folder_\d+$")
ACCESSION_ID_RE = re.compile(r"^GB-\d+$")

FOLDER = "folder"
SHEET = "sheet"


def decode_codes(image_path: Path) -> tuple[str, list[str]]:
    """Decode every code on a scanned image; return (qr_code, barcodes).

    barcodes holds zero or more "kollekt" IDs (see module docstring) found
    alongside the QR code — never used for FOLDER/SHEET classification
    (classify_code only looks at the QR code) and never required.

    Handles a QR code that encodes a full URL (e.g.
    https://botmus.gu.se/GB-1155799) by taking the last path segment.
    Raises ValueError if zero or more than one QR code is found.
    """
    with Image.open(image_path) as img:
        decoded = pyzbar.decode(img)

    qrcodes = [
        d.data.decode("utf-8", errors="replace").rstrip("/").split("/")[-1]
        for d in decoded
        if d.type == "QRCODE"
    ]
    barcodes = [
        d.data.decode("utf-8", errors="replace")
        for d in decoded
        if d.type != "QRCODE"
    ]

    if len(qrcodes) == 0:
        raise ValueError(f"No QR code found in {image_path.name}")
    if len(qrcodes) > 1:
        raise ValueError(f"Multiple QR codes in {image_path.name}: {qrcodes}")

    return qrcodes[0], barcodes


def decode_qr(image_path: Path) -> str:
    """Backwards-compatible: return just the QR code text, no barcodes."""
    code, _barcodes = decode_codes(image_path)
    return code


def classify_code(code: str) -> str:
    """Classify a decoded QR code as FOLDER or SHEET.

    Raises ValueError if the code matches neither known pattern, so an
    unexpected code format fails loudly (-> error/) instead of silently
    being mis-filed as one or the other.
    """
    if FOLDER_ID_RE.match(code):
        return FOLDER
    if ACCESSION_ID_RE.match(code):
        return SHEET
    raise ValueError(
        f"QR code '{code}' doesn't match a known folder-ID or accession-ID pattern"
    )


def read_qr(image_path: Path) -> str:
    """Backwards-compatible helper: return the accession ID of a specimen sheet.

    Raises ValueError if the image's QR code turns out to be a Folder-ID
    label rather than a specimen accession.
    """
    code = decode_qr(image_path)
    if classify_code(code) != SHEET:
        raise ValueError(
            f"{image_path.name}: expected a specimen accession QR, got a "
            f"folder-ID code ({code})"
        )
    return code
