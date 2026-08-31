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


def _console_speaks_unicode() -> bool:
    """
    Can this terminal actually print the symbols below?

    macOS and Linux terminals are UTF-8 and handle them fine. A Windows
    console may still be cp1252, where printing an emoji raises
    UnicodeEncodeError and takes down the run — losing the agent's work over
    a decorative character. Ask the stream's own encoder rather than guessing
    from the platform name.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "💭🔧↳❌✔⚠✖┌│└".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_UNICODE = _console_speaks_unicode()

# Every symbol used below, with a plain-ASCII fallback for consoles that
# cannot encode the nicer one.
_SYMBOLS = {
    "think": ("💭", "..."),
    "tool": ("🔧", "->"),
    "result": ("↳", "  >"),
    "error": ("❌", "!!"),
    "ok": ("✔", "OK"),
    "warn": ("⚠", "!"),
    "fail": ("✖", "x"),
    "top": ("┌─", "+-"),
    "mid": ("│", "|"),
    "bottom": ("└", "+"),
    "rule": ("─", "-"),
}


def _s(key: str) -> str:
    """The best symbol this console can display."""
    fancy, plain = _SYMBOLS[key]
    return fancy if _UNICODE else plain


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def banner(agent_name: str, task: str) -> None:
    """Announce the start of a run."""
    print()
    print(_c("1;36", f"{_s('top')} {agent_name} "))
    print(_c("36", f"{_s('mid')}  {task}"))
    print(_c("1;36", _s("bottom") + _s("rule") * 50))


def step(n: int, total: int) -> None:
    """Mark the beginning of one think->act cycle."""
    print(_c("90", f"\n[{_stamp()}] step {n}/{total}"))


def thinking(text: str) -> None:
    """The model's plain-text reasoning between tool calls."""
    if text.strip():
        print(_c("37", f"  {_s('think')} {text.strip()}"))


def tool_call(name: str, args: dict) -> None:
    """A tool the model decided to invoke, and with what arguments."""
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    if len(rendered) > 160:
        rendered = rendered[:157] + "..."
    print(_c("33", f"  {_s('tool')} {name}({rendered})"))


def tool_result(text: str, is_error: bool = False) -> None:
    """What the tool handed back to the model."""
    preview = text if len(text) <= 300 else text[:297] + "..."
    preview = preview.replace("\n", "\n     ")
    icon, colour = (_s("error"), "31") if is_error else (_s("result"), "32")
    print(_c(colour, f"     {icon} {preview}"))


def answer(text: str) -> None:
    """The agent's final response."""
    print()
    print(_c("1;32", f"{_s('ok')} result"))
    print(text)
    print()


def warn(text: str) -> None:
    print(_c("1;33", f"{_s('warn')} {text}"), file=sys.stderr)


def error(text: str) -> None:
    print(_c("1;31", f"{_s('fail')} {text}"), file=sys.stderr)
