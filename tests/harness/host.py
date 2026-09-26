"""Two ways to load this plugin, behind one interface.

`RealHost` drives the project's own PluginManager and registries. `FakeHost`
drives the stand-ins in this package. The tests use whichever the conftest
picked and cannot tell them apart, so the same assertions run against both.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import socket
import sys
from pathlib import Path
from typing import Any

from . import fakes, loader


class Bundle:
    """Everything a loaded plugin exposes, however it was loaded."""

    def __init__(
        self,
        *,
        plugin: Any,
        tools: Any,
        hooks: Any,
        subagents: Any,
        web: Any,
        storage: Any,
        executor: Any,
    ) -> None:
        self.plugin = plugin
        self.tools = tools
        self.hooks = hooks
        self.subagents = subagents
        self.web = web
        self.storage = storage
        self.executor = executor


# ---------------------------------------------------------------------------
# Fake
# ---------------------------------------------------------------------------


def _import_by_path(plugin_dir: Path, filename: str) -> Any | None:
    """Import one plugin module by file path, as the host does."""
    path = plugin_dir / filename
    if not path.is_file():
        return None
    name = f"harness_{plugin_dir.name}_{path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {filename}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instantiate(target: Any, available: dict[str, Any]) -> Any:
    """Construct a decorated class with whichever arguments it accepts."""
    if not inspect.isclass(target):
        return target
    parameters = inspect.signature(target.__init__).parameters
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    kwargs = {k: v for k, v in available.items() if takes_kwargs or k in parameters}
    return target(**kwargs)


class _FunctionTool:
    """Wraps a decorated bare function into the execute() protocol."""

    def __init__(self, function: Any) -> None:
        self._function = function
        self.name = getattr(function, "__name__", "function_tool")

    async def execute(self, payload: dict[str, Any]) -> Any:
        result = self._function(payload)
        return await result if inspect.isawaitable(result) else result


class FakeHost:
    """Loads a plugin with no host source present."""

    is_real = False

    def load(self, plugin_dir: Path, tmp_path: Path) -> Bundle:
        plugin = loader.load_plugin_descriptor(plugin_dir)
        tools = fakes.FakeToolRegistry()
        hooks = fakes.FakeHookManager()
        subagents = fakes.FakeSubagentRegistry(tools)
        web_registry = fakes.FakeWebRegistry()
        storage = fakes.FakeStorage(tmp_path / "data", tmp_path / "attachments")
        executor = fakes.FakeToolExecutor(tools, hooks)

        runtime_context: dict[str, Any] = {
            "storage": storage,
            "plugin_config": plugin.config,
            "tool_registry": tools,
            "subagent_registry": subagents,
            "tool_executor": executor,
            "llm_config": _fake_llm_config(plugin.config),
            "tool_registry_factory": tools.view_for_subagent,
        }

        web_context = fakes.FakeWebContext(plugin.name, web_registry, plugin.config)
        # Same order the host uses, so load-order surprises show up here too.
        targets = (
            ("hooks.py", "register_hooks", hooks),
            ("tools.py", "register_tools", tools),
            ("subagents.py", "register_subagents", subagents),
            ("web.py", "register_web_apis", web_context),
        )
        for filename, entry_point, manager in targets:
            module = _import_by_path(plugin_dir, filename)
            if module is None:
                continue
            register = getattr(module, entry_point, None)
            if register is not None:
                register(manager, plugin, runtime_context)
            self._scan_decorated(
                module,
                plugin,
                tools=tools,
                subagents=subagents,
                web_registry=web_registry,
                runtime_context=runtime_context,
            )

        return Bundle(
            plugin=plugin,
            tools=tools,
            hooks=hooks,
            subagents=subagents,
            web=web_registry,
            storage=storage,
            executor=executor,
        )

    @staticmethod
    def _scan_decorated(
        module: Any,
        plugin: Any,
        *,
        tools: fakes.FakeToolRegistry,
        subagents: fakes.FakeSubagentRegistry,
        web_registry: fakes.FakeWebRegistry,
        runtime_context: dict[str, Any],
    ) -> None:
        injectable = {
            "plugin": plugin,
            "runtime_context": runtime_context,
            "tool_registry": tools,
            "subagent_registry": subagents,
        }
        access = plugin.config.get("tool_access") if isinstance(plugin.config, dict) else {}

        for _, member in inspect.getmembers(module):
            tool_spec = getattr(member, "__plugin_tool_spec__", None)
            if tool_spec is not None:
                metadata = dict(tool_spec.get("metadata") or {})
                declared = (access or {}).get(tool_spec["name"]) if isinstance(access, dict) else None
                if isinstance(declared, dict) and "allowed_subagents" in declared:
                    metadata["allowed_subagents"] = tuple(declared["allowed_subagents"])
                impl = _instantiate(member, injectable) if inspect.isclass(member) else _FunctionTool(member)
                tools.register(impl, name=tool_spec["name"], tags=tool_spec.get("tags", ()), metadata=metadata)

            subagent_spec = getattr(member, "__plugin_subagent_spec__", None)
            if subagent_spec is not None:
                kwargs = {
                    **injectable,
                    "domain": subagent_spec["domain"],
                    "capabilities": subagent_spec.get("capabilities", ()),
                    "tools": None,
                }
                subagents.register(
                    subagent_spec["name"],
                    _instantiate(member, kwargs),
                    domain=subagent_spec["domain"],
                    capabilities=subagent_spec.get("capabilities", ()),
                    metadata=dict(subagent_spec.get("metadata") or {}),
                )

            web_spec = getattr(member, "__plugin_web_api_spec__", None)
            if web_spec is not None:
                web_registry.register(
                    plugin.name,
                    str(web_spec["endpoint"]),
                    member,
                    web_spec.get("methods"),
                    str(web_spec.get("description", "")),
                )

    def collect_commands(self, bundle: Bundle) -> dict[str, Any]:
        return loader.collect_commands(bundle.plugin.config, set(bundle.tools.tools))

    def parse_requirements(self, plugin_dir: Path) -> tuple[str, ...]:
        return loader.parse_requirements(plugin_dir / "requirements.txt")

    def validate_schema(self, schema: Any) -> dict[str, Any]:
        return loader.validate_config_schema(schema)

    def review(self, plugin_dir: Path) -> dict[str, Any] | None:
        _ = plugin_dir
        return None  # Static review needs the host's AST pass.

    async def call_web(self, bundle: Bundle, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
        route = bundle.web.resolve(bundle.plugin.name, endpoint, method)
        request = fakes.FakeRequest(endpoint=endpoint, method=method, **kwargs)
        return await fakes.invoke_handler(route, request)

    @staticmethod
    def execution_error() -> type[BaseException]:
        return fakes.ToolExecutionError


def _fake_llm_config(config: dict[str, Any]) -> Any:
    """The resolved, plugin-scoped LLM config the host injects."""
    from types import SimpleNamespace

    section = config.get("llm") if isinstance(config, dict) else {}
    section = section if isinstance(section, dict) else {}
    # Like the host: 0 or less means "not set", so the provider's 60s applies.
    timeout_sec = float(section.get("timeout_sec") or 0)
    return SimpleNamespace(
        provider=section.get("provider") or "openai",
        provider_name=section.get("provider") or "",
        base_url=section.get("base_url") or "",
        api_key="",
        model=section.get("model") or "gpt-4o",
        temperature=float(section.get("temperature") or 0.2),
        max_tokens=2048,
        timeout_sec=timeout_sec if timeout_sec > 0 else 60.0,
        system_prompt="",
    )


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    """Reserve and release a port, so connecting to it fails immediately."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RealHost:
    """Loads a plugin through the project's own PluginManager."""

    is_real = True

    def load(self, plugin_dir: Path, tmp_path: Path) -> Bundle:
        from agent.coordinator import AgentCoordinator
        from extension.hook import HookManager
        from extension.plugin import PluginManager
        from extension.plugin_storage import PluginStorageFactory
        from extension.skill import SkillManager
        from tool.executor import ToolExecutor
        from tool.registry import ToolRegistry

        tools = ToolRegistry()
        coordinator = AgentCoordinator()
        hooks = HookManager()
        manager = PluginManager(
            hook_manager=hooks,
            skill_manager=SkillManager(
                skills_root=str(tmp_path / ".skills"),
                data_root=str(tmp_path / ".state"),
            ),
            tool_registry=tools,
            subagent_registry=coordinator,
            config_dir=str(tmp_path / ".plugin-config"),
            runtime_context={
                # Point storage at a port nothing listens on, so the plugin takes
                # the same no-Redis path the fake host gives it.
                "plugin_storage_factory": PluginStorageFactory(
                    data_root=tmp_path / "data",
                    user_files_root=tmp_path / "attachments",
                    redis_url=f"redis://127.0.0.1:{_closed_port()}/0",
                ),
                "tool_executor": ToolExecutor(tools, hook_manager=hooks),
                "llm_config": _real_llm_config(),
            },
        )
        # No public equivalent: auto_discover() would scan the whole directory.
        plugin = manager._load_plugin_from_dir(plugin_dir)
        # The template ships disabled; tests are the one place that wants it on.
        plugin.enabled = True
        manager.install(plugin)

        return Bundle(
            plugin=plugin,
            tools=tools,
            hooks=hooks,
            subagents=_CoordinatorView(coordinator),
            web=manager.web_registry,
            storage=None,
            executor=ToolExecutor(tools, hook_manager=hooks),
        )

    def collect_commands(self, bundle: Bundle) -> dict[str, Any]:
        from runtime.command_catalog import RESERVED_COMMANDS, collect_plugin_commands

        return collect_plugin_commands(
            [bundle.plugin], set(bundle.tools.tools), reserved_commands=RESERVED_COMMANDS
        )

    def parse_requirements(self, plugin_dir: Path) -> tuple[str, ...]:
        from extension.plugin_governance import plan_requirements

        return plan_requirements(plugin_dir / "requirements.txt").requirements

    def validate_schema(self, schema: Any) -> dict[str, Any]:
        from extension.plugin_governance import validate_config_schema

        return validate_config_schema(schema)

    def review(self, plugin_dir: Path) -> dict[str, Any] | None:
        from extension.plugin_governance import review_plugin

        return review_plugin(plugin_dir)

    async def call_web(self, bundle: Bundle, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
        from extension.plugin_web import invoke_plugin_handler

        route = bundle.web.resolve(bundle.plugin.name, endpoint, method)
        request = fakes.FakeRequest(endpoint=endpoint, method=method, **kwargs)
        return await invoke_plugin_handler(route, request)

    @staticmethod
    def execution_error() -> type[BaseException]:
        from tool.executor import ToolExecutionError

        return ToolExecutionError


class _CoordinatorView:
    """`get` / `list_agents` over the coordinator, matching the fake registry."""

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    def get(self, name: str) -> Any | None:
        return self._coordinator.registry.get(name)

    def list_agents(self) -> list[str]:
        return list(self._coordinator.list_agents())

    def get_profile(self, name: str) -> dict[str, Any] | None:
        return self._coordinator.registry.get_profile(name)


def _real_llm_config() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        providers={},
        default_provider="openai",
        provider="openai",
        base_url="",
        api_key="",
        model="gpt-4o",
        temperature=0.7,
        max_tokens=2048,
        timeout_sec=60.0,
        system_prompt="",
    )


# ---------------------------------------------------------------------------
# Choosing between them
# ---------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parents[2]
# The file that proves a project checkout is above us; `src` next to it is the
# import root.
HOST_MARKER = Path("src") / "extension" / "plugin.py"
# Top-level packages the project owns. A failed import can leave some of these
# half-loaded, and the stand-ins must not inherit the wreckage.
HOST_PACKAGES = frozenset({"agent", "constants", "core", "extension", "infrastructure", "runtime", "tool"})


def find_host_src(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / HOST_MARKER).is_file():
            return candidate / "src"
    return None


def host_is_importable(host_src: Path) -> bool:
    """Put the project on the path and see whether it really loads.

    Source on disk is not enough: running from the plugin's own environment
    finds the project above but has none of its dependencies installed.
    """
    added = [path for path in (str(host_src), str(host_src.parent)) if path not in sys.path]
    for path in added:
        sys.path.insert(0, path)
    try:
        importlib.import_module("extension.plugin")
    except ImportError:
        for name in [n for n in sys.modules if n.split(".")[0] in HOST_PACKAGES]:
            del sys.modules[name]
        for path in added:
            sys.path.remove(path)
        return False
    return True


def select_host(plugin_dir: Path | None = None) -> Any:
    """Return the real host when the project imports, the stand-in otherwise.

    Shared by `conftest.py` and `main.py`, so both always agree on which half
    of the plugin API is being exercised.
    """
    target = plugin_dir or PLUGIN_DIR
    host_src = find_host_src(target)
    if host_src is not None and host_is_importable(host_src):
        return RealHost()

    # Stand in for the project. This has to happen before any of the plugin's
    # own modules are imported, or their `from extension...` fails.
    from . import shim

    shim.install()
    return FakeHost()
