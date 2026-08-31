# Automation Agents

A small, readable framework for building AI agents that do real work on a
schedule — organising files, triaging messages, watching web pages, pulling
data out of documents, summarising repositories.

Seven agents ship with it. The engine underneath is about 300 lines, and
adding your own agent is roughly ten.

---

## Quick start

```bash
git clone https://github.com/philr029/Automation-Agents.git
cd Automation-Agents

pip install -r requirements.txt

cp .env.example .env        # then paste your key from console.anthropic.com
```

```bash
python main.py --list                      # see what's available
python main.py file-organizer              # dry run: says what it would do
python main.py file-organizer --apply      # actually does it
```

**Every run is a dry run by default.** Agents plan, report, and change
nothing until you add `--apply`. Get in the habit of reading the dry run first.

---

## The agents

| Agent | What it does |
|---|---|
| `file-organizer` | Reads a messy folder, works out what each file actually is, sorts it into sensible subfolders and renames the badly-named ones. |
| `triage` | Reads a folder of messages, ranks them URGENT / RESPOND / FYI / IGNORE, and drafts replies for the ones that need them. |
| `web-watcher` | Checks a list of URLs against saved snapshots and reports only what genuinely changed — ignoring timestamps, ads and counters. |
| `report-writer` | Reads a stack of documents and writes one synthesised briefing: bottom line, key points, conflicts, next steps. |
| `extractor` | Pulls structured fields out of unstructured documents (invoices, CVs, forms) into a CSV you can open in a spreadsheet. |
| `repo-digest` | Turns raw git history into a changelog a non-programmer can read. |
| `researcher` | Researches a topic across web sources and writes a brief with citations. |
| `meeting-notes` | Turns a raw transcript into decisions, action items with owners, and open questions. |
| `code-reviewer` | Reads a git diff, flags real bugs with a failure scenario for each — not style opinions. |
| `daily-digest` | Pulls watched pages and new files into one short briefing and delivers it. |
| `spreadsheet-cleaner` | Cleans a messy CSV: finds the real header, normalises formats, quarantines bad rows. |
| `repurposer` | Turns one long document into an exec summary, a detailed summary, social posts and an email. |

Give any of them a custom task as the second argument:

```bash
python main.py triage "Only the messages from this week, and be strict about urgency"
python main.py extractor --apply --workspace ~/invoices
```

---

## How it works

An agent is a loop. That's the whole idea:

```
1. Send the conversation + the list of tools to the model
2. The model replies with text, tool calls, or both
3. Tool calls? Run them, append the results, go to 1
4. No tool calls? It's finished — its text is the answer
```

That loop lives in `agentkit/agent.py`. Everything else is scaffolding
around it.

```
core/          config, the API client, console logging
agentkit/      the engine: tool schemas, the loop, safety guards
toolkits/      what agents can do: files, web, data, git, documents, notify
agents/        the agents themselves — prompts + tool choices
main.py        the CLI
scheduler.py   runs agents on a schedule
pipelines.py   chains agents together
```

### Layers

**`core/config.py`** — one frozen dataclass holding every setting, built once
from `.env`. Passed down through everything, so nothing reads `os.environ`
halfway through a run.

**`agentkit/tools.py`** — the `@tool` decorator. It reads a function's type
hints and docstring and generates the JSON Schema the API needs, so the
schema can never drift out of sync with the code:

```python
@tool
def read_file(config: Config, path: str, max_chars: int = 20000) -> str:
    """Read a text file from the workspace.

    path: file to read, relative to the workspace root
    max_chars: truncate after this many characters
    """
```

`path` becomes a required string, `max_chars` an optional integer, both with
descriptions, and `config` is hidden from the model entirely.

**`agentkit/safety.py`** — two guard rails. A **sandbox** resolves every path
and rejects anything outside the workspace (closing `..`, absolute paths and
symlinks alike), and **dry run** makes destructive tools describe themselves
instead of running.

**`agents/library.py`** — each agent is a declarative `AgentSpec`: a name, a
system prompt, and a list of toolkits. No subclassing, no loop to reimplement.

---

## Chaining agents together

One agent does one job well. Real work is usually several jobs in a row:
extract the data, *then* summarise it, *then* send it to you. That's a
pipeline.

```bash
python pipelines.py --list
python pipelines.py invoice-run --apply
```

Each step's answer is substituted into the next step's task wherever
`{previous}` appears. Three ship by default — `invoice-run`,
`morning-brief`, `tidy-and-summarise` — and they're declared as data in
`pipelines.py`, same as agents are.

Why not one big agent instead? Because a narrow agent with five tools and a
specific prompt is more reliable than a broad one with twenty. Chaining keeps
each step narrow while still getting the whole job done.

---

## Reading real documents

`toolkits/files.py` refuses anything that isn't plain text, so a PDF never
gets dumped into the context window as binary noise.
`toolkits/documents.py` is the other half of that: it extracts *text* from
PDFs and Word files.

- `read_pdf` — needs `pypdf`. Takes `pages="3"` or `pages="1-5"`, and clamps
  a too-large range to the document rather than erroring. A PDF with no text
  layer is reported as a probable scan, not as an empty file.
- `read_docx` — no dependency at all; a `.docx` is a zip of XML, and the
  stdlib handles it.
- `read_document` — dispatches on file extension. Use this for mixed folders.

If `pypdf` isn't installed, `read_pdf` returns an install instruction instead
of crashing the run — every other tool keeps working.

---

## Delivering results

An agent that runs at 3am and writes a file nobody opens hasn't automated
anything. `toolkits/notify.py` sends the result somewhere you'll see it:
`send_webhook` (Slack/Discord/generic), `send_email` (SMTP), and
`save_result` (write to disk, optionally ping a channel too).

One detail worth knowing: **the model picks a destination *name*, never a
URL.** `send_webhook(destination="alerts")` looks up `WEBHOOK_URL_ALERTS` in
your environment. So an agent can only post to endpoints you configured, and
nothing it reads in a document can talk it into posting somewhere else.

Both senders are covered by dry run, because an unsent message costs nothing
and an accidental one to a team channel costs a conversation.

---

## Adding your own agent

Add a block to `agents/library.py`:

```python
register(
    AgentSpec(
        name="my-agent",
        summary="One line for the --list output.",
        toolkits=[files, data],
        default_task="What to do when no task is given.",
        instructions="""
        You do X.

        Method:
        1. ...

        Rules:
        - ...
        """,
    )
)
```

It shows up in `python main.py --list` immediately. Most of the work of a
good agent is in the prompt: a clear method, and explicit rules about what
*not* to do.

To give agents a new capability, write a function in a `toolkits/` module,
decorate it with `@tool`, and add that module to an agent's `toolkits` list.

---

## Running on a schedule

Edit `schedule.json`:

```json
[
  {"agent": "web-watcher", "every_minutes": 60, "apply": true},
  {"agent": "triage", "every_minutes": 1440, "apply": false}
]
```

Then either run the built-in loop:

```bash
python scheduler.py
```

…or, better for anything real, let the OS handle it:

```cron
*/30 * * * * cd /path/to/Automation-Agents && python scheduler.py --once >> cron.log 2>&1
```

Cron gives you restarts, logging and failure mail for free. Every scheduled
run writes a JSON transcript to `runs/`.

---

## Safety

Automation that can write files can also destroy them. Four things stand in
the way:

1. **Dry run by default.** Nothing changes until `--apply`.
2. **The workspace sandbox.** Agents cannot read or write outside
   `AGENT_WORKSPACE`, whatever path they ask for.
3. **A step ceiling.** `AGENT_MAX_STEPS` caps the loop so a confused agent
   can't spin forever burning tokens.
4. **A narrow shell.** The git toolkit allows eight read-only subcommands and
   refuses everything else. There is no general shell tool, deliberately.

Widen the allowlist by adding entries to it — never by removing the check.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

44 tests covering schema generation, the sandbox, argument injection, the
agent loop itself (via a scripted fake client), document parsing, and the
delivery tools' safety behaviour. They never touch the network and need no
API key, so they run in well under a second and cost nothing.

They run in CI on every push and pull request across Python 3.11, 3.12 and
3.13 — see `.github/workflows/tests.yml`.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. From console.anthropic.com |
| `AGENT_MODEL` | `claude-sonnet-5` | `claude-opus-5` for harder reasoning |
| `AGENT_WORKSPACE` | `./workspace` | The only folder agents may touch |
| `AGENT_DRY_RUN` | `true` | Rehearse instead of acting |
| `AGENT_MAX_STEPS` | `25` | Ceiling on think/act cycles |

CLI flags (`--apply`, `--workspace`, `--model`, `--max-steps`) override these
per run.
