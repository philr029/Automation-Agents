"""
File tools.

Every function here takes `config` as its last argument. The model never
sees or supplies it — `ToolRegistry.bind(config=...)` injects it, and the
`@tool` decorator skips it when building the schema. That is how the
sandbox root reaches the tools without the model being able to change it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentkit.safety import guard_write, resolve_in_workspace
from agentkit.tools import tool
from core.config import Config

# Extensions we will read as text. Anything else is reported as binary
# rather than dumped as mojibake into the model's context.
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".py", ".js", ".ts", ".html", ".css",
    ".sql", ".sh", ".xml", ".eml", ".rst",
}


@tool
def list_files(config: Config, subdirectory: str = ".", pattern: str = "*") -> str:
    """List files in the workspace with their sizes.

    subdirectory: folder to list, relative to the workspace root
    pattern: glob to match, e.g. '*.pdf'; use '**/*' to recurse
    """
    root = resolve_in_workspace(config, subdirectory)
    if not root.is_dir():
        return f"{subdirectory} is not a directory."

    rows = []
    for path in sorted(root.glob(pattern)):
        if path.is_file():
            rows.append(f"{path.relative_to(config.workspace)}\t{path.stat().st_size} bytes")
        elif path.is_dir():
            rows.append(f"{path.relative_to(config.workspace)}/\t(directory)")

    return "\n".join(rows) if rows else f"No entries matching {pattern!r} in {subdirectory}."


@tool
def read_file(config: Config, path: str, max_chars: int = 20000) -> str:
    """Read a text file from the workspace.

    path: file to read, relative to the workspace root
    max_chars: truncate after this many characters to protect the context window
    """
    target = resolve_in_workspace(config, path)
    if not target.is_file():
        return f"No such file: {path}"

    if target.suffix.lower() not in TEXT_SUFFIXES:
        return (
            f"{path} has extension {target.suffix!r}, which is not a known text "
            f"format ({target.stat().st_size} bytes). Skipping to avoid binary junk."
        )

    # `errors="replace"` means one bad byte degrades a character rather than
    # aborting the whole read.
    content = target.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return content[:max_chars] + f"\n\n[truncated — file is {len(content)} chars]"
    return content


@tool
def write_file(config: Config, path: str, content: str) -> str:
    """Create or overwrite a text file in the workspace.

    path: destination, relative to the workspace root
    content: the full text to write
    """
    target = resolve_in_workspace(config, path)

    blocked = guard_write(config, f"write {len(content)} chars to {path}")
    if blocked:
        return blocked

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}."


@tool
def move_file(config: Config, source: str, destination: str) -> str:
    """Move or rename a file inside the workspace.

    source: current path, relative to the workspace root
    destination: new path, relative to the workspace root
    """
    # Both ends are resolved through the sandbox, so a file cannot be moved
    # out of the workspace any more than it could be written outside it.
    src = resolve_in_workspace(config, source)
    dst = resolve_in_workspace(config, destination)

    if not src.exists():
        return f"No such file: {source}"
    if dst.exists():
        return f"Refusing to overwrite existing file at {destination}."

    blocked = guard_write(config, f"move {source} -> {destination}")
    if blocked:
        return blocked

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {source} to {destination}."


@tool
def make_directory(config: Config, path: str) -> str:
    """Create a folder (and any missing parents) in the workspace.

    path: folder to create, relative to the workspace root
    """
    target = resolve_in_workspace(config, path)

    blocked = guard_write(config, f"create directory {path}")
    if blocked:
        return blocked

    target.mkdir(parents=True, exist_ok=True)
    return f"Created directory {path}."


@tool
def search_files(config: Config, query: str, pattern: str = "**/*") -> str:
    """Find which files contain a piece of text (case-insensitive).

    query: the text to look for
    pattern: glob limiting which files are searched
    """
    needle = query.lower()
    hits: list[str] = []

    for path in sorted(config.workspace.glob(pattern)):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                rel = path.relative_to(config.workspace)
                hits.append(f"{rel}:{number}: {line.strip()[:150]}")
                break  # one hit per file keeps the result readable

    return "\n".join(hits) if hits else f"No files contain {query!r}."
