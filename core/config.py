"""
Configuration for the whole toolkit.

Everything tunable lives in one immutable object built once at startup from
environment variables (loaded from a `.env` file if present). Passing that
object around instead of reading `os.environ` in twenty places means the
settings are easy to see, easy to override in tests, and impossible to
mutate halfway through a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# The repository root — this file is at <root>/core/config.py, so two parents up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment, accepting the usual spellings."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read an integer from the environment, falling back if it isn't one."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of every setting the agents care about."""

    api_key: str | None
    model: str
    workspace: Path
    dry_run: bool
    max_steps: int

    # Where agents persist state between runs (e.g. the web watcher's
    # previous snapshot of a page) and where run transcripts are written.
    state_dir: Path
    runs_dir: Path

    @classmethod
    def load(cls, **overrides: object) -> "Config":
        """
        Build a Config from the environment.

        Any keyword argument overrides the corresponding environment value,
        which is what the CLI uses for flags like `--dry-run` and what tests
        use to point the workspace at a temporary directory.
        """
        load_dotenv(PROJECT_ROOT / ".env")

        workspace = Path(
            os.getenv("AGENT_WORKSPACE", str(PROJECT_ROOT / "workspace"))
        ).expanduser()
        if not workspace.is_absolute():
            workspace = (PROJECT_ROOT / workspace).resolve()

        config = cls(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            model=os.getenv("AGENT_MODEL", "claude-sonnet-5"),
            workspace=workspace,
            dry_run=_env_bool("AGENT_DRY_RUN", True),
            max_steps=_env_int("AGENT_MAX_STEPS", 25),
            state_dir=PROJECT_ROOT / ".state",
            runs_dir=PROJECT_ROOT / "runs",
        )

        # `dataclasses.replace` is the supported way to copy a frozen
        # dataclass with changes; it validates the field names for us.
        if overrides:
            from dataclasses import replace

            config = replace(config, **overrides)  # type: ignore[arg-type]

        config.workspace.mkdir(parents=True, exist_ok=True)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.runs_dir.mkdir(parents=True, exist_ok=True)
        return config
