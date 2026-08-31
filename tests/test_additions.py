"""
Tests for the second wave: documents, notify, pipelines and the new agents.

Same discipline as `test_agentkit.py` — no network, no API key, no real
webhooks. Anything that would send data out is exercised through dry run,
which is exactly the behaviour we most want to be sure of.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.tools import ToolRegistry
from core.config import Config
from toolkits import documents, notify


def _docx(paragraphs: list[str]) -> bytes:
    """Build a minimal but genuinely valid .docx in memory."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


class WorkspaceTest(unittest.TestCase):
    """Shared setup: a throwaway workspace with dry run off."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.load(workspace=Path(self.tmp.name).resolve(), dry_run=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, content) -> Path:
        path = self.config.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class TestDocxReading(WorkspaceTest):
    def test_extracts_paragraphs(self) -> None:
        self.write("notes.docx", _docx(["First paragraph.", "Second paragraph."]))
        result = documents.read_docx(config=self.config, path="notes.docx")
        self.assertIn("First paragraph.", result)
        self.assertIn("Second paragraph.", result)

    def test_rejects_a_non_docx_gracefully(self) -> None:
        self.write("fake.docx", "this is not a zip archive")
        result = documents.read_docx(config=self.config, path="fake.docx")
        self.assertIn("not a readable .docx", result)

    def test_missing_file_is_reported_not_raised(self) -> None:
        self.assertIn("No such file", documents.read_docx(config=self.config, path="ghost.docx"))

    def test_truncates_long_documents(self) -> None:
        self.write("long.docx", _docx(["word " * 200] * 20))
        result = documents.read_docx(config=self.config, path="long.docx", max_chars=200)
        self.assertIn("[truncated", result)


class TestPageRangeParsing(unittest.TestCase):
    def test_all_returns_every_page(self) -> None:
        self.assertEqual(documents._parse_page_range("all", 3), [0, 1, 2])

    def test_single_page_is_one_indexed(self) -> None:
        # Page "2" is index 1 — the model thinks in human page numbers.
        self.assertEqual(documents._parse_page_range("2", 5), [1])

    def test_range_is_inclusive(self) -> None:
        self.assertEqual(documents._parse_page_range("1-3", 10), [0, 1, 2])

    def test_range_clamps_to_the_document(self) -> None:
        # Asking for 1-100 of a 3-page PDF gives the 3 pages, not an error.
        self.assertEqual(documents._parse_page_range("1-100", 3), [0, 1, 2])

    def test_unparseable_range_raises_readable_error(self) -> None:
        with self.assertRaises(ValueError):
            documents._parse_page_range("first-few", 10)


class TestReadDocumentDispatch(WorkspaceTest):
    def test_routes_docx_to_the_docx_reader(self) -> None:
        self.write("a.docx", _docx(["Word content here."]))
        self.assertIn("Word content here.", documents.read_document(config=self.config, path="a.docx"))

    def test_routes_plain_text_to_the_file_reader(self) -> None:
        self.write("a.md", "# Markdown content")
        self.assertIn("Markdown content", documents.read_document(config=self.config, path="a.md"))

    def test_lists_documents_grouped_by_format(self) -> None:
        self.write("a.md", "text")
        self.write("b.docx", _docx(["x"]))
        self.write("c.png", b"\x89PNG binary")
        listing = documents.list_documents(config=self.config)
        self.assertIn(".md", listing)
        self.assertIn(".docx", listing)
        # Unreadable formats are named as skipped rather than silently dropped.
        self.assertIn("c.png", listing)
        self.assertIn("skipped", listing)


class TestNotifySafety(WorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        self._saved_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        super().tearDown()

    def test_webhook_without_configuration_is_refused(self) -> None:
        os.environ.pop("WEBHOOK_URL", None)
        result = notify.send_webhook(config=self.config, message="hi")
        self.assertIn("No webhook configured", result)

    def test_dry_run_does_not_send(self) -> None:
        # The URL is deliberately unroutable: if dry run leaked, this would
        # raise a connection error rather than return a preview.
        os.environ["WEBHOOK_URL"] = "http://127.0.0.1:1/never"
        dry = Config.load(workspace=self.config.workspace, dry_run=True)
        result = notify.send_webhook(config=dry, message="secret payload")
        self.assertIn("[dry run]", result)
        self.assertIn("secret payload", result)

    def test_destination_name_maps_to_an_env_var_not_a_url(self) -> None:
        # A model asking for destination "alerts" can only reach whatever
        # WEBHOOK_URL_ALERTS points at — it cannot supply a URL of its own.
        os.environ.pop("WEBHOOK_URL_ALERTS", None)
        result = notify.send_webhook(config=self.config, message="x", destination="alerts")
        self.assertIn("WEBHOOK_URL_ALERTS", result)

    def test_send_webhook_takes_no_url_argument(self) -> None:
        self.assertNotIn("url", notify.send_webhook._tool.schema["properties"])

    def test_email_without_configuration_lists_what_is_missing(self) -> None:
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_TO"):
            os.environ.pop(key, None)
        result = notify.send_email(config=self.config, subject="s", body="b")
        self.assertIn("SMTP_HOST", result)

    def test_save_result_writes_the_file(self) -> None:
        notify.save_result(config=self.config, filename="out.md", content="done")
        self.assertEqual((self.config.workspace / "out.md").read_text(), "done")


class TestNewToolkitsRegister(unittest.TestCase):
    def test_every_new_tool_produces_a_valid_schema(self) -> None:
        for module in (documents, notify):
            registry = ToolRegistry().add_module(module)
            self.assertGreater(len(registry), 0, f"{module.__name__} exposed no tools")
            for spec in registry.to_api_format():
                self.assertTrue(spec["name"])
                self.assertTrue(spec["description"], f"{spec['name']} has no description")
                self.assertEqual(spec["input_schema"]["type"], "object")

    def test_config_is_never_exposed_to_the_model(self) -> None:
        # The whole sandbox depends on this being true for every tool.
        for module in (documents, notify):
            for spec in ToolRegistry().add_module(module).to_api_format():
                self.assertNotIn("config", spec["input_schema"]["properties"])


class TestPipelines(unittest.TestCase):
    def test_every_step_names_a_registered_agent(self) -> None:
        from agents import list_names
        from pipelines import PIPELINES

        known = set(list_names())
        for pipeline in PIPELINES.values():
            for step in pipeline.steps:
                self.assertIn(step.agent, known, f"{pipeline.name} references unknown {step.agent}")

    def test_previous_placeholder_is_substituted_not_formatted(self) -> None:
        # Substitution matters: a previous answer containing braces (JSON,
        # code) would crash str.format but is fine for str.replace.
        task = "Summarise this:\n{previous}"
        previous = 'The extractor returned {"rows": 4}'
        self.assertIn('{"rows": 4}', task.replace("{previous}", previous))

    def test_pipelines_have_at_least_two_steps(self) -> None:
        from pipelines import PIPELINES

        for pipeline in PIPELINES.values():
            self.assertGreaterEqual(len(pipeline.steps), 2, f"{pipeline.name} is not a chain")


class TestExpandedLibrary(unittest.TestCase):
    def test_all_twelve_agents_build(self) -> None:
        from agents import get_spec, list_names

        names = list_names()
        self.assertEqual(len(names), 12)
        for name in names:
            spec = get_spec(name)
            registry = ToolRegistry()
            for module in spec.toolkits:
                registry.add_module(module)
            self.assertGreater(len(registry), 0, f"{name} has no tools")
            self.assertTrue(spec.summary, f"{name} has no summary")
            self.assertTrue(spec.default_task, f"{name} has no default task")

    def test_document_agents_can_read_documents(self) -> None:
        from agents import get_spec

        for name in ("extractor", "report-writer", "meeting-notes", "repurposer"):
            self.assertIn(documents, get_spec(name).toolkits, f"{name} cannot read PDFs")

    def test_code_reviewer_has_no_write_access(self) -> None:
        from agents import get_spec

        registry = ToolRegistry()
        for module in get_spec("code-reviewer").toolkits:
            registry.add_module(module)
        # It may write its review, but it must not be able to send anything.
        self.assertNotIn("send_webhook", registry.tools)
        self.assertNotIn("send_email", registry.tools)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMacOSPathHandling(unittest.TestCase):
    """
    Regression tests for the symlinked-workspace bug.

    On macOS the paths people reach for first are symlinks: /tmp resolves to
    /private/tmp and /var to /private/var. Linux has no symlink there, so a
    workspace whose path contains one is a situation Linux-only CI never
    produces — these tests build it explicitly with os.symlink so the bug
    cannot come back on any platform.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.real = base / "private" / "work"
        self.real.mkdir(parents=True)
        self.link = base / "work"
        try:
            os.symlink(self.real, self.link)      # mimics /tmp -> /private/tmp
        except (OSError, NotImplementedError) as exc:
            # Windows only permits symlinks for an administrator or with
            # Developer Mode on. Skip rather than fail: this is a macOS
            # regression test, and it still runs on Linux and macOS CI.
            self.tmp.cleanup()
            self.skipTest(f"cannot create symlinks on this platform: {exc}")
        (self.real / "note.md").write_text("hello world", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_workspace_is_resolved_even_when_passed_as_an_override(self) -> None:
        # The original bug: normalisation ran before overrides were applied,
        # so --workspace and test overrides skipped it entirely.
        config = Config.load(workspace=self.link, dry_run=False)
        self.assertEqual(config.workspace, self.real.resolve())

    def test_relative_workspace_is_resolved_too(self) -> None:
        config = Config.load(workspace=Path("workspace"), dry_run=True)
        self.assertTrue(config.workspace.is_absolute())
        self.assertEqual(config.workspace, config.workspace.resolve())

    def test_list_files_works_through_a_symlinked_workspace(self) -> None:
        from toolkits import files

        config = Config.load(workspace=self.link, dry_run=False)
        # This raised ValueError before the fix, on macOS only.
        self.assertIn("note.md", files.list_files(config=config))

    def test_every_path_tool_survives_a_symlinked_workspace(self) -> None:
        from toolkits import data, documents, files

        config = Config.load(workspace=self.link, dry_run=False)
        (self.real / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        self.assertIn("hello", files.read_file(config=config, path="note.md"))
        self.assertIn("note.md", files.search_files(config=config, query="hello"))
        self.assertIn("a | b", data.read_csv(config=config, path="rows.csv"))
        self.assertIn("note.md", documents.list_documents(config=config))
        files.write_file(config=config, path="sub/new.txt", content="x")
        self.assertTrue((self.real / "sub" / "new.txt").exists())

    def test_sandbox_still_blocks_escapes_through_a_symlink(self) -> None:
        # The fix must not have widened the sandbox — resolving more paths
        # could in principle have let something through.
        from agentkit.safety import SandboxViolation
        from toolkits import files

        config = Config.load(workspace=self.link, dry_run=False)
        for escape in ("../../../etc/passwd", "/etc/passwd", str(self.real.parent)):
            with self.assertRaises(SandboxViolation, msg=f"{escape} was not blocked"):
                files.read_file(config=config, path=escape)
