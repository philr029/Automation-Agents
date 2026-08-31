"""
Data tools: reading and writing the tabular formats automation actually meets.

Extraction agents are only useful if their output lands somewhere a human
can open. These tools make CSV and JSON first-class so an agent can finish
a job by producing a spreadsheet rather than a wall of prose.
"""

from __future__ import annotations

import csv
import io
import json

from agentkit.safety import guard_write, resolve_in_workspace
from agentkit.tools import tool
from core.config import Config


@tool
def read_csv(config: Config, path: str, max_rows: int = 100) -> str:
    """Read a CSV file and return its rows as readable text.

    path: CSV file, relative to the workspace root
    max_rows: stop after this many data rows
    """
    target = resolve_in_workspace(config, path)
    if not target.is_file():
        return f"No such file: {path}"

    with target.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        rows = []
        for index, row in enumerate(reader):
            if index > max_rows:
                rows.append(f"[stopped after {max_rows} rows]")
                break
            rows.append(" | ".join(row))

    return "\n".join(rows) if rows else "(empty file)"


@tool
def write_csv(config: Config, path: str, headers: list[str], rows: list) -> str:
    """Write rows to a CSV file, creating a spreadsheet a human can open.

    path: destination CSV, relative to the workspace root
    headers: the column names, in order
    rows: a list of rows, each row a list of values matching the headers
    """
    target = resolve_in_workspace(config, path)

    blocked = guard_write(config, f"write {len(rows)} rows to {path}")
    if blocked:
        # Show a sample even in dry run, so the rehearsal is informative.
        sample = json.dumps(rows[:3], default=str)
        return f"{blocked}\nHeaders: {headers}\nFirst rows: {sample}"

    # Build in memory first so a serialisation error cannot leave a
    # half-written file behind.
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        # Tolerate the model handing back dicts instead of lists.
        if isinstance(row, dict):
            writer.writerow([row.get(h, "") for h in headers])
        else:
            writer.writerow(row)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(buffer.getvalue(), encoding="utf-8")
    return f"Wrote {len(rows)} rows and {len(headers)} columns to {path}."


@tool
def read_json(config: Config, path: str) -> str:
    """Read a JSON file and return it formatted.

    path: JSON file, relative to the workspace root
    """
    target = resolve_in_workspace(config, path)
    if not target.is_file():
        return f"No such file: {path}"
    try:
        return json.dumps(json.loads(target.read_text(encoding="utf-8")), indent=2)[:20000]
    except json.JSONDecodeError as exc:
        return f"{path} is not valid JSON: {exc}"


@tool
def append_to_log(config: Config, path: str, entry: str) -> str:
    """Append one line to a running log file, creating it if needed.

    path: log file, relative to the workspace root
    entry: the line to append
    """
    from datetime import datetime, timezone

    target = resolve_in_workspace(config, path)

    blocked = guard_write(config, f"append an entry to {path}")
    if blocked:
        return blocked

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {entry}\n")
    return f"Appended to {path}."
