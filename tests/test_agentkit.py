"""
Tests for the parts that must be right.

These deliberately never call the API. Everything here — schema generation,
the sandbox, tool dispatch, the agent loop — is testable with a fake client,
which means the whole suite runs in under a second and costs nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.agent import Agent
from agentkit.safety import SandboxViolation, guard_write, resolve_in_workspace
from agentkit.tools import ToolRegistry, tool
from core.config import Config


@tool
def sample_tool(name: str, count: int = 3, tags: list[str] | None = None) -> str:
    """Repeat a name a number of times.

    name: the text to repeat
    count: how many times
    tags: optional labels
    """
    return " ".join([name] * count)


class TestSchemaGeneration(unittest.TestCase):
    def test_derives_types_from_hints(self) -> None:
        schema = sample_tool._tool.schema
        self.assertEqual(schema["properties"]["name"]["type"], "string")
        self.assertEqual(schema["properties"]["count"]["type"], "integer")
        self.assertEqual(schema["properties"]["tags"]["type"], "array")

    def test_only_defaultless_args_are_required(self) -> None:
        self.assertEqual(sample_tool._tool.schema["required"], ["name"])

    def test_docstring_is_split_into_summary_and_params(self) -> None:
        spec = sample_tool._tool
        self.assertEqual(spec.description, "Repeat a name a number of times.")
        self.assertEqual(spec.schema["properties"]["count"]["description"], "how many times")

    def test_optional_type_unwraps_to_its_inner_type(self) -> None:
        # `list[str] | None` must describe an array, not a union.
        self.assertNotIn("anyOf", sample_tool._tool.schema["properties"]["tags"])


class TestRegistry(unittest.TestCase):
    def test_rejects_undecorated_functions(self) -> None:
        with self.assertRaises(TypeError):
            ToolRegistry().add(lambda x: x)

    def test_bind_injects_hidden_arguments(self) -> None:
        @tool
        def needs_config(text: str, config: object = None) -> str:
            """Echo text with the injected config.

            text: anything
            """
            return f"{text}:{config}"

        bound = ToolRegistry().add(needs_config).bind(config="INJECTED")
        # The model supplies only `text`; `config` arrives from bind().
        self.assertEqual(bound.get("needs_config").call({"text": "hi"}), "hi:INJECTED")

    def test_bind_leaves_the_schema_untouched(self) -> None:
        bound = ToolRegistry().add(sample_tool).bind(config="X")
        self.assertEqual(bound.get("sample_tool").schema, sample_tool._tool.schema)


class TestSandbox(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.load(workspace=Path(self.tmp.name).resolve(), dry_run=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_relative_path_resolves_inside(self) -> None:
        resolved = resolve_in_workspace(self.config, "notes/a.txt")
        self.assertTrue(str(resolved).startswith(str(self.config.workspace)))

    def test_parent_traversal_is_blocked(self) -> None:
        with self.assertRaises(SandboxViolation):
            resolve_in_workspace(self.config, "../../etc/passwd")

    def test_absolute_escape_is_blocked(self) -> None:
        with self.assertRaises(SandboxViolation):
            resolve_in_workspace(self.config, "/etc/passwd")

    def test_sibling_prefix_is_not_treated_as_inside(self) -> None:
        # /tmp/xyz-evil must not pass a check for /tmp/xyz.
        sibling = self.config.workspace.parent / (self.config.workspace.name + "-evil")
        with self.assertRaises(SandboxViolation):
            resolve_in_workspace(self.config, str(sibling))

    def test_dry_run_blocks_writes_with_a_message(self) -> None:
        dry = Config.load(workspace=self.config.workspace, dry_run=True)
        self.assertIn("[dry run]", guard_write(dry, "delete everything") or "")
        self.assertIsNone(guard_write(self.config, "delete everything"))


# --- a fake API client, so the loop can be tested without the network -------

def _block(**kwargs):
    return SimpleNamespace(**kwargs)


class FakeClient:
    """Replays a scripted list of responses in place of the real SDK."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        # Snapshot the message list. The agent appends to the same list on
        # every turn, so storing it by reference would let later turns
        # rewrite what we recorded for earlier ones.
        recorded = dict(kwargs)
        recorded["messages"] = list(kwargs["messages"])
        self.calls.append(recorded)
        return self._responses.pop(0)


class TestAgentLoop(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.load(workspace=Path(self.tmp.name), dry_run=True, max_steps=5)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _agent(self, responses: list) -> Agent:
        return Agent(
            name="test",
            system_prompt="test",
            tools=ToolRegistry().add(sample_tool),
            config=self.config,
            client=FakeClient(responses),
            max_tokens=100,
        )

    def test_returns_text_when_no_tools_are_called(self) -> None:
        agent = self._agent([
            _block(
                content=[_block(type="text", text="All done.")],
                usage=_block(input_tokens=10, output_tokens=5),
                stop_reason="end_turn",
            )
        ])
        result = agent.run("hi", verbose=False)
        self.assertEqual(result.text, "All done.")
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.input_tokens, 10)

    def test_executes_a_tool_then_finishes(self) -> None:
        agent = self._agent([
            _block(
                content=[_block(type="tool_use", id="t1", name="sample_tool",
                                input={"name": "ha", "count": 2})],
                usage=_block(input_tokens=10, output_tokens=5),
                stop_reason="tool_use",
            ),
            _block(
                content=[_block(type="text", text="Finished.")],
                usage=_block(input_tokens=20, output_tokens=8),
                stop_reason="end_turn",
            ),
        ])
        result = agent.run("go", verbose=False)

        self.assertEqual(result.text, "Finished.")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.tool_calls[0]["tool"], "sample_tool")
        self.assertFalse(result.tool_calls[0]["error"])
        # Token counts accumulate across every turn of the loop.
        self.assertEqual(result.input_tokens, 30)

    def test_tool_errors_are_reported_to_the_model_not_raised(self) -> None:
        agent = self._agent([
            _block(
                content=[_block(type="tool_use", id="t1", name="no_such_tool", input={})],
                usage=_block(input_tokens=1, output_tokens=1),
                stop_reason="tool_use",
            ),
            _block(
                content=[_block(type="text", text="Recovered.")],
                usage=_block(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            ),
        ])
        result = agent.run("go", verbose=False)

        self.assertEqual(result.text, "Recovered.")
        self.assertTrue(result.tool_calls[0]["error"])
        # The error was fed back as a tool_result the model could read.
        follow_up = agent.client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(follow_up["is_error"])
        self.assertIn("No such tool", follow_up["content"])

    def test_stops_at_the_step_ceiling(self) -> None:
        looping = [
            _block(
                content=[_block(type="tool_use", id=f"t{i}", name="sample_tool",
                                input={"name": "x"})],
                usage=_block(input_tokens=1, output_tokens=1),
                stop_reason="tool_use",
            )
            for i in range(10)
        ]
        result = self._agent(looping).run("loop forever", verbose=False)

        self.assertTrue(result.stopped_early)
        self.assertEqual(result.steps, self.config.max_steps)


class TestAgentLibrary(unittest.TestCase):
    def test_every_agent_builds_and_has_tools(self) -> None:
        from agents import get_spec, list_names
        from agentkit.tools import ToolRegistry

        self.assertGreater(len(list_names()), 0)
        for name in list_names():
            spec = get_spec(name)
            registry = ToolRegistry()
            for module in spec.toolkits:
                registry.add_module(module)
            self.assertGreater(len(registry), 0, f"{name} has no tools")
            self.assertIn("automation agent", spec.system_prompt())

    def test_unknown_agent_name_is_a_clear_error(self) -> None:
        from agents import get_spec

        with self.assertRaises(KeyError):
            get_spec("does-not-exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
