"""
The agent loop.

This is the whole idea in one file. An "agent" is a model that can act, and
acting is a loop:

    1. Send the conversation so far, plus the list of tools, to the model.
    2. The model replies. Its reply contains text, tool calls, or both.
    3. If it made tool calls, run them and append the results as a new user
       message, then go back to step 1.
    4. If it made none, it is finished; its text is the answer.

Everything else in this repository — the toolkits, the agent definitions,
the CLI — is scaffolding around these thirty-odd lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic

from agentkit.tools import ToolRegistry
from core import log
from core.config import Config


@dataclass
class AgentResult:
    """Everything a finished run produced."""

    text: str                       # the agent's final answer
    steps: int                      # how many model round-trips it took
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_early: bool = False     # true if it hit the step ceiling

    @property
    def estimated_cost_usd(self) -> float:
        """
        Rough cost estimate at Sonnet's published rate ($3/$15 per Mtok).

        Deliberately approximate — it exists so a scheduled job can log
        "this run cost about a cent", not for billing.
        """
        return (self.input_tokens / 1e6) * 3.0 + (self.output_tokens / 1e6) * 15.0


class Agent:
    """
    A model, a system prompt, and a set of tools.

    Two agents differ only in those three things. `report_writer` and
    `file_organizer` share every line of this class; they just carry
    different instructions and different capabilities.
    """

    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: ToolRegistry,
        config: Config,
        client: anthropic.Anthropic,
        max_tokens: int = 4096,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self.config = config
        self.client = client
        self.max_tokens = max_tokens

    def run(self, task: str, verbose: bool = True) -> AgentResult:
        """Work on `task` until the model stops asking for tools."""
        if verbose:
            log.banner(self.name, task)

        # The conversation is a plain list of messages that grows as we go.
        # We hand the model the entire list every turn — that list *is* the
        # agent's memory; there is no hidden state anywhere else.
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        result = AgentResult(text="", steps=0)

        for step_number in range(1, self.config.max_steps + 1):
            if verbose:
                log.step(step_number, self.config.max_steps)
            result.steps = step_number

            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                tools=self.tools.to_api_format(),
                messages=messages,
            )

            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens

            # The assistant's reply goes back into the conversation verbatim.
            # Passing the content blocks through unchanged is what lets the
            # model see its own earlier reasoning on later turns.
            messages.append({"role": "assistant", "content": response.content})

            text_blocks = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if verbose:
                for chunk in text_blocks:
                    log.thinking(chunk)

            # `stop_reason == "tool_use"` means the model wants to act. Any
            # other reason ("end_turn", "max_tokens") means it is done talking.
            if not tool_uses:
                result.text = "\n".join(text_blocks).strip()
                return result

            # Run every requested tool and collect the results. The API
            # requires one tool_result block per tool_use block, matched by
            # id, in a single following user message.
            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                if verbose:
                    log.tool_call(block.name, dict(block.input))

                output, is_error = self._execute(block.name, dict(block.input))

                if verbose:
                    log.tool_result(output, is_error)

                result.tool_calls.append(
                    {"tool": block.name, "input": dict(block.input), "error": is_error}
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                        "is_error": is_error,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        # Fell out of the loop: the model kept asking for tools past the
        # ceiling. Report what we have rather than pretending it finished.
        result.stopped_early = True
        result.text = (
            f"Stopped after {self.config.max_steps} steps without a final answer. "
            "Raise AGENT_MAX_STEPS if the task genuinely needs more, or narrow it."
        )
        if verbose:
            log.warn(result.text)
        return result

    def _execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """
        Run one tool, converting any failure into a message the model can read.

        Returning errors to the model instead of raising is the important
        part: a wrong path or a malformed argument becomes feedback the model
        can correct on the next turn, rather than a crashed run.
        """
        tool = self.tools.get(name)
        if tool is None:
            return f"No such tool: {name}. Available: {', '.join(self.tools.tools)}", True

        try:
            return tool.call(arguments), False
        except TypeError as exc:
            return f"Wrong arguments for {name}: {exc}", True
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            return f"{type(exc).__name__}: {exc}", True
