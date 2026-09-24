"""Process inspection helpers (Linux /proc)."""

from __future__ import annotations

from pathlib import Path


def live_group_members(pgid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """PIDs in process group ``pgid`` that are still alive (zombies excluded)."""
    live: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # /proc/<pid>/stat: "pid (comm) state ppid pgrp ..." — comm may contain spaces.
        fields = stat[stat.rfind(")") + 2 :].split()
        state, pgrp = fields[0], int(fields[2])
        if pgrp == pgid and state != "Z":
            live.append(int(entry.name))
    return live
