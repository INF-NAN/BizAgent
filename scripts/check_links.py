#!/usr/bin/env python3
"""Check that every relative link in README.md and docs/**/*.md resolves (`make lint`, CI).

    uv run python scripts/check_links.py              # README.md + docs/**/*.md
    uv run python scripts/check_links.py FILE ...     # only these files

Checks markdown links and images, reference definitions and the src/href of HTML tags. A
relative target must exist inside the repository; a #fragment pointing into a markdown file
must name one of its headings, slugged the way GitHub does it (lowercase; punctuation and
symbols dropped, letters of any script kept; spaces become hyphens; repeats get -1, -2, ...),
or an HTML id/name. External links (any URL scheme) are not fetched. Code blocks, inline
code and HTML comments are skipped. Standard library only. Exit code 1 if a link is broken.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
LABEL = r"(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*"
DEST = r"<[^<>\n]*>|(?:[^\s()]|\([^\s()]*\))*"
TITLE = r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?"
INLINE = re.compile(rf"!?\[(?P<label>{LABEL})\]\(\s*(?P<dest>{DEST}){TITLE}\s*\)")
REF_DEF = re.compile(r"^ {0,3}\[(?P<label>[^\]^][^\]]*)\]:\s*(?P<dest><[^<>\n]*>|\S+)")
TAG = re.compile(r"<(?:a|img|source)\b[^>]*>", re.I)
TAG_LINK = re.compile(r"\b(?:src|href)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
TAG_ID = re.compile(r"<[a-z][^>]*?\b(?:id|name)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class Broken:
    file: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.target} — {self.reason}"


def closes(line: str, fence: str) -> bool:
    """A closing fence: the opening fence's character, at least as long, nothing after it."""
    m = FENCE.match(line)
    return (
        m is not None
        and m.group(1)[0] == fence[0]
        and len(m.group(1)) >= len(fence)
        and not line[m.end() :].strip()
    )


def unfenced(text: str) -> list[str]:
    """The file's lines, with fenced code blocks (fences included) blanked out."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if fence is None:
            m = FENCE.match(line)
            if m is None:
                out.append(line)
                continue
            fence = m.group(1)
        elif closes(line, fence):
            fence = None
        out.append("")
    return out


def prose_lines(text: str) -> list[str]:
    """unfenced(), with inline code and HTML comments (which may span lines) removed as well."""
    out: list[str] = []
    in_comment = False
    for line in unfenced(text):
        rest, kept = CODE_SPAN.sub("", line), ""
        while rest:
            if in_comment:
                end = rest.find("-->")
                in_comment, rest = (True, "") if end < 0 else (False, rest[end + 3 :])
            else:
                start = rest.find("<!--")
                if start < 0:
                    kept, rest = kept + rest, ""
                else:
                    kept, rest, in_comment = kept + rest[:start], rest[start + 4 :], True
        out.append(kept)
    return out


def heading_text(raw: str) -> str:
    """What GitHub renders for a heading's inline markup, roughly: text without the markup."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)  # images add no text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their label
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("*", "")
    return re.sub(r"(?<![\w])__?(\S(?:.*?\S)?)__?(?![\w])", r"\1", text)


def slug(text: str) -> str:
    kept = (c for c in text.lower() if c in " -_" or unicodedata.category(c)[0] in "LMN")
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Heading slugs (with GitHub's -1, -2 suffixes for repeats) and HTML ids of a markdown file."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for line in unfenced(path.read_text(encoding="utf-8")):
        h = HEADING.match(line)
        if h:
            base = slug(heading_text(h.group(1) or ""))
            n = seen.get(base, 0)
            seen[base] = n + 1
            found.add(base if n == 0 else f"{base}-{n}")
        for a, b in TAG_ID.findall(line):
            found.add((a or b).lower())
    return found


def links(line: str) -> list[str]:
    """Link destinations on one prose line, including links nested in a link's label."""
    found: list[str] = []
    for m in INLINE.finditer(line):
        found.append(m.group("dest"))
        found.extend(links(m.group("label")))
    ref = REF_DEF.match(line)
    if ref:
        found.append(ref.group("dest"))
    for tag in TAG.findall(line):
        found.extend(a or b for a, b in TAG_LINK.findall(tag))
    return [d[1:-1] if d.startswith("<") and d.endswith(">") else d for d in found]


def check_file(path: Path, root: Path, cache: dict[Path, set[str]]) -> tuple[int, list[Broken]]:
    rel = path.relative_to(root).as_posix()
    broken: list[Broken] = []
    checked = 0

    def anchor_ok(target: Path, fragment: str) -> bool:
        if target not in cache:
            cache[target] = anchors(target)
        return unquote(fragment).lower() in cache[target]

    for n, line in enumerate(prose_lines(path.read_text(encoding="utf-8")), start=1):
        for dest in links(line):
            if not dest or SCHEME.match(dest) or dest.startswith("//"):
                continue  # empty, or external: not fetched
            checked += 1
            target_part, _, fragment = dest.partition("#")
            target_part = unquote(target_part.split("?", 1)[0])
            if not target_part:
                if fragment and not anchor_ok(path, fragment):
                    broken.append(Broken(rel, n, dest, "no such heading or id in this file"))
                continue
            base = root if target_part.startswith("/") else path.parent
            target = (base / target_part.lstrip("/")).resolve()
            if not target.is_relative_to(root):
                broken.append(Broken(rel, n, dest, "points outside the repository"))
            elif not target.exists():
                broken.append(Broken(rel, n, dest, "file not found"))
            elif fragment and target.suffix == ".md" and target.is_file() and not anchor_ok(target, fragment):
                broken.append(Broken(rel, n, dest, f"no such heading or id in {target.relative_to(root)}"))
    return checked, broken


def default_files(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check relative links in README.md and docs/**/*.md.")
    ap.add_argument("files", nargs="*", type=Path, help="markdown files (default: README.md, docs/**/*.md)")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    files = [f.resolve() for f in args.files] or [f for f in default_files(root) if f.exists()]
    cache: dict[Path, set[str]] = {}
    checked, broken = 0, []
    for f in files:
        n, bad = check_file(f, root, cache)
        checked += n
        broken.extend(bad)
    for b in broken:
        print(b)
    print(f"checked {checked} relative links in {len(files)} files; {len(broken)} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
