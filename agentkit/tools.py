"""
Turning ordinary Python functions into tools the model can call.

The Anthropic API expects each tool as a name, a description, and a JSON
Schema for its inputs. Writing that schema by hand for every function is
tedious and drifts out of sync with the code. So instead we *derive* it:
the `@tool` decorator reads the function's type hints and docstring and
builds the schema automatically.

    @tool
    def read_file(path: str, max_bytes: int = 20_000) -> str:
        '''Read a UTF-8 text file.

        path: file to read, relative to the workspace
        max_bytes: stop after this many bytes
        '''

becomes a tool named `read_file`, with `path` required, `max_bytes`
optional, and per-argument descriptions taken from those docstring lines.
"""

from __future__ import annotations

import inspect
import json
import re
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, get_args, get_origin

# Maps Python annotations onto the JSON Schema types the API understands.
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """Translate one type hint into a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty:
        # No hint at all — accept anything rather than guessing wrong.
        return {}

    origin = get_origin(annotation)

    # `str | None` / `Optional[str]` arrive as a Union. Strip the None and
    # describe the remaining type; optionality is expressed by leaving the
    # argument out of the `required` list, not by the type itself.
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _schema_for_annotation(non_none[0])
        return {}

    # `list[str]` -> an array whose items are strings.
    if origin in (list, set, tuple):
        args = get_args(annotation)
        item = _schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": item or {"type": "string"}}

    if origin is dict:
        return {"type": "object"}

    return {"type": _JSON_TYPES.get(annotation, "string")}


def _split_docstring(func: Callable) -> tuple[str, dict[str, str]]:
    """
    Split a docstring into a summary and per-argument descriptions.

    Everything before the first `name: description` line is the summary
    that describes the tool as a whole. Each `name: description` line after
    that documents one argument.
    """
    doc = inspect.getdoc(func) or ""
    summary_lines: list[str] = []
    params: dict[str, str] = {}
    param_names = set(inspect.signature(func).parameters)

    for line in doc.splitlines():
        match = re.match(r"^\s*(\w+)\s*:\s*(.+)$", line)
        if match and match.group(1) in param_names:
            params[match.group(1)] = match.group(2).strip()
        elif not params:
            # Still in the summary section.
            summary_lines.append(line)

    return "\n".join(summary_lines).strip(), params


@dataclass
class Tool:
    """One callable capability, plus the schema that describes it."""

    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]

    def to_api_format(self) -> dict[str, Any]:
        """The exact shape the Messages API wants in its `tools` list."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }

    def call(self, arguments: dict[str, Any]) -> str:
        """
        Run the tool and return a string for the model to read.

        Tool results must be text, so anything that isn't already a string
        is JSON-encoded. Exceptions are deliberately *not* swallowed here —
        the agent loop catches them and reports them back to the model as an
        error result, which lets the model correct itself and retry.
        """
        result = self.func(**arguments)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)


def tool(func: Callable) -> Callable:
    """
    Decorator that attaches a derived `Tool` to a function.

    The function stays perfectly callable from normal Python — we only hang
    a `_tool` attribute on it, which `ToolRegistry` looks for.
    """
    summary, param_docs = _split_docstring(func)
    signature = inspect.signature(func)
    hints = typing.get_type_hints(func)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, parameter in signature.parameters.items():
        # `config` is injected by the registry at bind time, not chosen by
        # the model, so it never appears in the schema.
        if name in {"self", "config"}:
            continue

        prop = _schema_for_annotation(hints.get(name, parameter.annotation))
        if name in param_docs:
            prop["description"] = param_docs[name]
        properties[name] = prop

        # An argument with no default is one the model must supply.
        if parameter.default is inspect.Parameter.empty:
            required.append(name)

    func._tool = Tool(  # type: ignore[attr-defined]
        name=func.__name__,
        description=summary or f"Call {func.__name__}.",
        schema={
            "type": "object",
            "properties": properties,
            "required": required,
            # Without this the model can send fields we never defined, and
            # they pass validation on their way into the handler. Closing the
            # object turns a silent surprise into a rejected call.
            "additionalProperties": False,
        },
        func=func,
    )
    return func


@dataclass
class ToolRegistry:
    """
    The set of tools one agent is allowed to use.

    Agents are defined largely by which tools they hold: a file organizer
    gets file tools, a web watcher gets fetch tools. Giving each agent the
    narrowest useful set is both a safety measure and an accuracy measure —
    fewer, more relevant tools means fewer wrong turns.
    """

    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *functions: Callable) -> "ToolRegistry":
        """Register decorated functions. Returns self so calls can chain."""
        for func in functions:
            attached = getattr(func, "_tool", None)
            if attached is None:
                raise TypeError(f"{func.__name__} is missing the @tool decorator")
            self.tools[attached.name] = attached
        return self

    def add_module(self, module: Any) -> "ToolRegistry":
        """Register every @tool-decorated function in a module."""
        for value in vars(module).values():
            if callable(value) and hasattr(value, "_tool"):
                self.add(value)
        return self

    def bind(self, **injected: Any) -> "ToolRegistry":
        """
        Pre-supply arguments the model should not control.

        Every file tool needs the `config` object to know where the sandbox
        is, but we must never let the model choose it. `bind` wraps each
        tool so those arguments are filled in before the call, while the
        schema the model sees stays unchanged.
        """
        bound = ToolRegistry()
        for name, existing in self.tools.items():
            wanted = set(inspect.signature(existing.func).parameters)
            extras = {k: v for k, v in injected.items() if k in wanted}
            if not extras:
                bound.tools[name] = existing
                continue

            def make(fn: Callable, fixed: dict[str, Any]) -> Callable:
                def wrapper(**kwargs: Any) -> Any:
                    return fn(**kwargs, **fixed)

                return wrapper

            bound.tools[name] = Tool(
                name=existing.name,
                description=existing.description,
                schema=existing.schema,
                func=make(existing.func, extras),
            )
        return bound

    def to_api_format(self) -> list[dict[str, Any]]:
        return [t.to_api_format() for t in self.tools.values()]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def __len__(self) -> int:
        return len(self.tools)
