"""
Getting results out of the machine and in front of a person.

An agent that runs at 3am and writes a file nobody opens has not automated
anything. These tools deliver the result somewhere you will actually see it.

Both tools are covered by dry run, because sending a message is exactly the
kind of thing you want to rehearse first — an unsent message costs nothing,
an accidental one to a team channel costs a conversation. Destinations come
from environment variables rather than from the model, so an agent can only
send to endpoints *you* configured.
"""

from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage

import requests

from agentkit.safety import guard_write
from agentkit.tools import tool
from core.config import Config


@tool
def send_webhook(config: Config, message: str, destination: str = "default") -> str:
    """Post a message to a preconfigured Slack, Discord or generic webhook.

    message: the text to send; Slack and Discord both accept Markdown
    destination: which configured webhook to use, matching a WEBHOOK_URL_<NAME> variable
    """
    # The model picks a *name*, never a URL. The URL comes from the
    # environment, so an agent cannot be talked into posting to an arbitrary
    # endpoint by anything it reads in a document.
    variable = (
        "WEBHOOK_URL"
        if destination == "default"
        else f"WEBHOOK_URL_{destination.upper().replace('-', '_')}"
    )
    url = os.getenv(variable)
    if not url:
        return (
            f"No webhook configured for {destination!r} (expected {variable} in .env). "
            "Add it, or write the result to a file instead."
        )

    blocked = guard_write(config, f"post {len(message)} chars to the {destination!r} webhook")
    if blocked:
        return f"{blocked}\nMessage would have been:\n{message[:500]}"

    # Slack and Discord disagree on the field name, so send both. Each
    # ignores the one it does not recognise, and generic endpoints get JSON.
    payload = {"text": message, "content": message}

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        return f"Webhook post failed: {exc}"

    return f"Posted {len(message)} characters to the {destination!r} webhook."


@tool
def send_email(config: Config, subject: str, body: str, to: str = "") -> str:
    """Email a result via the configured SMTP server.

    subject: the email subject line
    body: the plain-text body
    to: recipient address; leave empty to use the configured default
    """
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "")
    recipient = to or os.getenv("SMTP_TO", "")

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", host), ("SMTP_USER", user),
            ("SMTP_PASSWORD", password), ("recipient", recipient),
        )
        if not value
    ]
    if missing:
        return f"Email is not configured — missing: {', '.join(missing)}. Add them to .env."

    blocked = guard_write(config, f"email {recipient} with subject {subject!r}")
    if blocked:
        return f"{blocked}\nBody would have been:\n{body[:500]}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    port = int(os.getenv("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()  # never send credentials over a plaintext link
            server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        return f"Email failed: {type(exc).__name__}: {exc}"

    return f"Emailed {recipient} with subject {subject!r}."


@tool
def save_result(config: Config, filename: str, content: str, also_notify: bool = False) -> str:
    """Save a result to the workspace, optionally posting it to the default webhook too.

    filename: destination file, relative to the workspace root
    content: the text to save
    also_notify: post the same content to the default webhook as well
    """
    # The common ending for a scheduled agent: keep a durable copy on disk,
    # and optionally ping a human that there is something new to look at.
    from toolkits.files import write_file

    saved = write_file(config=config, path=filename, content=content)
    if not also_notify:
        return saved

    notified = send_webhook(
        config=config,
        message=f"*{filename}*\n\n{content[:1500]}",
        destination="default",
    )
    return f"{saved}\n{notified}"
