#!/usr/bin/env python3
"""
Run agents on a schedule.

An agent you have to launch by hand is a tool. An agent that runs on its own
is automation. This is the smallest thing that turns one into the other:
a loop that reads a schedule file and runs agents when they are due.

    python scheduler.py --once     # run everything due right now, then exit
    python scheduler.py            # stay running and keep checking

The schedule lives in `schedule.json`:

    [
      {"agent": "web-watcher", "every_minutes": 60, "apply": true},
      {"agent": "triage", "every_minutes": 1440, "task": "Triage today's mail"}
    ]

For a real deployment, prefer `--once` from cron or a systemd timer: the OS
handles restarts, logging and failure notification far better than a Python
`while True` can.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import log
from core.client import MissingAPIKey, build_client
from core.config import Config

SCHEDULE_FILE = Path(__file__).parent / "schedule.json"
# Records when each job last ran, so restarting the scheduler does not
# re-run everything immediately.
STATE_FILE = Path(__file__).parent / ".state" / "scheduler.json"


def load_schedule() -> list[dict]:
    if not SCHEDULE_FILE.exists():
        log.warn(f"No schedule file at {SCHEDULE_FILE}. Nothing to do.")
        return []
    try:
        jobs = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error(f"schedule.json is not valid JSON: {exc}")
        return []
    return [j for j in jobs if not j.get("disabled")]


def load_state() -> dict[str, str]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def job_key(job: dict) -> str:
    """Identify a job so two schedules of the same agent track separately."""
    return f"{job['agent']}::{job.get('task', '')[:40]}"


def is_due(job: dict, state: dict[str, str], now: datetime) -> bool:
    last_run = state.get(job_key(job))
    if last_run is None:
        return True  # never run before
    try:
        previous = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    return now - previous >= timedelta(minutes=job.get("every_minutes", 60))


def run_job(job: dict) -> None:
    """Run one scheduled job, isolating any failure to that job."""
    from agents import build, get_spec

    # Each job gets its own config so one job's `apply` or `workspace`
    # cannot leak into another's.
    overrides: dict[str, object] = {"dry_run": not job.get("apply", False)}
    if job.get("workspace"):
        overrides["workspace"] = Path(job["workspace"]).expanduser().resolve()
    if job.get("model"):
        overrides["model"] = job["model"]

    config = Config.load(**overrides)
    client = build_client(config)

    spec = get_spec(job["agent"])
    agent = build(job["agent"], config, client)
    result = agent.run(job.get("task") or spec.default_task, verbose=True)

    # An unattended run is only useful if it leaves a trace.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    transcript = config.runs_dir / f"{job['agent']}-{stamp}.json"
    transcript.write_text(
        json.dumps(
            {
                "agent": job["agent"],
                "scheduled": True,
                "answer": result.text,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def tick() -> int:
    """Run every job that is currently due. Returns how many ran."""
    state = load_state()
    now = datetime.now(timezone.utc)
    ran = 0

    for job in load_schedule():
        if not is_due(job, state, now):
            continue
        try:
            run_job(job)
        except MissingAPIKey as exc:
            log.error(str(exc))
            return ran
        except Exception as exc:  # noqa: BLE001
            # One broken job must not stop the others or kill the scheduler.
            log.error(f"Job {job.get('agent')} failed: {type(exc).__name__}: {exc}")

        # Record the attempt either way, so a persistently failing job
        # retries on its normal schedule instead of every single tick.
        state[job_key(job)] = now.isoformat()
        save_state(state)
        ran += 1

    return ran


def main() -> int:
    parser = argparse.ArgumentParser(description="Run scheduled agents.")
    parser.add_argument("--once", action="store_true", help="run what is due, then exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between checks")
    args = parser.parse_args()

    if args.once:
        count = tick()
        print(f"Ran {count} job(s).")
        return 0

    print(f"Scheduler running. Checking every {args.interval}s. Ctrl-C to stop.")
    try:
        while True:
            tick()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
