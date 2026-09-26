"""The host's half: registries, storage, the web surface and a tool executor.

Each class mirrors the API the real host hands a plugin, and nothing more. When
a signature here drifts from the real one, the in-repo run of the same tests
against the real host fails first - that is the only thing keeping this honest.
"""

from __future__ import annotations

import inspect
import re
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any

from .shim import HOOK_POINTS, HookPoint, PluginWebError

UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _segment(value: Any, *, fallback: str = "unnamed") -> str:
    text = UNSAFE_SEGMENT.sub("-", str(value or "").strip()).strip("-.")
    return text or fallback


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class FakeStorage:
    """A plugin-scoped storage service backed by a temp directory.

    `client()` always returns None, so a plugin's Redis-unavailable path is the
    one under test. A plugin that cannot work without Redis should say so by
    failing here rather than by silently passing.
    """

    def __init__(self, root: Path, user_files_root: Path) -> None:
        self._root = root
        self._user_files_root = user_files_root

    def dir(self, *parts: str) -> Path:
        target = self._join(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def path(self, *parts: str) -> Path:
        target = self._join(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _join(self, *parts: str) -> Path:
        return self._root.joinpath(*(_segment(part) for part in parts if str(part).strip()))

    def resolve(self, raw_path: str | Path) -> Path | None:
        return _resolved_file(raw_path, self._root)

    def user_file(self, raw_path: str | Path) -> Path | None:
        return _resolved_file(raw_path, self._user_files_root)

    def key(self, *parts: str) -> str:
        return ":".join(["fake:plugins", *(_segment(p) for p in parts if str(p).strip())])

    def client(self) -> Any | None:
        return None

    def available(self) -> bool:
        return False

    def close(self) -> None:
        return None


def _resolved_file(raw_path: str | Path, root: Path) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser().resolve()
        anchor = root.resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(anchor) or not candidate.is_file():
        return None
    return candidate


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolSpec:
    def __init__(
        self,
        name: str,
        impl: Any,
        allowed_roles: tuple[str, ...],
        tags: tuple[str, ...],
        metadata: dict[str, Any],
    ) -> None:
        self.name = name
        self.impl = impl
        self.allowed_roles = allowed_roles
        self.tags = tags
        self.metadata = metadata
        self.consequential = _resolve_consequential(impl, metadata)


def _resolve_consequential(tool: Any, metadata: dict[str, Any]) -> bool:
    declared = getattr(tool, "consequential", None)
    if isinstance(declared, bool):
        return declared
    raw = metadata.get("consequential")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"true", "1", "yes"}


def _resolve_metadata(tool: Any, metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Fill description and input_schema in from the tool object, as the host does."""
    resolved = dict(metadata or {})
    if not resolved.get("description"):
        description = getattr(tool, "description", None)
        if isinstance(description, str) and description.strip():
            resolved["description"] = description.strip()
    if not (resolved.get("input_schema") or resolved.get("parameters_schema")):
        declared = (
            getattr(tool, "parameters", None)
            or getattr(tool, "input_schema", None)
            or getattr(tool, "parameters_schema", None)
        )
        if isinstance(declared, dict) and declared.get("type") == "object":
            resolved["input_schema"] = declared
        elif isinstance(declared, str) and declared.strip():
            resolved["input_schema"] = declared.strip()
    return resolved


class FakeToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, ToolSpec] = {}

    def register(
        self,
        tool: Any,
        *,
        name: str | None = None,
        allowed_roles: tuple[str, ...] = ("user", "admin"),
        tags: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        resolved_name = str(name or getattr(tool, "name", "")).strip()
        if not resolved_name:
            msg = "Tool object must provide a non-empty name"
            raise ValueError(msg)
        self.tools[resolved_name] = ToolSpec(
            resolved_name, tool, allowed_roles, tags, _resolve_metadata(tool, metadata)
        )
        return resolved_name

    def get(self, name: str) -> Any:
        return self.tools[name].impl

    def has(self, name: str) -> bool:
        return name in self.tools

    def view_for_subagent(self, subagent_name: str, allowed_tool_names: tuple[str, ...] | None = None) -> Any:
        return FilteredToolView(self, subagent_name, allowed_tool_names)


class FilteredToolView:
    """What a subagent sees: only the tools both sides allow."""

    def __init__(self, registry: FakeToolRegistry, subagent: str, allowed: tuple[str, ...] | None) -> None:
        self._registry = registry
        self._subagent = subagent
        self._allowed = set(allowed) if allowed is not None else None

    def _visible(self, name: str) -> bool:
        if self._allowed is not None and name not in self._allowed:
            return False
        spec = self._registry.tools.get(name)
        if spec is None:
            return False
        declared = spec.metadata.get("allowed_subagents")
        return not declared or self._subagent in tuple(declared)

    def get(self, name: str) -> Any:
        return self._registry.get(name) if self._visible(name) else None

    def has(self, name: str) -> bool:
        return self._visible(name)

    def list_tools(self) -> list[str]:
        return [name for name in self._registry.tools if self._visible(name)]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


class HookRegistration:
    def __init__(self, plugin_name: str, point: Any, handler: Any, priority: int) -> None:
        self.plugin_name = plugin_name
        self.point = HookPoint(str(point))
        self.handler = handler
        self.priority = priority


class FakeHookManager:
    def __init__(self) -> None:
        self._hooks: dict[Any, list[HookRegistration]] = defaultdict(list)

    def register(self, plugin_name: str, point: Any, handler: Any, *, priority: int = 100) -> None:
        registration = HookRegistration(plugin_name, point, handler, priority)
        if str(registration.point) not in HOOK_POINTS:
            msg = f"unknown hook point {point!r}"
            raise ValueError(msg)
        bucket = self._hooks[registration.point]
        bucket.insert(bisect_right([item.priority for item in bucket], priority), registration)

    def list_hooks(self, point: Any | None = None) -> list[HookRegistration]:
        if point is not None:
            return list(self._hooks.get(HookPoint(str(point)), []))
        return [item for bucket in self._hooks.values() for item in bucket]

    async def trigger(self, point: Any, context: dict[str, Any]) -> dict[str, Any]:
        current = context
        for registration in self._hooks.get(HookPoint(str(point)), []):
            result = await registration.handler(current)
            if result is not None:
                current = result
            # A respond or drop ends the inbound turn, so later handlers of
            # every plugin are skipped.
            if str(point) == "before_route":
                outcome = current.get("route_outcome")
                if isinstance(outcome, dict) and outcome.get("action") in {"respond", "drop"}:
                    break
        return current

    def clear(self, plugin_name: str | None = None) -> None:
        if plugin_name is None:
            self._hooks.clear()
            return
        for point in self._hooks:
            self._hooks[point] = [r for r in self._hooks[point] if r.plugin_name != plugin_name]


# ---------------------------------------------------------------------------
# SubAgents
# ---------------------------------------------------------------------------


class FakeSubagentRegistry:
    def __init__(self, tool_registry: FakeToolRegistry) -> None:
        self._agents: dict[str, Any] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._tool_registry = tool_registry

    def register(
        self,
        name: str,
        agent: Any,
        *,
        domain: str | None = None,
        capabilities: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        resolved_name = str(name).strip()
        if not resolved_name:
            msg = "Subagent name must be non-empty"
            raise ValueError(msg)
        resolved_metadata = dict(metadata or {})
        allowed = resolved_metadata.get("allowed_tools")
        # The host replaces the agent's `tools` attribute with a filtered view,
        # so an agent that declares one never sees the full registry.
        if hasattr(agent, "tools"):
            agent.tools = self._tool_registry.view_for_subagent(
                resolved_name, tuple(allowed) if allowed else None
            )
        self._agents[resolved_name] = agent
        self._profiles[resolved_name] = {
            "domain": domain,
            "capabilities": tuple(capabilities or ()),
            "metadata": resolved_metadata,
        }

    def get(self, name: str) -> Any | None:
        return self._agents.get(name)

    def list_agents(self) -> list[str]:
        return list(self._agents)

    def get_profile(self, name: str) -> dict[str, Any] | None:
        return self._profiles.get(name)


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def normalize_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip().strip("/")
    if not value:
        raise PluginWebError("endpoint is required", status_code=422)
    if "\\" in value or "\x00" in value:
        raise PluginWebError("endpoint contains an illegal character", status_code=422)
    if "://" in value:
        raise PluginWebError("endpoint must not include a scheme", status_code=422)
    if any(segment in ("", ".", "..") for segment in value.split("/")):
        raise PluginWebError("endpoint must not contain empty or relative segments", status_code=422)
    return value


class WebRoute:
    def __init__(self, plugin: str, endpoint: str, methods: tuple[str, ...], handler: Any, description: str) -> None:
        self.plugin = plugin
        self.endpoint = endpoint
        self.methods = methods
        self.handler = handler
        self.description = description


class FakeWebRegistry:
    def __init__(self) -> None:
        self._routes: dict[str, dict[str, WebRoute]] = {}

    def register(
        self,
        plugin: str,
        endpoint: str,
        handler: Any,
        methods: Any = None,
        description: str = "",
    ) -> None:
        resolved = normalize_endpoint(endpoint)
        chosen = tuple(str(m).upper() for m in (methods or ("GET",)))
        for method in chosen:
            if method not in ALLOWED_METHODS:
                msg = f"method {method} is not allowed"
                raise PluginWebError(msg, status_code=422)
        self._routes.setdefault(plugin, {})[resolved] = WebRoute(plugin, resolved, chosen, handler, description)

    def resolve(self, plugin: str, endpoint: str, method: str) -> WebRoute:
        routes = self._routes.get(plugin) or {}
        route = routes.get(normalize_endpoint(endpoint))
        if route is None:
            msg = f"'{endpoint}' is not published by plugin '{plugin}'"
            raise PluginWebError(msg, status_code=404)
        if method.upper() not in route.methods:
            msg = f"method {method.upper()} is not allowed here"
            raise PluginWebError(msg, status_code=405)
        return route

    def list_for(self, plugin: str) -> list[WebRoute]:
        return list((self._routes.get(plugin) or {}).values())


class FakeWebContext:
    """The `web` facade a plugin's `register_web_apis` receives."""

    def __init__(self, plugin_name: str, registry: FakeWebRegistry, plugin_config: dict[str, Any]) -> None:
        self.plugin_name = plugin_name
        self.config = plugin_config
        self._registry = registry

    def register_web_api(self, endpoint: str, handler: Any, methods: Any = None, description: str = "") -> None:
        self._registry.register(self.plugin_name, endpoint, handler, methods, description)

    def routes(self) -> list[WebRoute]:
        return self._registry.list_for(self.plugin_name)


class FakeUpload:
    def __init__(self, payload: bytes, filename: str, content_type: str) -> None:
        self._payload = payload
        self.filename = filename
        self.content_type = content_type

    async def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


class FakeRequest:
    """The narrow request view a plugin handler is given."""

    def __init__(
        self,
        username: str,
        *,
        method: str = "GET",
        endpoint: str = "",
        query: dict[str, str] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        upload: bytes | None = None,
        filename: str = "upload.bin",
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.username = username
        self.method = method.upper()
        self.endpoint = endpoint
        self.path = endpoint
        self.plugin_name = ""
        self.query = query or {}
        self.headers = headers or {}
        self._body = body
        self._form = form or {}
        self._upload = upload
        self._filename = filename
        self._content_type = content_type

    async def json(self, default: Any = None) -> Any:
        if self._body is not None:
            return self._body
        return {} if default is None else default

    async def body(self) -> bytes:
        return self._upload or b""

    async def form(self) -> dict[str, Any]:
        return self._form

    async def files(self) -> dict[str, Any]:
        if self._upload is None:
            return {}
        return {"file": FakeUpload(self._upload, self._filename, self._content_type)}


async def invoke_handler(route: WebRoute, request: FakeRequest) -> Any:
    """Call a handler, awaiting it only when it is a coroutine."""
    result = route.handler(request)
    return await result if inspect.isawaitable(result) else result


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


class ToolExecutionError(RuntimeError):
    """Raised when a tool call fails or a hook refuses it."""


class FakeToolExecutor:
    """Runs a tool the way the host does: through the tool-call hooks."""

    def __init__(self, registry: FakeToolRegistry, hook_manager: FakeHookManager) -> None:
        self.registry = registry
        self.hook_manager = hook_manager

    async def execute(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        trace_id: str = "trace-missing",
        timeout_sec: float | None = None,
        authorization_context: dict[str, Any] | None = None,
    ) -> Any:
        _ = timeout_sec
        if not self.registry.has(tool_name):
            msg = f"tool '{tool_name}' is not registered"
            raise ToolExecutionError(msg)

        context = await self.hook_manager.trigger(
            HookPoint.before_tool_call,
            {
                "trace_id": trace_id,
                "tool": tool_name,
                "payload": dict(payload),
                "authorization_context": dict(authorization_context or {}),
                "denied": False,
                "reason": "",
            },
        )
        if context.get("denied") is True:
            reason = str(context.get("reason") or "") or f"tool '{tool_name}' was denied by a plugin hook"
            raise ToolExecutionError(reason)
        updated = context.get("payload")
        if not isinstance(updated, dict):
            msg = "before_tool_call hook must preserve payload as an object"
            raise TypeError(msg)

        try:
            result = await self.registry.get(tool_name).execute(updated)
        except Exception as exc:  # mirrors how the host normalises a tool failure
            msg = f"tool '{tool_name}' failed: {exc}"
            raise ToolExecutionError(msg) from exc

        after = await self.hook_manager.trigger(
            HookPoint.after_tool_call,
            {"trace_id": trace_id, "tool": tool_name, "payload": dict(updated), "result": result},
        )
        if "result" not in after:
            msg = "after_tool_call hook must preserve result"
            raise KeyError(msg)
        return after["result"]
