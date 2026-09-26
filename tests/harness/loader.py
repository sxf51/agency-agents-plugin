"""Manifest, schema and command rules, re-derived from the documented contract.

The host implements these in `src/extension/plugin_governance.py` and
`src/runtime/command_catalog.py`. Standalone there is no host, so the rules are
written out here. They are the rules a plugin author has to satisfy, so a
second implementation is the point: if this one and the host's ever disagree,
the in-repo run of these same tests against the real host catches it.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

SUPPORTED_CONFIG_TYPES = {
    "string",
    "text",
    "int",
    "float",
    "bool",
    "object",
    "list",
    "dict",
    "template_list",
    "file",
}

EMPTY_FOR_TYPE: dict[str, Any] = {
    "string": "",
    "text": "",
    "int": 0,
    "float": 0.0,
    "bool": False,
    "object": {},
    "dict": {},
    "list": [],
    "template_list": [],
    "file": [],
}

# Core commands a plugin may not claim. Mirrors CORE_HELP_COMMANDS.
RESERVED_COMMANDS = {
    "/id",
    "/help",
    "/status",
    "/task",
    "/plugins",
    "/memory",
    "/context",
    "/context-full",
    "/clear",
    "/config",
    "/quit",
    "/exit",
}

VALID_NAV = ("sidebar", "plugin", "hidden")


def read_manifest(plugin_dir: Any) -> dict[str, Any]:
    for name in ("plugin.yaml", "plugin.yml"):
        path = plugin_dir / name
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    return {}


def validate_config_schema(schema: Any, *, location: str = "schema") -> dict[str, dict[str, Any]]:
    """Check every field declares a supported type; recurse into objects."""
    if not isinstance(schema, dict):
        raise ValueError(f"{location} must be an object")
    result: dict[str, dict[str, Any]] = {}
    for key, raw in schema.items():
        if not isinstance(key, str) or not key or not isinstance(raw, dict):
            raise ValueError(f"{location} entries must be named objects")
        spec = dict(raw)
        if spec.get("type") not in SUPPORTED_CONFIG_TYPES:
            raise ValueError(f"{location}.{key} has unsupported type {spec.get('type')!r}")
        if spec["type"] == "object":
            spec["items"] = validate_config_schema(spec.get("items", {}), location=f"{location}.{key}.items")
        result[key] = spec
    return result


def merge_config(schema: dict[str, dict[str, Any]], existing: Any) -> dict[str, Any]:
    """Fill in defaults and drop keys the schema no longer declares."""
    source = existing if isinstance(existing, dict) else {}
    merged: dict[str, Any] = {}
    for key, spec in schema.items():
        value = source.get(key, spec.get("default", EMPTY_FOR_TYPE.get(spec["type"])))
        if spec["type"] == "object":
            value = merge_config(spec.get("items", {}), value)
        merged[key] = value
    return merged


def parse_ui(raw: Any) -> dict[str, Any]:
    """Read the `ui:` block, dropping malformed entries rather than raising."""
    spec: dict[str, Any] = {"pages": [], "panels": []}
    if not isinstance(raw, dict):
        return spec
    for entry in raw.get("pages") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or not all(char.isalnum() or char in "-_" for char in name):
            continue
        nav = str(entry.get("nav") or "plugin")
        spec["pages"].append(
            {
                "name": name,
                "title": entry.get("title") or name,
                "description": entry.get("description") or "",
                "nav": nav if nav in VALID_NAV else "plugin",
                "roles": [str(r).strip() for r in (entry.get("roles") or ()) if str(r).strip()],
            }
        )
    for panel in raw.get("panels") or ():
        if isinstance(panel, dict) and panel.get("id"):
            widgets = [w for w in (panel.get("widgets") or ()) if isinstance(w, dict) and w.get("id")]
            spec["panels"].append({**panel, "widgets": widgets})
    return spec


def collect_commands(config: Any, available_tools: set[str]) -> dict[str, dict[str, Any]]:
    """Keep only the commands the runtime would accept.

    Every rejection below is silent in the real runtime, which is why a plugin's
    tests should assert that the commands it declares survive this.
    """
    collected: dict[str, dict[str, Any]] = {}
    raw_commands = config.get("commands") if isinstance(config, dict) else None
    if not isinstance(raw_commands, list):
        return collected

    for raw in raw_commands:
        if not isinstance(raw, dict):
            continue
        command = str(raw.get("command", "")).strip().lower()
        if not command.startswith("/") or command in RESERVED_COMMANDS or command in collected:
            continue
        route = str(raw.get("route", "task")).strip().lower()
        if route not in {"task", "tool"}:
            continue
        try:
            int(raw.get("priority", 100))
        except (TypeError, ValueError):
            continue
        tool_name = str(raw.get("tool", "")).strip()
        if route == "tool" and (not tool_name or tool_name not in available_tools):
            continue
        required = str(raw.get("requires_tool", "")).strip()
        if required and required not in available_tools:
            continue
        collected[command] = dict(raw)
    return collected


def parse_requirements(path: Any) -> tuple[str, ...]:
    """Parse requirements.txt, raising on the forms the loader rejects."""
    if not path.is_file():
        return ()
    parsed: list[str] = []
    for number, source in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "http://", "https://", "git+", ".", "/")):
            msg = f"Unsupported plugin requirement at {path}:{number}: {line!r}"
            raise ValueError(msg)
        parsed.append(line)
    return tuple(parsed)


def load_plugin_descriptor(plugin_dir: Any) -> Any:
    """Build the `plugin` object a plugin's code is handed.

    Config merge order matches the host: manifest defaults first, then the
    values a user would have saved from the schema.
    """
    manifest = read_manifest(plugin_dir)
    raw_config = manifest.get("config")
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}

    schema: dict[str, Any] = {}
    schema_path = plugin_dir / "_conf_schema.json"
    if schema_path.is_file():
        schema = validate_config_schema(json.loads(schema_path.read_text(encoding="utf-8-sig")))
        config = {**config, **merge_config(schema, {})}

    return PluginDescriptor(
        name=str(manifest.get("name") or plugin_dir.name),
        version=str(manifest.get("version") or "1.0.0"),
        description=str(manifest.get("description") or ""),
        enabled=bool(manifest.get("enabled", True)),
        metadata={str(k): str(v) for k, v in (manifest.get("metadata") or {}).items()},
        config=config,
        config_schema=schema,
        ui=parse_ui(manifest.get("ui")),
        manifest=manifest,
        path=plugin_dir,
    )


class PluginDescriptor:
    """The same fields a plugin reads off the host's `Plugin` dataclass."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        description: str,
        enabled: bool,
        metadata: dict[str, str],
        config: dict[str, Any],
        config_schema: dict[str, Any],
        ui: dict[str, Any],
        manifest: dict[str, Any],
        path: Any,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.enabled = enabled
        self.metadata = metadata
        self.config = config
        self.config_schema = config_schema
        self.ui = _UiSpec(ui)
        self.manifest = manifest
        self.status = "active"
        self.error: str | None = None
        self.metadata.setdefault("path", str(path))


class _UiSpec:
    """Attribute access over the parsed `ui:` block, like the host's dataclass."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.pages = [_Page(entry) for entry in raw.get("pages", [])]
        self.panels = raw.get("panels", [])


class _Page:
    def __init__(self, entry: dict[str, Any]) -> None:
        self.name = entry["name"]
        self.title = entry["title"]
        self.description = entry["description"]
        self.nav = entry["nav"]
        self.roles = tuple(entry["roles"])
