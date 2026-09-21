"""The previous attempt's records, moved aside before this one writes its own."""

import shutil
from pathlib import Path


def next_attempt_archive(directory: Path) -> Path | None:
    """A fresh `attempt-<n>/` subdirectory at the next free index, or None
    when it cannot be made — the caller then deletes the records instead."""
    index = 1
    while (directory / f"attempt-{index}").exists():
        index += 1
    archive = directory / f"attempt-{index}"
    try:
        archive.mkdir()
    except OSError:
        return None
    return archive


def clear_previous_attempt(directory: Path) -> None:
    """The fallback ladder re-invokes this fan-out into the SAME dir. A shard
    dying before its redirects run would otherwise leave the PREVIOUS
    attempt's records in place, fabricating a success for the aggregator.
    Moving records into `attempt-<n>/` makes "an attempt reports only its own
    result" a property of the directory: `Path.glob` does not recurse, so the
    aggregator never sees the archive, while the archived logs still ride the
    published artifact as the ONLY surviving record of a superseded failure.
    Everything except an existing archive moves, rather than a list of the name
    shapes this run mints: a list has to be extended by whoever adds the next
    artifact kind, and the one that is forgotten is invisible — the stale file is
    read as this attempt's answer and the run publishes it. A record that cannot
    be MOVED is deleted instead — this step tolerates leftover state and must not
    be killed by it.
    """
    stale_records = [
        path for path in directory.iterdir() if not path.name.startswith("attempt-")
    ]
    if not stale_records:
        return
    archive = next_attempt_archive(directory)
    for stale in stale_records:
        if archive is not None:
            try:
                # Moves the link itself, never follows it.
                shutil.move(str(stale), str(archive / stale.name))
                continue
            except OSError:
                pass
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            stale.unlink(missing_ok=True)
