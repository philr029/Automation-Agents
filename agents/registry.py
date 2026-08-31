"""
How an agent is defined, and how a definition becomes a running agent.

Every agent in this project is the same `Agent` class from `agentkit`. What
distinguishes them is data, not code: a system prompt and a choice of tools.
So an agent is declared as an `AgentSpec` — about ten lines — and `build()`
turns any spec into a live agent.

Adding a new agent means adding one spec to `agents/library.py`. There is no
subclass to write and no loop to reimplement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from agentkit.agent import Agent
from agentkit.tools import ToolRegistry
from core.config import Config

# A shared preamble every agent inherits. Behaviour you want from *all*
# agents belongs here; behaviour specific to one belongs in its own prompt.
SHARED_RULES = """
You are an automation agent. You work unattended, so these rules matter:

- Use your tools to find things out. Never guess at a file's contents, a
  page's text, or a directory's layout when a tool can tell you.
- Take one step at a time and read each tool result before deciding the next.
- If a tool returns an error, read it and adapt. Do not repeat the same
  failing call unchanged.
- If a tool result begins with "[dry run]", the system is in rehearsal mode.
  Carry on planning as though it had succeeded, and say clearly in your final
  answer that nothing was actually changed.
- Stop when the task is done and give a short, concrete final answer: what
  you did, what you found, and anything a human needs to decide.
- Never invent data. If something cannot be determined, say so plainly.
""".strip()


@dataclass
class AgentSpec:
    """A declarative description of one agent."""

    name: str                          # CLI identifier, e.g. "file-organizer"
    summary: str                       # one line shown by `--list`
    instructions: str                  # the agent-specific system prompt
    toolkits: list[Any] = field(default_factory=list)   # modules to take tools from
    extra_tools: list[Callable] = field(default_factory=list)  # individual tools
    default_task: str = ""             # used when the user gives no task
    max_tokens: int = 4096

    def system_prompt(self) -> str:
        """The agent's own instructions, on top of the shared rules."""
        return f"{SHARED_RULES}\n\n---\n\n{self.instructions.strip()}"


# Populated by agents/library.py at import time.
REGISTRY: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    """Add a spec to the registry. Called once per agent in the library."""
    REGISTRY[spec.name] = spec
    return spec


def get_spec(name: str) -> AgentSpec:
    """Look up an agent by name, with a helpful error if it is misspelled."""
    _load_library()
    if name not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"No agent named {name!r}. Available: {known}")
    return REGISTRY[name]


def list_names() -> list[str]:
    _load_library()
    return sorted(REGISTRY)


def describe_all() -> str:
    """A formatted table of every agent, for `main.py --list`."""
    _load_library()
    width = max((len(n) for n in REGISTRY), default=0)
    return "\n".join(
        f"  {spec.name.ljust(width)}  {spec.summary}"
        for spec in sorted(REGISTRY.values(), key=lambda s: s.name)
    )


def build(name: str, config: Config, client: anthropic.Anthropic) -> Agent:
    """Turn a registered spec into an `Agent` ready to run."""
    spec = get_spec(name)

    registry = ToolRegistry()
    for module in spec.toolkits:
        registry.add_module(module)
    if spec.extra_tools:
        registry.add(*spec.extra_tools)

    # Inject `config` into every tool that asks for it. This is what gives
    # the tools their sandbox root and dry-run flag without ever exposing
    # those to the model.
    registry = registry.bind(config=config)

    return Agent(
        name=spec.name,
        system_prompt=spec.system_prompt(),
        tools=registry,
        config=config,
        client=client,
        max_tokens=spec.max_tokens,
    )


def _load_library() -> None:
    """Import the library on first use, avoiding a circular import at module load."""
    if not REGISTRY:
        import agents.library  # noqa: F401  (importing is the point)
