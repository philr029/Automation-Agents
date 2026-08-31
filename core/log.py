"""
Console output for agent runs.

Agents are only trustworthy if you can see what they did. Every tool call,
every result, and every model message goes through here, so a run reads like
a transcript rather than a black box.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

# ANSI colour codes, disabled automatically when output is piped to a file
# so log files don't fill up with escape sequences.
_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def banner(agent_name: str, task: str) -> None:
    """Announce the start of a run."""
    print()
    print(_c("1;36", f"┌─ {agent_name} "))
    print(_c("36", f"│  {task}"))
    print(_c("1;36", "└" + "─" * 50))


def step(n: int, total: int) -> None:
    """Mark the beginning of one think->act cycle."""
    print(_c("90", f"\n[{_stamp()}] step {n}/{total}"))


def thinking(text: str) -> None:
    """The model's plain-text reasoning between tool calls."""
    if text.strip():
        print(_c("37", f"  💭 {text.strip()}"))


def tool_call(name: str, args: dict) -> None:
    """A tool the model decided to invoke, and with what arguments."""
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    if len(rendered) > 160:
        rendered = rendered[:157] + "..."
    print(_c("33", f"  🔧 {name}({rendered})"))


def tool_result(text: str, is_error: bool = False) -> None:
    """What the tool handed back to the model."""
    preview = text if len(text) <= 300 else text[:297] + "..."
    preview = preview.replace("\n", "\n     ")
    icon, colour = ("❌", "31") if is_error else ("↳", "32")
    print(_c(colour, f"     {icon} {preview}"))


def answer(text: str) -> None:
    """The agent's final response."""
    print()
    print(_c("1;32", "✔ result"))
    print(text)
    print()


def warn(text: str) -> None:
    print(_c("1;33", f"⚠ {text}"), file=sys.stderr)


def error(text: str) -> None:
    print(_c("1;31", f"✖ {text}"), file=sys.stderr)
