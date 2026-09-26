"""Checks every plugin should pass, derived from the plugin's own declarations.

Nothing here names a tool, command or endpoint. Each check reads what this
plugin declares - in `plugin.yaml`, `_conf_schema.json`, `SKILL.md`, `pages/`
and its registrations - and confirms the pieces agree with one another. Change
what the plugin does and the checks follow; they need no editing.

What they are for: the plugin API fails quietly. A command whose `tool` is
misspelled is dropped without a word. A widget pointing at an endpoint that was
never registered renders as an empty box. A page calling `apiGet('stat')` when
the endpoint is `stats` fails only in the browser, in production. Every check
below is one of those.

`main.py doctor` and `test_plugin_contract.py` both run exactly this, so they
can never disagree.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from . import pageaudit

if TYPE_CHECKING:
    from pathlib import Path

# Widget types the dashboard knows how to render. Mirrors
# frontend/src/api/types/pluginUi.ts.
WIDGET_TYPES = frozenset(
    {
        "stat",
        "table",
        "line-chart",
        "bar-chart",
        "log-stream",
        "form",
        "action",
        "markdown",
        "key-value",
    }
)
HOOK_POINTS = frozenset(
    {
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
    }
)
CONFIG_TYPES = frozenset(
    {"string", "text", "int", "float", "bool", "object", "list", "dict", "template_list", "file"}
)
# Keys the manifest's `config:` owns and the schema must not also claim.
STRUCTURAL_CONFIG_KEYS = frozenset({"commands", "tool_access"})
# A tool called `search` or `run` will collide with a builtin one day; a tool
# carrying a word from its plugin's name will not.
MIN_TOKEN_LENGTH = 3
GENERIC_NAME_PARTS = frozenset({"plugin", "tool", "tools", "the", "and", "for"})
PROBE_USER = "contract-check"


@dataclass(frozen=True)
class Finding:
    """One thing that will not behave the way its author expects."""

    code: str
    message: str
    hint: str = ""
    where: str = ""

    def render(self) -> str:
        location = f" [{self.where}]" if self.where else ""
        hint = f"\n      {self.hint}" if self.hint else ""
        return f"{self.code}{location}: {self.message}{hint}"


@dataclass
class CheckContext:
    """What a check needs: the plugin on disk, and the plugin as loaded."""

    plugin_dir: Path
    host: Any
    bundle: Any
    manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.manifest:
            path = next(
                (self.plugin_dir / name for name in ("plugin.yaml", "plugin.yml") if (self.plugin_dir / name).is_file()),
                None,
            )
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
            self.manifest = raw if isinstance(raw, dict) else {}

    @property
    def config(self) -> dict[str, Any]:
        raw = self.manifest.get("config")
        return raw if isinstance(raw, dict) else {}

    @property
    def routes(self) -> dict[str, tuple[str, ...]]:
        """Registered endpoint -> the methods it accepts."""
        return {
            route.endpoint: tuple(route.methods)
            for route in self.bundle.web.list_for(self.bundle.plugin.name)
        }

    @property
    def tool_names(self) -> set[str]:
        return set(self.bundle.tools.tools)

    def call(self, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
        return asyncio.run(self.host.call_web(self.bundle, endpoint, method, username=PROBE_USER, **kwargs))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def check_manifest(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    name = str(context.manifest.get("name") or "")
    if not name:
        findings.append(Finding("manifest.name", "plugin.yaml declares no name", "The directory name is used instead."))
    elif name != context.plugin_dir.name:
        findings.append(
            Finding(
                "manifest.name",
                f"manifest name {name!r} does not match the directory {context.plugin_dir.name!r}",
                "A repository install requires them to match.",
            )
        )
    if not str(context.manifest.get("description") or "").strip():
        findings.append(
            Finding("manifest.description", "plugin.yaml has no description", "It is what the plugin list shows.")
        )
    return findings


def check_commands(context: CheckContext) -> list[Finding]:
    """Every declared command must survive the runtime's silent drop rules."""
    declared = context.config.get("commands")
    if not isinstance(declared, list):
        return []
    surviving = set(context.host.collect_commands(context.bundle))
    findings: list[Finding] = []
    for entry in declared:
        if not isinstance(entry, dict):
            findings.append(Finding("command.shape", f"a commands entry is not a mapping: {entry!r}"))
            continue
        command = str(entry.get("command", "")).strip().lower()
        if command in surviving:
            continue
        findings.append(
            Finding(
                "command.dropped",
                f"{command or entry!r} is declared but the runtime drops it",
                "Check: the name starts with '/', route is task or tool, priority is an integer, "
                "and both `tool` and `requires_tool` name registered tools.",
                where="plugin.yaml",
            )
        )
    return findings


def check_tool_access(context: CheckContext) -> list[Finding]:
    """A tool_access key that names nothing is an allow-list doing nothing."""
    access = context.config.get("tool_access")
    if not isinstance(access, dict):
        return []
    known = context.tool_names
    return [
        Finding(
            "tool_access.unknown_tool",
            f"tool_access names {tool!r}, which is not registered",
            "The entry has no effect. Fix the name or drop it.",
            where="plugin.yaml",
        )
        for tool in access
        if tool not in known
    ]


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------


def plugin_name_tokens(plugin_name: str) -> set[str]:
    """The words a plugin's own names should be recognisable by."""
    parts = re.split(r"[-_\s]+", plugin_name.lower())
    return {part for part in parts if len(part) >= MIN_TOKEN_LENGTH and part not in GENERIC_NAME_PARTS}


def check_tools(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    tokens = plugin_name_tokens(context.bundle.plugin.name)
    for name, spec in context.bundle.tools.tools.items():
        if not spec.metadata.get("description"):
            findings.append(
                Finding(
                    "tool.no_description",
                    f"{name} declares no description",
                    "The planner has nothing to decide from. Add a `description` attribute or metadata entry.",
                )
            )
        if not (spec.metadata.get("input_schema") or spec.metadata.get("parameters_schema")):
            findings.append(
                Finding(
                    "tool.no_schema",
                    f"{name} declares no input schema",
                    "Add a `parameters` attribute: a JSON Schema object.",
                )
            )
        if not isinstance(spec.consequential, bool):
            findings.append(Finding("tool.consequential", f"{name} has a non-boolean consequential flag"))
        if tokens and not any(token in name.lower() for token in tokens):
            findings.append(
                Finding(
                    "tool.generic_name",
                    f"{name} carries nothing from the plugin name {context.bundle.plugin.name!r}",
                    "Registering a name a builtin tool already uses is fatal at startup. "
                    f"Work one of {', '.join(sorted(tokens))} into it.",
                )
            )
    return findings


def check_subagents(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    known = context.tool_names
    for name in context.bundle.subagents.list_agents():
        profile = context.bundle.subagents.get_profile(name) or {}
        if not profile.get("domain"):
            findings.append(
                Finding(
                    "subagent.no_domain",
                    f"{name} declares no domain",
                    "The planner matches on domain and capabilities; without them it cannot route to this agent.",
                )
            )
        if not profile.get("capabilities"):
            findings.append(Finding("subagent.no_capabilities", f"{name} declares no capabilities"))
        allowed = (profile.get("metadata") or {}).get("allowed_tools") or ()
        findings.extend(
            Finding(
                "subagent.unknown_tool",
                f"{name} allows {tool!r}, which is not registered",
                "The agent will look for a tool that does not exist.",
            )
            for tool in allowed
            if tool not in known
        )
    return findings


def check_hooks(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    for registration in context.bundle.hooks.list_hooks():
        point = str(registration.point)
        if point not in HOOK_POINTS:
            findings.append(Finding("hook.unknown_point", f"a handler is registered at unknown point {point!r}"))
        if registration.plugin_name != context.bundle.plugin.name:
            findings.append(
                Finding(
                    "hook.wrong_owner",
                    f"a {point} handler is registered under {registration.plugin_name!r}",
                    "Register with `plugin.name`, or the host cannot clean it up on unload.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Configuration schema
# ---------------------------------------------------------------------------


def check_config_schema(context: CheckContext) -> list[Finding]:
    schema_path = context.plugin_dir / "_conf_schema.json"
    if not schema_path.is_file():
        return []
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        return [Finding("schema.invalid_json", f"_conf_schema.json is not valid JSON: {exc}")]
    try:
        context.host.validate_schema(raw)
    except ValueError as exc:
        return [Finding("schema.invalid", str(exc), "The config form will not render.")]

    findings = [
        Finding(
            "schema.duplicate_key",
            f"{key!r} is declared in both _conf_schema.json and the manifest's config:",
            "The saved schema value always wins, so the manifest default is dead.",
        )
        for key in raw
        if key in context.config
    ]
    findings.extend(
        Finding("schema.structural_key", f"{key!r} belongs in the manifest, not the schema")
        for key in raw
        if key in STRUCTURAL_CONFIG_KEYS
    )
    return findings


def check_requirements(context: CheckContext) -> list[Finding]:
    try:
        context.host.parse_requirements(context.plugin_dir)
    except ValueError as exc:
        return [
            Finding(
                "requirements.rejected",
                str(exc),
                "Options, local paths, VCS entries and direct URLs are refused by the loader.",
            )
        ]
    return []


def check_skill(context: CheckContext) -> list[Finding]:
    path = next(
        (context.plugin_dir / name for name in ("SKILL.md", "skill.md") if (context.plugin_dir / name).is_file()),
        None,
    )
    if path is None:
        return []
    text = path.read_text(encoding="utf-8")
    frontmatter = read_frontmatter(text)
    if frontmatter is None:
        return [
            Finding(
                "skill.no_frontmatter",
                f"{path.name} has no YAML frontmatter",
                "Open the file with a --- block declaring name and description.",
            )
        ]
    return [
        Finding("skill.missing_field", f"{path.name} frontmatter declares no {key}")
        for key in ("name", "description")
        if not str(frontmatter.get(key) or "").strip()
    ]


def read_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse a SKILL.md YAML frontmatter block, or None when there is not one."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return None
    try:
        payload = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Dashboard surface
# ---------------------------------------------------------------------------


def check_pages(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    declared = {page.name for page in context.bundle.plugin.ui.pages}
    on_disk = {directory.name for directory in pageaudit.page_directories(context.plugin_dir)}

    findings.extend(
        Finding(
            "page.missing_directory",
            f"ui.pages declares {name!r} but pages/{name}/index.html does not exist",
            "The page is silently ignored.",
            where="plugin.yaml",
        )
        for name in sorted(declared)
        if not (context.plugin_dir / "pages" / name / pageaudit.PAGE_ENTRY_FILE).is_file()
    )
    findings.extend(
        Finding(
            "page.undeclared",
            f"pages/{name}/ exists but no ui.pages entry declares it",
            "It will never be served.",
        )
        for name in sorted(on_disk - declared)
    )
    return findings


def check_page_assets(context: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    routes = context.routes
    total_files = 0
    total_bytes = 0

    for page_dir in pageaudit.page_directories(context.plugin_dir):
        files = pageaudit.page_files(page_dir)
        total_files += len(files)
        total_bytes += sum(path.stat().st_size for path in files)
        label = f"pages/{page_dir.name}"

        entry = page_dir / pageaudit.PAGE_ENTRY_FILE
        if entry.is_file():
            text = entry.read_text(encoding="utf-8", errors="replace")
            if pageaudit.head_tag_hidden_in_comment(text):
                findings.append(
                    Finding(
                        "page.head_in_comment",
                        "the first head tag in the source sits inside a comment",
                        "The host inserts the bridge after the first one it finds by scanning the text, "
                        "comments included, so the page would load with no script running at all.",
                        where=label,
                    )
                )
            if pageaudit.self_injected_bridge(text):
                findings.append(
                    Finding(
                        "page.self_bridge",
                        f"the page loads {pageaudit.BRIDGE_ASSET_NAME} itself",
                        "The host inserts it; loading it again can load the bridge twice.",
                        where=label,
                    )
                )
        else:
            findings.append(Finding("page.no_entry", "no index.html", "The page is not registered.", where=label))

        findings.extend(
            Finding(
                "page.external_asset",
                f"{file_name}:{line} references {url}",
                "The page CSP blocks off-origin resources silently.",
                where=label,
            )
            for file_name, url, line in pageaudit.external_references(page_dir)
        )
        findings.extend(
            Finding("page.missing_asset", f"index.html references {reference}, which is not in the page directory", where=label)
            for reference in pageaudit.missing_local_assets(page_dir)
        )

        for file_name, method, endpoint, line in pageaudit.bridge_calls(page_dir):
            if endpoint not in routes:
                findings.append(
                    Finding(
                        "page.unknown_endpoint",
                        f"{file_name}:{line} calls {endpoint!r}, which this plugin does not publish",
                        f"Registered: {', '.join(sorted(routes)) or 'nothing'}",
                        where=label,
                    )
                )
            elif method not in routes[endpoint]:
                findings.append(
                    Finding(
                        "page.wrong_method",
                        f"{file_name}:{line} calls {endpoint!r} with {method}, but it accepts "
                        f"{', '.join(routes[endpoint])}",
                        where=label,
                    )
                )

    if total_files > pageaudit.MAX_PAGE_FILE_COUNT:
        findings.append(
            Finding("page.too_many_files", f"pages/ holds {total_files} files, above the {pageaudit.MAX_PAGE_FILE_COUNT} advised")
        )
    if total_bytes > pageaudit.MAX_PAGE_TOTAL_BYTES:
        findings.append(
            Finding("page.too_large", f"pages/ is {total_bytes // 1024} KiB, above the advised limit")
        )
    return findings


def check_panels(context: CheckContext) -> list[Finding]:
    """Widgets must point at endpoints that exist and fields that are returned."""
    findings: list[Finding] = []
    routes = context.routes
    bodies: dict[str, Any] = {}

    for panel in context.bundle.plugin.ui.panels:
        for widget in panel.get("widgets", []):
            widget_id = widget.get("id", "?")
            kind = widget.get("type")
            if kind not in WIDGET_TYPES:
                findings.append(
                    Finding(
                        "widget.unknown_type",
                        f"{widget_id} declares type {kind!r}",
                        f"Known types: {', '.join(sorted(WIDGET_TYPES))}",
                    )
                )
            source = widget.get("source") or {}
            # The dashboard POSTs an action widget's endpoint regardless of what
            # `source.method` says; everything else is a read.
            data_method = "POST" if kind == "action" else str(source.get("method") or "GET").upper()
            for key, method in (("endpoint", data_method), ("sse", "GET")):
                endpoint = source.get(key)
                if endpoint and endpoint not in routes:
                    findings.append(
                        Finding(
                            "widget.unknown_endpoint",
                            f"{widget_id} reads {endpoint!r}, which this plugin does not publish",
                            "The widget will render empty.",
                        )
                    )
                elif endpoint and method not in routes[endpoint]:
                    findings.append(
                        Finding(
                            "widget.wrong_method",
                            f"{widget_id} calls {endpoint!r} with {method}, but it accepts {', '.join(routes[endpoint])}",
                        )
                    )
            submit = widget.get("submit")
            if submit and submit not in routes:
                findings.append(
                    Finding("widget.unknown_endpoint", f"{widget_id} submits to {submit!r}, which is not published")
                )
            elif submit and "POST" not in routes[submit]:
                findings.append(Finding("widget.wrong_method", f"{widget_id} submits to {submit!r}, which refuses POST"))

            findings.extend(_check_widget_field(context, widget, source, routes, bodies))
    return findings


def _check_widget_field(
    context: CheckContext,
    widget: dict[str, Any],
    source: dict[str, Any],
    routes: dict[str, tuple[str, ...]],
    bodies: dict[str, Any],
) -> list[Finding]:
    field_name = source.get("field")
    endpoint = source.get("endpoint")
    if not field_name or not endpoint or endpoint not in routes or "GET" not in routes[endpoint]:
        return []
    if endpoint not in bodies:
        try:
            bodies[endpoint] = _as_data(context.call(endpoint))
        except Exception as exc:
            return [Finding("widget.endpoint_failed", f"{endpoint!r} raised while being probed: {exc}")]
    body = bodies[endpoint]
    if isinstance(body, dict) and field_name not in body:
        return [
            Finding(
                "widget.unknown_field",
                f"{widget.get('id', '?')} reads field {field_name!r}, which {endpoint!r} does not return",
                f"It returns: {', '.join(sorted(body)) or 'nothing'}",
            )
        ]
    return []


def check_endpoints_respond(context: CheckContext) -> list[Finding]:
    """Every GET endpoint should answer rather than raise.

    A raised exception becomes a 500 with a message the page cannot act on; a
    plugin that wants to refuse should return `error_response` instead.
    """
    findings: list[Finding] = []
    for endpoint, methods in sorted(context.routes.items()):
        if "GET" not in methods:
            continue
        try:
            context.call(endpoint)
        except Exception as exc:
            findings.append(
                Finding(
                    "endpoint.raised",
                    f"GET {endpoint} raised {type(exc).__name__}: {exc}",
                    "Return error_response(code, status) for an expected failure.",
                )
            )
    return findings


def _as_data(response: Any) -> Any:
    if isinstance(response, dict | list):
        return response
    content = getattr(response, "content", None)
    if content is not None:
        return content
    body = getattr(response, "body", b"")
    try:
        return json.loads(bytes(body))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Everything, in one call
# ---------------------------------------------------------------------------

ALL_CHECKS = (
    ("manifest", check_manifest),
    ("commands", check_commands),
    ("tool access", check_tool_access),
    ("tools", check_tools),
    ("subagents", check_subagents),
    ("hooks", check_hooks),
    ("config schema", check_config_schema),
    ("requirements", check_requirements),
    ("skill", check_skill),
    ("pages", check_pages),
    ("page assets", check_page_assets),
    ("panels", check_panels),
    ("endpoints", check_endpoints_respond),
)


def run_all(context: CheckContext) -> dict[str, list[Finding]]:
    """Run every check, grouped by name so a report can show what passed."""
    return {label: check(context) for label, check in ALL_CHECKS}
