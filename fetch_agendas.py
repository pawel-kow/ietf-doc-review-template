#!/usr/bin/env python3
"""Fetch IETF WG meeting agendas and the documents they reference.

For every working group listed in a YAML config this script:
  1. downloads the WG agenda for the given IETF meeting,
  2. splits it into sections (Markdown headings, top-level numbered items),
  3. finds all referenced Internet-Drafts (and RFCs linked by URL) and
     downloads them with `ietf-reviewtool fetch` into a subfolder per section,
  4. writes <output_dir>/<meeting>/<WG>/agenda.md with those references
     re-linked to the local .txt files.

Config (YAML):

    meeting: 127
    output_dir: IETF          # optional; "{meeting}" is substituted
    fetch_args: []            # optional extra args for `ietf-reviewtool fetch`
    wgs:
      - REGEXT
      - RPP

Usage:
    python fetch_agendas.py [config.yaml] [--force] [--wg RPP ...]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

DATATRACKER = "https://datatracker.ietf.org"
PROCEEDINGS = "https://www.ietf.org/proceedings"
USER_AGENT = "ietf-agenda-fetcher/1.0"

# A draft name, optionally followed by a two-digit revision.
DRAFT_RE = re.compile(r"\b(draft-[a-z0-9]+(?:-[a-z0-9]+)*)", re.IGNORECASE)
REV_SUFFIX_RE = re.compile(r"-(\d{2})$")
# URLs that point at a draft or RFC (datatracker, rfc-editor, archive, tools).
URL_RE = re.compile(r"(?:https?://|(?<![\w/])/doc/)[^\s<>()\[\]]+", re.IGNORECASE)
URL_DOC_RE = re.compile(r"(draft-[a-z0-9-]*[a-z0-9]|rfc\d+)", re.IGNORECASE)
URL_DOC_HOSTS = ("datatracker.ietf.org", "rfc-editor.org", "ietf.org/archive",
                 "tools.ietf.org", "ietf.org/rfc", "ietf.org/id")
# Existing Markdown constructs that must be treated as one unit:
#   [label](target) | <https://...> | [draft-name] (shortcut reference)
MD_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(([^)\s]*)\)"
    r"|<(https?://[^>]+)>"
    r"|\[(draft-[a-z0-9-]+)\](?![(\[:])",
    re.IGNORECASE,
)

# Agenda structure.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
NUMBERED_RE = re.compile(r"^ {0,3}(\d{1,2})[.)]\s+(\S.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
NUMBERED_LEVEL = 10  # numbered items nest below any heading level

Resolver = Callable[[str], "str | None"]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def http_get(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def fetch_agenda(meeting: str, wg: str) -> tuple[str, str, str]:
    """Return (text, format, source_url). format is 'md', 'txt' or other ext."""
    name = f"agenda-{meeting}-{wg.lower()}"

    # Preferred: ask the datatracker API for the uploaded source file and
    # fetch it raw from the proceedings archive (keeps Markdown as Markdown).
    try:
        meta, _ = http_get(f"{DATATRACKER}/api/v1/doc/document/{name}/?format=json")
        uploaded = json.loads(meta).get("uploaded_filename") or ""
        if uploaded:
            url = f"{PROCEEDINGS}/{meeting}/agenda/{uploaded}"
            body, _ = http_get(url)
            ext = uploaded.rsplit(".", 1)[-1].lower()
            return body.decode("utf-8", errors="replace"), ext, url
    except (urllib.error.URLError, ValueError) as exc:
        print(f"  ! API lookup failed ({exc}); falling back to materials URL")

    # Fallback: the materials URL (serves text agendas as-is, renders others).
    url = f"{DATATRACKER}/meeting/{meeting}/materials/{name}"
    body, ctype = http_get(url)
    fmt = "txt" if ctype.startswith("text/plain") else "html"
    return body.decode("utf-8", errors="replace"), fmt, url


def fetch_documents(names: list[str], dest: Path, extra_args: list[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tool = shutil.which("ietf-reviewtool") or str(Path(sys.executable).parent / "ietf-reviewtool")
    cmd = [tool, "fetch", *extra_args, *names]
    print(f"  [{dest}] $ ietf-reviewtool {' '.join(cmd[1:])}")
    result = subprocess.run(cmd, cwd=dest)
    if result.returncode != 0:
        print(f"  ! ietf-reviewtool exited with {result.returncode}")


# --------------------------------------------------------------------------- #
# Agenda structure
# --------------------------------------------------------------------------- #

def slugify(title: str, max_len: int = 40) -> str:
    title = re.sub(r"<[^>]+>", "", title)                  # HTML tags
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", title)     # (3 min), [10 mins]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "section"


def section_paths(lines: list[str]) -> list[tuple[str, ...]]:
    """Return, for each line, the section folder path it belongs to.

    Markdown headings and top-level numbered items ("1. Title") open sections;
    numbered items nest under the current heading. A single H1 is treated as
    the document title and does not create a folder.
    """
    in_fence = False
    h1_count = 0
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (m := HEADING_RE.match(line)) and len(m.group(1)) == 1:
            h1_count += 1

    stack: list[tuple[int, str]] = []
    counters: dict[tuple[str, ...], int] = {}
    paths: list[tuple[str, ...]] = []
    in_fence = False

    def push(level: int, name: str | None, title: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = tuple(n for _, n in stack)
        if name is None:
            counters[parent] = counters.get(parent, 0) + 1
            name = f"{counters[parent]:02d}"
        stack.append((level, f"{name}-{slugify(title)}"))

    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            if m := HEADING_RE.match(line):
                level = len(m.group(1))
                if not (level == 1 and h1_count == 1):
                    push(level, None, m.group(2))
            elif m := NUMBERED_RE.match(line):
                push(NUMBERED_LEVEL, f"{int(m.group(1)):02d}", m.group(2))
        paths.append(tuple(n for _, n in stack))
    return paths


# --------------------------------------------------------------------------- #
# Reference extraction / relinking
# --------------------------------------------------------------------------- #

def doc_from_url(url: str) -> str | None:
    if not (url.startswith("/doc/") or any(h in url.lower() for h in URL_DOC_HOSTS)):
        return None
    m = URL_DOC_RE.search(url)
    return normalize(m.group(1)) if m else None


def normalize(name: str) -> str:
    """Lower-case and strip a trailing revision number from a draft name."""
    name = name.lower().rstrip("-.")
    if name.startswith("draft-"):
        name = REV_SUFFIX_RE.sub("", name)
    return name


def relink(text: str, resolve: Resolver) -> str:
    """Replace draft/RFC references with links to resolve(doc).

    resolve() returns a local path, or None to leave the reference as is.
    It is called for every reference found, so it can also collect them.
    """

    def link_url(url: str, label: str | None = None) -> str | None:
        doc = doc_from_url(url)
        target = resolve(doc) if doc else None
        return f"[{label or doc}]({target})" if target else None

    def names(segment: str) -> str:
        def repl(m: re.Match) -> str:
            target = resolve(normalize(m.group(1)))
            return f"[{m.group(1)}]({target})" if target else m.group(0)
        return DRAFT_RE.sub(repl, segment)

    def plain(segment: str) -> str:
        # Bare URLs first, then bare draft names in what remains.
        out, pos = [], 0
        for m in URL_RE.finditer(segment):
            out.append(names(segment[pos:m.start()]))
            url = m.group(0).rstrip(".,;:")
            tail = m.group(0)[len(url):]
            out.append((link_url(url) or url) + tail)
            pos = m.end()
        out.append(names(segment[pos:]))
        return "".join(out)

    out, pos = [], 0
    for m in MD_LINK_RE.finditer(text):
        out.append(plain(text[pos:m.start()]))
        if m.group(3):                                   # <https://...>
            out.append(link_url(m.group(3)) or m.group(0))
        else:                                            # [label](target) / [draft]
            label = m.group(1) if m.group(4) is None else m.group(4)
            target = m.group(2) if m.group(4) is None else m.group(4)
            doc = doc_from_url(target) or (normalize(target) if DRAFT_RE.fullmatch(target) else None)
            local = resolve(doc) if doc else None
            out.append(f"[{label}]({local})" if local else m.group(0))
        pos = m.end()
    out.append(plain(text[pos:]))
    return "".join(out)


def local_file(name: str, folder: Path) -> str | None:
    """Return the newest local .txt file for a draft/RFC name, if any."""
    if (folder / f"{name}.txt").exists():
        return f"{name}.txt"
    candidates = sorted(folder.glob(f"{name}-[0-9][0-9].txt"))
    return candidates[-1].name if candidates else None


def text_to_markdown(text: str) -> str:
    """Make a plain-text agenda render sensibly as Markdown.

    Leading spaces become non-breaking spaces (so indented lines are not turned
    into code blocks, where links would not render) and every line ends with a
    hard line break so the original layout is preserved.
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        lines.append(" " * indent + stripped + ("  " if stripped else ""))
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def process_wg(meeting: str, wg: str, out_root: Path, extra_args: list[str], force: bool) -> None:
    print(f"== IETF {meeting} {wg}")
    wg_dir = out_root / wg
    wg_dir.mkdir(parents=True, exist_ok=True)

    text, fmt, source = fetch_agenda(meeting, wg)
    print(f"  agenda: {source} ({fmt})")
    if fmt == "txt":
        text = text_to_markdown(text)
    elif fmt != "md":
        print(f"  ! agenda format '{fmt}' is stored as-is; links may not render")

    lines = text.split("\n")
    paths = section_paths(lines)

    # Pass 1: collect references; each document goes to the first section
    # that mentions it.
    home: dict[str, tuple[str, ...]] = {}
    for line, path in zip(lines, paths):
        relink(line, lambda doc, p=path: home.setdefault(doc, p) and None)
    if not home:
        print("  no referenced documents")

    by_section: dict[tuple[str, ...], list[str]] = {}
    for doc, path in home.items():
        by_section.setdefault(path, []).append(doc)

    for path, docs in by_section.items():
        folder = wg_dir.joinpath(*path)
        print(f"  {'/'.join(path) or '.'}: {', '.join(docs)}")
        missing = [d for d in docs if force or not local_file(d, folder)]
        if missing:
            fetch_documents(missing, folder, extra_args)

    # Pass 2: rewrite references to point at the local files.
    local: dict[str, str] = {}
    for doc, path in home.items():
        if f := local_file(doc, wg_dir.joinpath(*path)):
            local[doc] = "/".join((*path, f))
        else:
            print(f"  ! no local file for {doc}; left unlinked")

    body = "\n".join(relink(line, local.get) for line in lines)
    header = f"<!-- Source: {source} -->\n\n"
    (wg_dir / "agenda.md").write_text(header + body, encoding="utf-8")
    print(f"  wrote {wg_dir / 'agenda.md'} ({len(local)}/{len(home)} linked)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", default="meetings.yaml", type=Path)
    ap.add_argument("--wg", action="append", help="only process these WGs (repeatable)")
    ap.add_argument("--force", action="store_true", help="re-fetch documents even if present")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text())
    meeting = str(cfg["meeting"])
    out_root = Path(str(cfg.get("output_dir", "IETF")).format(meeting=meeting)) / meeting
    extra_args = [str(a) for a in cfg.get("fetch_args", [])]
    wgs = [str(w) for w in cfg["wgs"]]
    if args.wg:
        wanted = {w.lower() for w in args.wg}
        wgs = [w for w in wgs if w.lower() in wanted]

    failed = []
    for wg in wgs:
        try:
            process_wg(meeting, wg, out_root, extra_args, args.force)
        except urllib.error.HTTPError as exc:
            print(f"  ! failed to download agenda: {exc}")
            failed.append(wg)
    if failed:
        print(f"Failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
