"""What this plugin actually registered, as a table.

Printed after every test run and by `main.py inspect`. A plugin declares its
capabilities in several places - the manifest, decorators, explicit register_*
calls - and nothing otherwise shows them in one list. Most of the time the
useful information is what is *missing* from this table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import checks, pageaudit

if TYPE_CHECKING:
    from pathlib import Path


def inventory(plugin_dir: Path, host: Any, bundle: Any) -> dict[str, Any]:
    """Collect everything the plugin registered, without judging it."""
    return {
        "plugin": bundle.plugin.name,
        "version": bundle.plugin.version,
        "description": bundle.plugin.description,
        "backend": "the project's own PluginManager" if host.is_real else "stand-in (harness/)",
        "tools": _tools(bundle),
        "subagents": _subagents(bundle),
        "hooks": _hooks(bundle),
        "skills": _skills(plugin_dir),
        "commands": _commands(host, bundle),
        "endpoints": _endpoints(bundle),
        "pages": _pages(plugin_dir, bundle),
        "config": sorted(bundle.plugin.config_schema),
    }


def _tools(bundle: Any) -> list[dict[str, Any]]:
    rows = []
    for name, spec in sorted(bundle.tools.tools.items()):
        rows.append(
            {
                "name": name,
                "effect": "writes" if spec.consequential else "read-only",
                "schema": "yes" if spec.metadata.get("input_schema") else "MISSING",
                "description": str(spec.metadata.get("description") or "").strip(),
                "allowed_subagents": tuple(spec.metadata.get("allowed_subagents") or ()),
            }
        )
    return rows


def _subagents(bundle: Any) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(bundle.subagents.list_agents()):
        profile = bundle.subagents.get_profile(name) or {}
        metadata = profile.get("metadata") or {}
        rows.append(
            {
                "name": name,
                "domain": profile.get("domain") or "",
                "capabilities": tuple(profile.get("capabilities") or ()),
                "allowed_tools": tuple(metadata.get("allowed_tools") or ()),
            }
        )
    return rows


def _hooks(bundle: Any) -> list[dict[str, Any]]:
    rows = [
        {"point": str(registration.point), "priority": registration.priority}
        for registration in bundle.hooks.list_hooks()
    ]
    return sorted(rows, key=lambda row: (row["priority"], row["point"]))


def _skills(plugin_dir: Path) -> list[dict[str, str]]:
    path = next(
        (plugin_dir / name for name in ("SKILL.md", "skill.md") if (plugin_dir / name).is_file()),
        None,
    )
    if path is None:
        return []
    frontmatter = checks.read_frontmatter(path.read_text(encoding="utf-8")) or {}
    return [
        {
            "name": str(frontmatter.get("name") or path.stem),
            "description": str(frontmatter.get("description") or ""),
        }
    ]


def _commands(host: Any, bundle: Any) -> list[dict[str, str]]:
    rows = []
    for name, spec in sorted(host.collect_commands(bundle).items()):
        route = str(spec.get("route", "task"))
        if route == "tool":
            target = f"tool: {spec.get('tool', '')}"
        else:
            hints = spec.get("planner_hints") or {}
            preferred = hints.get("preferred_subagent")
            target = f"task -> {preferred}" if preferred else "task"
        rows.append({"command": name, "target": target, "usage": str(spec.get("usage") or "")})
    return rows


def _endpoints(bundle: Any) -> list[dict[str, str]]:
    rows = [
        {
            "endpoint": route.endpoint,
            "methods": " ".join(route.methods),
            "description": route.description,
        }
        for route in bundle.web.list_for(bundle.plugin.name)
    ]
    return sorted(rows, key=lambda row: row["endpoint"])


def _pages(plugin_dir: Path, bundle: Any) -> list[dict[str, Any]]:
    rows = []
    for page in bundle.plugin.ui.pages:
        directory = plugin_dir / "pages" / page.name
        files = pageaudit.page_files(directory) if directory.is_dir() else []
        rows.append({"name": page.name, "nav": page.nav, "files": len(files)})
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(data: dict[str, Any]) -> str:
    """One block per capability, aligned so gaps are obvious at a glance."""
    lines = [
        f"{data['plugin']} {data['version']}   via {data['backend']}",
        data["description"],
        "",
    ]
    lines += _block("tools", [f"{row['name']:<28} {row['effect']:<10} schema: {row['schema']}" for row in data["tools"]])
    lines += _block(
        "subagents",
        [
            f"{row['name']:<28} domain={row['domain'] or '-':<12} tools={len(row['allowed_tools']) or 'all'}"
            for row in data["subagents"]
        ],
    )
    lines += _block("hooks", [f"{row['point']} (priority {row['priority']})" for row in data["hooks"]], inline=True)
    lines += _block("skills", [f"{row['name']} - {row['description'][:70]}" for row in data["skills"]])
    lines += _block("commands", [f"{row['command']:<22} {row['target']}" for row in data["commands"]])
    lines += _block("endpoints", [f"{row['methods']:<16} {row['endpoint']}" for row in data["endpoints"]])
    lines += _block("pages", [f"{row['name']:<20} nav={row['nav']:<10} {row['files']} files" for row in data["pages"]])
    lines += _block("config", data["config"], inline=True)
    return "\n".join(lines).rstrip()


def _block(label: str, rows: list[str], *, inline: bool = False) -> list[str]:
    if not rows:
        return [f"{label:<12}0   -", ""]
    if inline:
        return [f"{label:<12}{len(rows):<4}{', '.join(rows)}", ""]
    head = f"{label:<12}{len(rows):<4}{rows[0]}"
    return [head, *[f"{'':<16}{row}" for row in rows[1:]], ""]


def render_findings(grouped: dict[str, list[checks.Finding]]) -> str:
    """The doctor's output: what passed, and what to do about what did not."""
    total = sum(len(items) for items in grouped.values())
    lines: list[str] = []
    for label, findings in grouped.items():
        mark = "ok  " if not findings else "FAIL"
        lines.append(f"  {mark}  {label}")
        lines.extend(f"          {finding.render()}" for finding in findings)
    lines.append("")
    lines.append("No problems found." if total == 0 else f"{total} problem(s) found.")
    return "\n".join(lines)
