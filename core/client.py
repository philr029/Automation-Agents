"""
The single place where we talk to the Anthropic API.

Isolating client construction here means the retry policy, the API key check,
and the default model live in one file. If you ever switch to Bedrock or
Vertex, this is the only module that changes.
"""

from __future__ import annotations

import anthropic

from core.config import Config


class MissingAPIKey(RuntimeError):
    """Raised with actionable instructions instead of a bare KeyError."""


def build_client(config: Config) -> anthropic.Anthropic:
    """Create an Anthropic client, failing loudly if the key is absent."""
    if not config.api_key:
        raise MissingAPIKey(
            "ANTHROPIC_API_KEY is not set.\n"
            "  1. cp .env.example .env\n"
            "  2. paste your key from https://console.anthropic.com/settings/keys\n"
        )

    # `max_retries` makes the SDK transparently retry 429s and 5xx responses
    # with exponential backoff, which matters for unattended scheduled runs.
    return anthropic.Anthropic(api_key=config.api_key, max_retries=4, timeout=120.0)
