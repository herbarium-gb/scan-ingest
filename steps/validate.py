"""Validate a JP2 file meets minimum quality requirements."""

from pathlib import Path
from PIL import Image


def validate_jp2(jp2_path: Path, config: dict) -> None:
    """Raise ValueError if the file does not meet validation thresholds."""
    v = config.get("validation", {})
    min_w = v.get("min_width_px", 4000)
    min_h = v.get("min_height_px", 6000)
    min_mb = v.get("min_file_size_mb", 5)
    max_mb = v.get("max_file_size_mb", 100)

    size_mb = jp2_path.stat().st_size / (1024 * 1024)
    if size_mb < min_mb:
        raise ValueError(f"{jp2_path.name}: file too small ({size_mb:.1f} MB < {min_mb} MB)")
    if size_mb > max_mb:
        raise ValueError(f"{jp2_path.name}: file too large ({size_mb:.1f} MB > {max_mb} MB)")

    with Image.open(jp2_path) as img:
        w, h = img.size

    if w < min_w or h < min_h:
        raise ValueError(
            f"{jp2_path.name}: resolution too low ({w}×{h} px, "
            f"minimum {min_w}×{min_h})"
        )
