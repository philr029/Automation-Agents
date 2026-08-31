#!/usr/bin/env python3
"""
Command-line entry point.

    python main.py --list
    python main.py file-organizer
    python main.py triage "Only look at the messages from this week"
    python main.py extractor --apply --workspace ~/invoices

By default every run is a *dry run*: agents plan and report but change
nothing on disk. Add `--apply` once you are happy with what it says it will do.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import log
from core.client import MissingAPIKey, build_client
from core.config import Config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="automation-agents",
        description="Run an automation agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("agent", nargs="?", help="which agent to run")
    parser.add_argument("task", nargs="?", help="what to do; omit to use the agent's default")
    parser.add_argument("--list", action="store_true", help="show the available agents and exit")
    parser.add_argument("--apply", action="store_true", help="actually make changes (default is a dry run)")
    parser.add_argument("--workspace", help="folder the agent may read and write")
    parser.add_argument("--model", help="override the model, e.g. claude-opus-5")
    parser.add_argument("--max-steps", type=int, help="ceiling on think/act cycles")
    parser.add_argument("--quiet", action="store_true", help="print only the final answer")
    parser.add_argument("--save", action="store_true", help="write a JSON transcript to runs/")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    """Turn CLI flags into config overrides, leaving unset flags to the environment."""
    overrides: dict[str, object] = {}
    if args.apply:
        overrides["dry_run"] = False
    if args.workspace:
        overrides["workspace"] = Path(args.workspace).expanduser().resolve()
    if args.model:
        overrides["model"] = args.model
    if args.max_steps:
        overrides["max_steps"] = args.max_steps
    return Config.load(**overrides)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Imported here rather than at module scope so `--list` and `--help`
    # stay fast and work without any dependencies being importable.
    from agents import build, describe_all, get_spec, list_names

    if args.list or not args.agent:
        print("\nAvailable agents:\n")
        print(describe_all())
        print("\nRun one with:  python main.py <agent> \"<task>\"")
        print("Add --apply to let it actually change files.\n")
        return 0

    if args.agent not in list_names():
        log.error(f"No agent named {args.agent!r}. Available: {', '.join(list_names())}")
        return 2

    config = build_config(args)

    try:
        client = build_client(config)
    except MissingAPIKey as exc:
        log.error(str(exc))
        return 1

    spec = get_spec(args.agent)
    task = args.task or spec.default_task

    if config.dry_run and not args.quiet:
        log.warn("Dry run — nothing on disk will change. Add --apply to commit.")

    agent = build(args.agent, config, client)
    result = agent.run(task, verbose=not args.quiet)

    if args.quiet:
        print(result.text)
    else:
        log.answer(result.text)
        print(
            f"  {result.steps} steps · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens + result.output_tokens:,} tokens · "
            f"~${result.estimated_cost_usd:.4f}\n"
        )

    if args.save:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = config.runs_dir / f"{args.agent}-{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "agent": args.agent,
                    "task": task,
                    "dry_run": config.dry_run,
                    "model": config.model,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                    "answer": result.text,
                    "tokens": {
                        "input": result.input_tokens,
                        "output": result.output_tokens,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  transcript saved to {path}")

    # Non-zero exit if the agent ran out of steps, so a cron job can notice.
    return 3 if result.stopped_early else 0


if __name__ == "__main__":
    sys.exit(main())
