"""Stand-ins for the host modules a plugin imports.

A plugin's source says `from extension.plugin import plugin_tool` and
`from extension.hook import HookPoint`. Those live in the host, so without the
host checked out the plugin will not even import. Installing these module
objects into `sys.modules` before the plugin is loaded makes the same source
work unchanged - decorators included.

Only what plugin code imports is provided. Everything the *host* does with
those declarations lives in `fakes.py`.
"""

from __future__ import annotations

import mimetypes
import sys
import types
from enum import StrEnum
from pathlib import Path
from typing import Any

# The ten lifecycle points, in the runtime's execution order. Mirrors
# src/extension/hook.py; a point added there and not here means the harness
# silently ignores handlers registered at it.
HOOK_POINTS = (
    "before_route",
    "before_plan",
    "after_plan_graph",
    "before_node_execute",
    "after_node_execute",
    "before_tool_call",
    "after_tool_call",
    "on_node_error",
    "after_workflow",
    "on_workflow_error",
)

HookPoint = StrEnum("HookPoint", {name: name for name in HOOK_POINTS})


def plugin_tool(
    name: str,
    *,
    tags: tuple[str, ...] = (),
    allowed_subagents: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Mark a class or function as a plugin tool."""

    def decorate(target: Any) -> Any:
        target.__plugin_tool_spec__ = {
            "name": name,
            "tags": tags,
            "metadata": {
                **(metadata or {}),
                **({"allowed_subagents": allowed_subagents} if allowed_subagents else {}),
            },
        }
        return target

    return decorate


def plugin_subagent(
    name: str,
    *,
    domain: str,
    capabilities: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    metadata: dict[str, str] | None = None,
) -> Any:
    """Mark a class as a plugin subagent."""

    def decorate(target: Any) -> Any:
        target.__plugin_subagent_spec__ = {
            "name": name,
            "domain": domain,
            "capabilities": capabilities,
            "metadata": {**(metadata or {}), **({"allowed_tools": tools} if tools else {})},
        }
        return target

    return decorate


def plugin_web_api(endpoint: str, *, methods: tuple[str, ...] = ("GET",), description: str = "") -> Any:
    """Mark a module-level function as a plugin web endpoint."""

    def decorate(target: Any) -> Any:
        target.__plugin_web_api_spec__ = {
            "endpoint": endpoint,
            "methods": tuple(methods),
            "description": description,
        }
        return target

    return decorate


class PluginWebError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeResponse:
    """What the response helpers return, in place of a FastAPI Response.

    Real handlers return these objects straight to the host, so tests read
    `.status_code`, `.media_type` and `.body` off them the same way either way.
    """

    def __init__(
        self,
        *,
        content: Any = None,
        body: bytes = b"",
        status_code: int = 200,
        media_type: str = "application/json",
        headers: dict[str, str] | None = None,
        path: str = "",
        filename: str = "",
        generator: Any = None,
    ) -> None:
        self.content = content
        self.body = body
        self.status_code = status_code
        self.media_type = media_type
        self.headers = headers or {}
        self.path = path
        self.filename = filename
        self.generator = generator


def json_response(payload: Any, status_code: int = 200) -> FakeResponse:
    return FakeResponse(content=payload, status_code=status_code, media_type="application/json")


def error_response(message: str, status_code: int = 400) -> FakeResponse:
    return FakeResponse(
        content={"status": "error", "message": str(message)},
        status_code=status_code,
        media_type="application/json",
    )


def file_response(path: Any, filename: str = "", media_type: str = "") -> FakeResponse:
    resolved = Path(path)
    return FakeResponse(
        path=str(resolved),
        filename=filename or resolved.name,
        media_type=media_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
    )


def stream_response(generator: Any, media_type: str = "text/event-stream") -> FakeResponse:
    return FakeResponse(generator=generator, media_type=media_type)


def bytes_response(payload: bytes, media_type: str, headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(
        body=payload,
        media_type=media_type,
        headers=headers or {"Cache-Control": "no-store"},
    )


def install() -> None:
    """Put the stand-ins in `sys.modules`, unless the real host is already there."""
    if "extension.plugin" in sys.modules:
        return

    package = sys.modules.get("extension")
    if package is None:
        package = types.ModuleType("extension")
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules["extension"] = package

    plugin_module = types.ModuleType("extension.plugin")
    plugin_module.plugin_tool = plugin_tool  # type: ignore[attr-defined]
    plugin_module.plugin_subagent = plugin_subagent  # type: ignore[attr-defined]

    hook_module = types.ModuleType("extension.hook")
    hook_module.HookPoint = HookPoint  # type: ignore[attr-defined]

    web_module = types.ModuleType("extension.plugin_web")
    for helper in (
        plugin_web_api,
        json_response,
        error_response,
        file_response,
        stream_response,
        bytes_response,
    ):
        setattr(web_module, helper.__name__, helper)
    web_module.PluginWebError = PluginWebError  # type: ignore[attr-defined]

    for name, module in (
        ("extension.plugin", plugin_module),
        ("extension.hook", hook_module),
        ("extension.plugin_web", web_module),
    ):
        sys.modules[name] = module
        setattr(package, name.split(".", maxsplit=1)[1], module)
