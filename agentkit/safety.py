"""
Guard rails.

An agent that can write files is an agent that can destroy files. Two
mechanisms keep that in check, and every tool that touches disk goes
through them:

  1. The sandbox. Every path is resolved and checked to be inside the
     configured workspace. Escapes via `..`, absolute paths, or symlinks
     are rejected before anything is opened.

  2. Dry run. When enabled, destructive tools return a description of what
     they *would* do and change nothing. This is the default, so a new
     agent's first run is always a rehearsal.
"""

from __future__ import annotations

from pathlib import Path

from core.config import Config


class SandboxViolation(PermissionError):
    """Raised when a tool is asked to touch something outside the workspace."""


def resolve_in_workspace(config: Config, candidate: str) -> Path:
    """
    Turn a model-supplied path string into a safe absolute Path.

    `Path.resolve()` collapses `..` segments *and* follows symlinks, so
    checking the resolved result is what actually closes both escape routes.
    Comparing with `relative_to` rather than string prefixes avoids the
    classic bug where `/work-evil` passes a `startswith("/work")` check.
    """
    root = config.workspace.resolve()
    target = Path(candidate).expanduser()
    target = target if target.is_absolute() else root / target
    target = target.resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise SandboxViolation(
            f"{candidate!r} is outside the workspace ({root}). "
            "Move the files you want processed into the workspace, or point "
            "AGENT_WORKSPACE at the directory you meant."
        ) from None

    return target


def guard_write(config: Config, action: str) -> str | None:
    """
    Check whether a destructive action may proceed.

    Returns None when the tool should go ahead, or a string to hand straight
    back to the model when it should not. Callers read as:

        blocked = guard_write(config, f"delete {path}")
        if blocked:
            return blocked
    """
    if config.dry_run:
        return f"[dry run] Would {action}. Re-run with --apply to do it for real."
    return None
