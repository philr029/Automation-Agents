#!/usr/bin/env python3
"""
Chaining agents together.

One agent does one job well. Real work is usually several jobs in a row:
extract the data, *then* summarise it, *then* send it to me. A pipeline runs
agents in sequence and feeds each one's answer into the next.

    python pipelines.py --list
    python pipelines.py invoice-run --apply

Pipelines are declared in `PIPELINES` below. Each step names an agent and a
task; `{previous}` in a task is replaced with the previous step's answer.

Why not one big agent? Because a narrow agent with five tools and a specific
prompt is measurably more reliable than a broad one with twenty. Chaining
keeps each step narrow while still getting the whole job done.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from core import log
from core.client import MissingAPIKey, build_client
from core.config import Config


@dataclass
class Step:
    """One agent invocation within a pipeline."""

    agent: str
    task: str
    # When true, a failure here stops the pipeline. When false, the pipeline
    # carries on with whatever the step managed to produce — right for a
    # final "notify someone" step, wrong for "extract the data".
    optional: bool = False


@dataclass
class Pipeline:
    name: str
    summary: str
    steps: list[Step] = field(default_factory=list)


PIPELINES: dict[str, Pipeline] = {
    "invoice-run": Pipeline(
        name="invoice-run",
        summary="Extract invoice data to CSV, then summarise the spend.",
        steps=[
            Step(
                agent="extractor",
                task="Extract every invoice in the workspace into invoices.csv.",
            ),
            Step(
                agent="report-writer",
                task=(
                    "Read invoices.csv and write spend-summary.md: total spend, "
                    "the largest suppliers, and anything that looks anomalous.\n\n"
                    "The extraction step reported:\n{previous}"
                ),
            ),
        ],
    ),
    "morning-brief": Pipeline(
        name="morning-brief",
        summary="Check watched pages, then write and deliver a single briefing.",
        steps=[
            Step(
                agent="web-watcher",
                task="Check every URL in watchlist.txt and report what changed.",
            ),
            Step(
                agent="report-writer",
                task=(
                    "Write morning-brief.md from the change report below. Lead with "
                    "what a person needs to know first. If nothing changed, say so "
                    "in one line rather than padding it.\n\n{previous}"
                ),
            ),
        ],
    ),
    "tidy-and-summarise": Pipeline(
        name="tidy-and-summarise",
        summary="Organise a messy folder, then summarise what is now in it.",
        steps=[
            Step(agent="file-organizer", task="Organize the workspace into sensible folders."),
            Step(
                agent="report-writer",
                task=(
                    "Now write inventory.md describing what this folder contains "
                    "and how it is organised.\n\nThe organiser reported:\n{previous}"
                ),
            ),
        ],
    ),
}


def run_pipeline(name: str, config: Config, verbose: bool = True) -> str:
    """Run every step in order, threading each answer into the next task."""
    from agents import build

    if name not in PIPELINES:
        raise KeyError(f"No pipeline named {name!r}. Available: {', '.join(PIPELINES)}")

    pipeline = PIPELINES[name]
    client = build_client(config)
    previous = ""

    for number, step in enumerate(pipeline.steps, start=1):
        if verbose:
            print()
            log.banner(
                f"{pipeline.name} — step {number}/{len(pipeline.steps)}: {step.agent}",
                step.task.split("\n")[0],
            )

        agent = build(step.agent, config, client)
        # `{previous}` is substituted rather than formatted, because the
        # previous answer is arbitrary text that may well contain braces.
        task = step.task.replace("{previous}", previous or "(nothing yet)")

        try:
            result = agent.run(task, verbose=verbose)
        except Exception as exc:  # noqa: BLE001
            if step.optional:
                log.warn(f"Optional step {step.agent} failed: {exc}. Continuing.")
                continue
            raise

        if result.stopped_early and not step.optional:
            log.warn(f"Step {step.agent} hit the step ceiling; passing on a partial result.")

        previous = result.text

    return previous


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a chain of agents.")
    parser.add_argument("pipeline", nargs="?", help="which pipeline to run")
    parser.add_argument("--list", action="store_true", help="show available pipelines")
    parser.add_argument("--apply", action="store_true", help="actually make changes")
    parser.add_argument("--workspace", help="folder the agents may use")
    args = parser.parse_args()

    if args.list or not args.pipeline:
        print("\nAvailable pipelines:\n")
        width = max(len(n) for n in PIPELINES)
        for pipeline in PIPELINES.values():
            print(f"  {pipeline.name.ljust(width)}  {pipeline.summary}")
            for number, step in enumerate(pipeline.steps, start=1):
                print(f"    {number}. {step.agent}")
        print()
        return 0

    overrides: dict[str, object] = {"dry_run": not args.apply}
    if args.workspace:
        from pathlib import Path

        overrides["workspace"] = Path(args.workspace).expanduser().resolve()

    config = Config.load(**overrides)
    if config.dry_run:
        log.warn("Dry run — nothing will change. Add --apply to commit.")

    try:
        final = run_pipeline(args.pipeline, config)
    except MissingAPIKey as exc:
        log.error(str(exc))
        return 1
    except KeyError as exc:
        log.error(str(exc))
        return 2

    log.answer(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
