#!/usr/bin/env python3
"""Jämför JP2-kompression av BookEye-TIFF vid olika nivåer.

Kör:  python scripts/psnr_test.py <bild1.tif> <bild2.tif> ...
Utan argument används previous/*.tif som fallback (går att köra från valfri
katalog, sökvägar är relativa till repo-roten).

För varje TIFF och varje kompressionsnivå skrivs en JP2 till psnr_out/, och
storlek + PSNR mot käll-TIFF:en rapporteras. Syftet är att hitta en nivå som
ger Picturae-liknande kvalitet/storlek för BookEye-materialet.

Picturae-referens (GB-0500017): ~12:1, ~17 MB, och vår omkodning låg på ~49 dB
mot deras data. 40+ dB = visuellt omärkbar skillnad.

Riktiga BookEye-TIFF:ar finns i:
  260601-Resumed-scanning/bookeye/*.tif
"""

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from steps.convert import tiff_to_jp2

# Kompressionsnivåer att testa, som bits-per-pixel. För 24-bit RGB blir
# ratio ≈ 24 / bpp:  3.0→8:1  2.4→10:1  2.0→12:1 (Picturae)  1.5→16:1
# 1.2→20:1  1.0→24:1
RATES_BPP = [3.0, 2.4, 2.0, 1.7, 1.5, 1.2, 1.0]

config = yaml.safe_load(open(REPO_ROOT / "config.yml"))
out_dir = REPO_ROOT / "psnr_out"
out_dir.mkdir(exist_ok=True)


def psnr(src: np.ndarray, enc: np.ndarray) -> float:
    diff = src.astype(np.int16) - enc.astype(np.int16)
    mse = float(np.mean(diff.astype(np.float32) ** 2))
    return 10 * math.log10(255 ** 2 / mse) if mse > 0 else float("inf")


def decode_jp2(jp2: Path) -> np.ndarray:
    """Avkoda JP2 via opj_decompress (samma toolchain som kodningen)."""
    tmp = jp2.with_suffix(".decoded.tif")
    subprocess.run(["opj_decompress", "-i", str(jp2), "-o", str(tmp)],
                   check=True, capture_output=True)
    try:
        return np.array(Image.open(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    tiffs = [Path(a) for a in sys.argv[1:]]
    if not tiffs:
        tiffs = sorted((REPO_ROOT / "previous").glob("*.tif"))
        print("Inga argument – använder previous/*.tif. "
              "Ange BookEye-TIFF:ar som argument för det riktiga testet.\n")
    tiffs = [t for t in tiffs if t.exists()]
    if not tiffs:
        print("Hittade inga TIFF-filer.")
        return

    for tif in tiffs:
        print(f"=== {tif.name} ===")
        src = np.array(Image.open(tif))
        h, w = src.shape[:2]
        raw_mb = w * h * 3 / (1024 * 1024)
        print(f"  {w}×{h}, okomprimerat ~{raw_mb:.0f} MB")
        print(f"  {'bpp':>4}  {'ratio':>6}  {'MB':>6}  {'PSNR dB':>8}")

        for bpp in RATES_BPP:
            cfg = {**config, "jp2": {**config["jp2"], "rate": bpp}}
            jp2 = out_dir / f"{tif.stem}_bpp{bpp:g}.jp2"
            try:
                tiff_to_jp2(tif, jp2, cfg)
            except Exception as e:
                print(f"  {bpp:4.1f}  FEL: {e}")
                continue
            size_mb = jp2.stat().st_size / (1024 * 1024)
            db = psnr(src, decode_jp2(jp2))
            print(f"  {bpp:4.1f}  {raw_mb / size_mb:5.0f}:1  {size_mb:6.1f}  {db:8.1f}")
        print()

    print(f"JP2-filer sparade i {out_dir}/ för visuell granskning.")
    print("Picturae-referens: ~12:1, ~17 MB. 40+ dB = visuellt omärkbar skillnad.")


if __name__ == "__main__":
    main()
