"""Convert TIFF to JP2 using opj_compress."""

import subprocess
from pathlib import Path

from PIL import Image


def _bpp_to_ratio(tiff_path: Path, rate_bpp: float) -> int:
    """Convert Kakadu-style bits-per-pixel rate to an opj_compress compression ratio."""
    with Image.open(tiff_path) as img:
        mode_bpp = {"1": 1, "L": 8, "P": 8, "RGB": 24, "RGBA": 32, "CMYK": 32,
                    "I": 32, "F": 32, "I;16": 16, "I;16B": 16}
        bit_depth = mode_bpp.get(img.mode, 24)
    return max(1, round(bit_depth / rate_bpp))


def tiff_to_jp2(tiff_path: Path, jp2_path: Path, config: dict) -> None:
    jp2_cfg = config.get("jp2", {})
    rate_bpp = jp2_cfg.get("rate", 2.0)
    resolutions = jp2_cfg.get("resolutions", 8)
    progression = jp2_cfg.get("progression", "RPCL")
    block_size = jp2_cfg.get("block_size", [64, 64])
    tile_size = jp2_cfg.get("tile_size")

    precinct_sizes = jp2_cfg.get("precinct_sizes", [])
    precinct_str = ",".join(f"[{w},{h}]" for w, h in precinct_sizes)

    ratio = _bpp_to_ratio(tiff_path, rate_bpp)

    cmd = [
        "opj_compress",
        "-i", str(tiff_path),
        "-o", str(jp2_path),
        "-p", progression,
        "-n", str(resolutions),
        "-r", str(ratio),
        "-I",
        "-SOP",
        "-PLT",
        "-b", f"{block_size[0]},{block_size[1]}",
        "-TP", "R",
    ]
    if precinct_str:
        cmd += ["-c", precinct_str]
    if tile_size:
        cmd += ["-t", f"{tile_size[0]},{tile_size[1]}"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"opj_compress failed for {tiff_path.name}: {result.stderr.strip()}")
