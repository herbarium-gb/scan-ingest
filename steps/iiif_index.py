"""Look up whether an image ID is already published in the IIIF viewer.

herbarium-platform's viewer finds each image through small JSON "shard"
files, <HERBARIUM_PLATFORM_DIR>/viewer/<SHARD_TARGET>/idx/<shard key>.json,
each mapping image IDs to the directory (relative to the image root) the
JP2 lives in, e.g. {"GB-0577660": "2023/05/12"}. ingest.py consults them
before filing anything, so a rescan of an already-published sheet or
folder label (e.g. one Picturae already delivered) never replaces the
published image — rebuilding the shards for the new date folder would
otherwise silently repoint the viewer at the new file.
"""

import json
import os
from pathlib import Path

from steps.paths import repo_path


def shard_key_for(image_id: str) -> str:
    """Must match shard_key_for() in herbarium-platform's
    viewer/scripts/build_shards.py (and shardKeyFor() in the viewer)."""
    if image_id.startswith("GB-Folder_"):
        return image_id[:14]  # "GB-Folder_" (10 chars) + 4 digits
    return image_id[:7]       # "GB-" (3 chars) + 4 digits


def idx_dir() -> Path | None:
    """The shard directory, or None if HERBARIUM_PLATFORM_DIR isn't set."""
    platform_dir = os.getenv("HERBARIUM_PLATFORM_DIR")
    if not platform_dir:
        return None
    target = os.getenv("SHARD_TARGET", "prod")
    return repo_path(platform_dir) / "viewer" / target / "idx"


def published_dir(image_id: str) -> str | None:
    """Directory the image is currently published under ("2023/05/12"), or
    None if it isn't published (or there's no shard index to check)."""
    d = idx_dir()
    if d is None:
        return None
    shard = d / f"{shard_key_for(image_id)}.json"
    if not shard.exists():
        return None
    with open(shard, encoding="utf-8") as f:
        return json.load(f).get(image_id)
