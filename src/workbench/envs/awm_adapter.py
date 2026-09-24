"""The only place that knows how to call AWM (rule R4 references in docs/RECON.md).

- DB creation uses AWM's Python interface ``awm.core.reset.reset_single_database``
  (third_party/agent-world-model/awm/core/reset.py:53-100).
- The MCP server has to run as a subprocess: ``awm.core.server.run_server`` blocks in
  ``os.system(...)`` (awm/core/server.py:163). We invoke ``python -m awm.core.server``
  (server.py:202-205) and ALWAYS pass ``--db_path``, ``--temp_server_path`` and
  ``--output_dir`` (Config fields at server.py:13-26); otherwise AWM writes a temp file next
  to the dataset's gen_envs.jsonl (server.py:134-138). See docs/DECISIONS.md ADR-011.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def build_session_db(dataset_dir: Path, scenario: str, dest_dir: Path) -> Path:
    """Create a fresh SQLite DB for ``scenario`` inside ``dest_dir`` and return its path."""
    from awm.core.reset import reset_single_database  # lazy: heavy import chain

    dest_dir.mkdir(parents=True, exist_ok=True)
    created = reset_single_database(
        input_db=str(dataset_dir / "gen_db.jsonl"),
        input_sample=str(dataset_dir / "gen_sample.jsonl"),
        scenario=scenario,
        database_dir=str(dest_dir),
    )
    return Path(created)


def copy_db(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def server_command(
    *,
    dataset_dir: Path,
    scenario: str,
    db_path: Path,
    host: str,
    port: int,
    temp_server_path: Path,
    output_dir: Path,
    python: str = sys.executable,
) -> list[str]:
    return [
        python,
        "-m",
        "awm.core.server",
        "--scenario",
        scenario,
        "--envs_load_path",
        str(dataset_dir / "gen_envs.jsonl"),
        "--db_path",
        str(db_path),
        "--host",
        host,
        "--port",
        str(port),
        "--temp_server_path",
        str(temp_server_path),
        "--output_dir",
        str(output_dir),
    ]
