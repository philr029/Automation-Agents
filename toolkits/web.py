"""
Web tools: fetch a page, reduce it to readable text, and detect changes.

There is deliberately no HTML parsing library here. `html.parser` from the
standard library is enough to strip tags, and one fewer dependency is one
fewer thing to install, pin, and audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser

import requests

from agentkit.tools import tool
from core.config import Config

# Identify ourselves honestly. Many sites block unlabelled scrapers, and
# a real user agent string is the polite thing to send.
USER_AGENT = "AutomationAgents/1.0 (+https://github.com/philr029/Automation-Agents)"

# Tags whose contents are code or styling, never prose.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}


class _TextExtractor(HTMLParser):
    """Collects the human-readable text of a page, ignoring markup."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._suppressed = 0  # depth counter, so nested skip-tags work

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed and data.strip():
            self.chunks.append(data.strip())

    def text(self) -> str:
        joined = "\n".join(self.chunks)
        # Collapse runs of blank lines left behind by stripped markup.
        return re.sub(r"\n{3,}", "\n\n", joined)


def _fetch(url: str, timeout: int = 30) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


@tool
def fetch_page(url: str, max_chars: int = 15000) -> str:
    """Download a web page and return its readable text with markup stripped.

    url: full URL including https://
    max_chars: truncate the extracted text after this many characters
    """
    try:
        response = _fetch(url)
    except requests.RequestException as exc:
        return f"Could not fetch {url}: {exc}"

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(response.text)
        text = parser.text()
    else:
        text = response.text

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated — page is {len(text)} chars]"
    return text or "(page returned no readable text)"


@tool
def fetch_json(url: str) -> str:
    """Download a JSON API endpoint and return it formatted.

    url: full URL of a JSON endpoint
    """
    try:
        response = _fetch(url)
        return json.dumps(response.json(), indent=2)[:15000]
    except requests.RequestException as exc:
        return f"Could not fetch {url}: {exc}"
    except ValueError:
        return f"{url} did not return valid JSON."


@tool
def check_for_changes(config: Config, url: str, label: str) -> str:
    """Compare a page against the last time it was checked and report if it changed.

    url: page to monitor
    label: short name used to store this page's snapshot between runs
    """
    try:
        text = _TextExtractor()
        text.feed(_fetch(url).text)
        current = text.text()
    except requests.RequestException as exc:
        return f"Could not fetch {url}: {exc}"

    # A stable filename derived from the label, safe on every filesystem.
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "page"
    snapshot = config.state_dir / f"watch-{slug}.json"

    # Hashing rather than storing the full page keeps state small; we keep
    # the text too so the model can be shown what actually changed.
    digest = hashlib.sha256(current.encode()).hexdigest()

    if not snapshot.exists():
        snapshot.write_text(json.dumps({"hash": digest, "text": current}))
        return f"First check of {label}. Baseline saved ({len(current)} chars); nothing to compare yet."

    previous = json.loads(snapshot.read_text())
    if previous.get("hash") == digest:
        return f"{label} is unchanged since the last check."

    old_lines = set(previous.get("text", "").splitlines())
    added = [line for line in current.splitlines() if line not in old_lines]

    snapshot.write_text(json.dumps({"hash": digest, "text": current}))

    preview = "\n".join(added[:40]) or "(only removals or reordering)"
    return f"{label} CHANGED. {len(added)} new line(s):\n\n{preview}"
