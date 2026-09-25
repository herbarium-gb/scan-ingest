"""Resolve configured paths the same way wherever a script is started from."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_path(value: str | Path) -> Path:
    """An absolute path; a relative one (from config.yml or .env) counts
    from the repo root, not the current directory — so a run from cron or
    another directory writes to the same place as one from the repo.
    "~" is expanded too, which .env files don't do themselves."""
    return (REPO_ROOT / Path(value).expanduser()).resolve()
