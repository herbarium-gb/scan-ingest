"""Persisted "current folder" state, carried across ingest runs.

Ingest typically runs in batches (e.g. nightly per infrastructure-and-ops.txt),
so a folder's Folder-ID label image and the sheets that follow it aren't
guaranteed to land in the same run — a folder's label might be processed
tonight and its last few sheets only arrive (and get processed) tomorrow, if
scanning and transfer overlap with a run. The current folder therefore needs
to survive across runs, not just within one — hence a tiny file on disk
rather than an in-memory variable.
"""

from pathlib import Path

UNASSIGNED = "UNASSIGNED"  # sentinel: no Folder-ID label has been seen yet


class FolderState:
    """Tracks which Folder-ID group newly-processed sheets currently belong to."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.current_folder_id = self._load()

    def _load(self) -> str:
        if self.state_path.exists():
            value = self.state_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        return UNASSIGNED

    def set_current_folder(self, folder_id: str) -> None:
        self.current_folder_id = folder_id
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(folder_id, encoding="utf-8")
