"""
A deliberately narrow shell.

Letting a model run arbitrary commands is the fastest way to turn a helpful
agent into a destructive one. So this toolkit exposes exactly one command —
`git` — and only its read-only subcommands. That is enough to summarise a
repository's history, and not enough to do damage.

If you widen this, widen it by adding entries to ALLOWED, never by removing
the check.
"""

from __future__ import annotations

import subprocess

from agentkit.safety import resolve_in_workspace
from agentkit.tools import tool
from core.config import Config

# Read-only git subcommands. Anything not in this set is refused.
ALLOWED = {"log", "diff", "status", "show", "branch", "shortlog", "blame", "tag"}


@tool
def git_command(config: Config, subcommand: str, arguments: str = "", repository: str = ".") -> str:
    """Run a read-only git command in a repository inside the workspace.

    subcommand: one of log, diff, status, show, branch, shortlog, blame, tag
    arguments: extra flags, e.g. '--oneline -20'
    repository: repo folder, relative to the workspace root
    """
    if subcommand not in ALLOWED:
        return f"{subcommand!r} is not permitted. Allowed: {', '.join(sorted(ALLOWED))}"

    workdir = resolve_in_workspace(config, repository)
    if not (workdir / ".git").exists():
        return f"{repository} is not a git repository."

    # `shell=False` with an argument list means the arguments are passed to
    # git directly and are never interpreted by a shell — no quoting bugs,
    # no command injection through `arguments`.
    command = ["git", "-C", str(workdir), subcommand, *arguments.split()]

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return "git command timed out after 30s."

    output = (completed.stdout + completed.stderr).strip()
    return output[:15000] if output else "(no output)"
