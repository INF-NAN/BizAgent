"""scripts/check_links.py: relative links in README.md and docs/**/*.md must resolve."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FENCE = "```"
README_OK = f"""\
# Demo

[![ci](https://example.com/badge.svg)](https://example.com/ci)
<img src="docs/assets/shot.png" width="400" alt="shot">
See [the guide](docs/guide.md "Guide"), [its section](docs/guide.md#11-未验证),
[a repeat](docs/guide.md#notes-1), [a directory](docs/assets/), [root-relative](/docs/guide.md),
[spaced](<docs/my notes.md>), [encoded](docs/my%20notes.md), [top](#demo),
[an id](docs/guide.md#custom-anchor), [mail](mailto:someone@example.com),
[an ADR](docs/guide.md#adr-028-任务书原文移入-docsprocess数字守卫按文件豁免).

[ref]: docs/guide.md

`[in code](docs/missing.md)` and <!-- [in a comment](docs/missing.md) --> stay unchecked
<!--
[in a multi-line comment](docs/missing.md)
-->

{FENCE}bash
[in a fence](docs/missing.md)
{FENCE}
"""
GUIDE = """\
# Guide

## 1.1 未验证

## Notes

## Notes

<a id="custom-anchor"></a>

## ADR-028 任务书原文移入 `docs/process/`，数字守卫按文件豁免

[back](../README.md#demo)
"""


def check(root: Path, *files: str) -> tuple[int, str]:
    argv = [sys.executable, "scripts/check_links.py", "--root", str(root), *files]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    return out.returncode, out.stdout + out.stderr


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def test_valid_links_pass_and_code_comments_and_fences_are_skipped(tmp_path: Path) -> None:
    write(tmp_path, "README.md", README_OK)
    write(tmp_path, "docs/guide.md", GUIDE)
    write(tmp_path, "docs/my notes.md", "# notes\n")
    (tmp_path / "docs" / "assets").mkdir()
    (tmp_path / "docs" / "assets" / "shot.png").write_bytes(b"\x89PNG")
    write(tmp_path, "other/x.md", "[not scanned](nope.md)\n")  # outside README.md and docs/
    rc, out = check(tmp_path)
    assert rc == 0, out
    # 12 in README (external links are not counted), 1 in docs/guide.md, none in "docs/my notes.md"
    assert out.strip() == "checked 13 relative links in 3 files; 0 broken"


def test_broken_links_are_reported_with_file_and_line(tmp_path: Path) -> None:
    write(
        tmp_path,
        "README.md",
        "# Demo\n"
        "[missing](docs/nope.md)\n"
        "[bad anchor](docs/guide.md#no-such-heading)\n"
        "[bad self anchor](#nowhere)\n"
        '<img src="docs/assets/missing.png" alt="x">\n'
        "[escape](../outside.md)\n"
        "[ref]: docs/gone.md\n",
    )
    write(tmp_path, "docs/guide.md", "# Guide\n\n[fine](../README.md#demo)\n")
    rc, out = check(tmp_path)
    assert rc == 1
    assert out.splitlines() == [
        "README.md:2: docs/nope.md — file not found",
        "README.md:3: docs/guide.md#no-such-heading — no such heading or id in docs/guide.md",
        "README.md:4: #nowhere — no such heading or id in this file",
        "README.md:5: docs/assets/missing.png — file not found",
        "README.md:6: ../outside.md — points outside the repository",
        "README.md:7: docs/gone.md — file not found",
        "checked 7 relative links in 2 files; 6 broken",
    ]
    rc, out = check(tmp_path, str(tmp_path / "docs" / "guide.md"))  # only the files given
    assert rc == 0 and out.strip() == "checked 1 relative links in 1 files; 0 broken"
