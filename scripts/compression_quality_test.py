#!/usr/bin/env python3
"""Compare JP2 compression levels on a batch of BookEye TIFFs.

For each TIFF, encodes a lossless JP2 and a lossy one at each ratio in
RATIOS into compression_quality_out/, plus a collage of 100 % crops from
the most detailed parts of the sheet with a column per level, so the
levels can be compared by eye. Ends with summary.txt: per level, the file
size and the lowest quality on any sheet in the batch (PSNR, see psnr()
below), with a plain-language verdict — a level only counts as safe if it
is safe for every sheet.

Run (from the repo root, with the scan-ingest environment active):
  python scripts/compression_quality_test.py <image1.tif> <image2.tif> ...
e.g. python scripts/compression_quality_test.py ~/scans/*.tif

Takes about a minute and ~140 MB of disk per image. Files from an earlier
run with the same image names are overwritten.
"""

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from steps.convert import tiff_to_jp2  # noqa: E402

Image.MAX_IMAGE_PIXELS = None  # full-size scans exceed Pillow's default guard

# Lossy levels to test, as compression ratios (uncompressed : JP2). Each
# step halves the file size, down to where loss starts to show. Beyond
# ~100:1 the encoder breaks down rather than degrading gracefully: with
# 1024 px tiles, a tile's share of the budget no longer covers its headers
# and decodes as flat grey.
RATIOS = [12, 24, 48, 96]
CROP = 500     # crop side, in pixels at 100 %
N_CROPS = 3    # crops per sheet
THRESHOLD_DB = 40  # at or above: the eye can't tell it from the original

config = yaml.safe_load(open(REPO_ROOT / "config.yml"))
CURRENT = round(24 / config["jp2"]["rate"])  # config's rate in bpp -> ratio
out_dir = REPO_ROOT / "compression_quality_out"
font = ImageFont.load_default(size=28)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    """Peak signal-to-noise ratio in dB: how far b's pixels are from a's.
    Computed in row chunks to spare memory on full-size scans."""
    se = 0.0
    for y0 in range(0, a.shape[0], 1000):
        d = a[y0:y0 + 1000].astype(np.int16) - b[y0:y0 + 1000].astype(np.int16)
        se += float(np.square(d, dtype=np.float64).sum())
    mse = se / a.size
    return 10 * math.log10(255 ** 2 / mse) if mse else float("inf")


def pick_crops(src: np.ndarray) -> list[tuple[int, int]]:
    """Top-left corners of the N_CROPS most detailed, non-overlapping
    CROP-sized blocks — plain paper is smooth, plant and text are not, so
    the crops land on the specimen and its labels."""
    g = src.mean(axis=2)
    grad = np.zeros_like(g)
    grad[:, 1:] += np.abs(np.diff(g, axis=1))
    grad[1:, :] += np.abs(np.diff(g, axis=0))
    h, w = g.shape
    step = CROP // 2
    cands = sorted(((grad[y:y + CROP, x:x + CROP].mean(), x, y)
                    for y in range(0, h - CROP, step)
                    for x in range(0, w - CROP, step)), reverse=True)
    picked = []
    for _, x, y in cands:
        if all(abs(x - px) >= CROP or abs(y - py) >= CROP for px, py in picked):
            picked.append((x, y))
        if len(picked) == N_CROPS:
            break
    return picked


def size_label(mb: float, ratio: float) -> str:
    return f"{mb:.0f} MB ({ratio:.0f}:1)"


def decode_jp2(jp2: Path) -> np.ndarray:
    """Decode via opj_decompress (the same toolchain used to encode)."""
    tmp = jp2.with_suffix(".decoded.tif")
    subprocess.run(["opj_decompress", "-i", str(jp2), "-o", str(tmp)],
                   check=True, capture_output=True)
    try:
        return np.array(Image.open(tmp).convert("RGB"))
    finally:
        tmp.unlink(missing_ok=True)


def process(tif: Path) -> list[tuple]:
    """Encode one sheet at every level and save its crop collage. Returns
    one row per level for the summary — (ratio, MB, actual ratio, dB),
    ratio and dB None for the lossless copy."""
    src = np.array(Image.open(tif).convert("RGB"))
    h, w = src.shape[:2]
    raw = w * h * 3  # uncompressed bytes
    picked = pick_crops(src)

    def cut(a: np.ndarray) -> list[np.ndarray]:
        return [a[y:y + CROP, x:x + CROP] for x, y in picked]

    lossless = out_dir / f"{tif.stem}_lossless.jp2"
    tiff_to_jp2(tif, lossless, config, lossless=True)
    size = lossless.stat().st_size
    rows = [(None, size / 1e6, raw / size, None)]
    # The lossless JP2 has the original's exact pixels, so its crops are
    # taken straight from the source.
    crops = {f"{size_label(size / 1e6, raw / size)}, lossless": cut(src)}

    for r in RATIOS:
        jp2 = out_dir / f"{tif.stem}_{r}to1.jp2"
        # The encoder takes the ratio as a cap, so the actual one (from the
        # file size) can differ a little; that is what gets reported.
        tiff_to_jp2(tif, jp2, {**config, "jp2": {**config["jp2"], "rate": 24 / r}})
        a = decode_jp2(jp2)
        size = jp2.stat().st_size
        db = psnr(src, a)
        rows.append((r, size / 1e6, raw / size, db))
        label = size_label(size / 1e6, raw / size) + (", current" if r == CURRENT else "")
        crops[label] = cut(a)
        print(f"  {tif.name}: {size_label(size / 1e6, raw / size)}, {db:.1f} dB", flush=True)

    # A row per crop, a column per level, the level's label on top.
    label_h, gap = 44, 12
    sheet = Image.new("RGB", (len(crops) * (CROP + gap) - gap,
                              label_h + len(picked) * (CROP + gap) - gap), "white")
    d = ImageDraw.Draw(sheet)
    for j, (name, tiles) in enumerate(crops.items()):
        ox = j * (CROP + gap)
        d.text((ox + 4, 6), name, fill="black", font=font)
        for i, tile in enumerate(tiles):
            sheet.paste(Image.fromarray(tile), (ox, label_h + i * (CROP + gap)))
    sheet.save(out_dir / f"{tif.stem}_crops.png")
    return rows


def summarize(results: dict[str, list[tuple]]) -> None:
    """Print and save summary.txt: one line per level across the batch."""
    n = len(results)
    lines = [f"=== Summary: {n} sheet(s) ===",
             f"{'Level':<16} {'File size':<12} {'Lowest quality':<16} Verdict"]
    safe = []
    for k, r in enumerate([None, *RATIOS]):
        level = [rows[k] for rows in results.values()]
        mbs = [mb for _, mb, _, _ in level]
        lo, hi = f"{min(mbs):.0f}", f"{max(mbs):.0f}"
        size = f"{lo} MB" if lo == hi else f"{lo}-{hi} MB"
        if r is None:
            lines.append(f"{'lossless':<16} {size:<12} {'identical':<16} archive copy")
            continue
        ratio = sum(ratio for _, _, ratio, _ in level) / n
        name = f"~{ratio:.0f}:1" + (", current" if r == CURRENT else "")
        worst = min(db for _, _, _, db in level)
        ok = worst >= THRESHOLD_DB
        if ok:
            safe.append((name, size))
        verdict = ("no visible difference on any sheet" if ok
                   else "may be visible on some sheet")
        lines.append(f"{name:<16} {size:<12} {f'{worst:.1f} dB':<16} {verdict}")

    lines.append("")
    if safe:
        name, size = safe[-1]
        lines.append(f"Smallest level with no visible difference on any sheet: "
                     f"{name} ({size}).")
    else:
        lines.append("Every compressed level may show a visible difference on some sheet.")
    lines += [
        "",
        "Picturae reference: their delivered JP2s for this collection are",
        "about 17 MB (12:1). Example GB-0500017: 6949 x 10362 px, 16.9 MB,",
        "12.2:1. Their encoding settings (Kakadu, rate 2.0) match the",
        "current setting here.",
        "",
        f"{THRESHOLD_DB} dB or more = the eye can't tell it from the original.",
        "A measure, not a verdict: check the *_crops.png collages by eye.",
    ]
    report = "\n".join(lines)
    print("\n" + report)
    (out_dir / "summary.txt").write_text(report + "\n", encoding="utf-8")


def main() -> None:
    tiffs = [Path(a) for a in sys.argv[1:] if Path(a).exists()]
    if not tiffs:
        sys.exit(__doc__)
    out_dir.mkdir(exist_ok=True)
    results = {}
    for tif in tiffs:
        results[tif.name] = process(tif)
    summarize(results)
    print(f"\nJP2s, crop collages and summary.txt are in {out_dir}/")


if __name__ == "__main__":
    main()
