"""Rename a copied plugin in one pass.

Step two of copying a plugin is renaming everything the old one was called: the
manifest name, every tool, every subagent, every slash command. Missing one is
quiet - a command still pointing at the old tool name is dropped by the runtime
without a word.

The names come from what the plugin actually registered, not from guessing at a
prefix. That distinction matters: a blind `template_` -> `notes_` rewrite also
hits `template_list`, a config field type belonging to the framework, and
quietly breaks the config schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# Files worth rewriting. Anything binary, generated or vendored is skipped.
TEXT_SUFFIXES = frozenset({".py", ".yaml", ".yml", ".json", ".md", ".html", ".css", ".js", ".txt", ".toml"})
SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})
# The harness is plugin-agnostic, so renaming inside it would be wrong.
SKIP_RELATIVE = ("tests/harness",)


def _stem(plugin_name: str) -> str:
    """Drop the `plugin-` / `-plugin` decoration: `plugin-template` -> `template`."""
    return plugin_name.removeprefix("plugin-").removesuffix("-plugin") or plugin_name


def identifier_of(plugin_name: str) -> str:
    """The Python-identifier form, which prefixes tool and subagent names."""
    return _stem(plugin_name).replace("-", "_")


def slug_of(plugin_name: str) -> str:
    """The hyphenated form, which prefixes slash commands."""
    return _stem(plugin_name).replace("_", "-")


@dataclass
class RenamePlan:
    """Exactly which strings change, and to what."""

    old_plugin: str
    new_plugin: str
    renames: dict[str, str] = field(default_factory=dict)

    @property
    def old_identifier(self) -> str:
        return identifier_of(self.old_plugin)

    @property
    def new_identifier(self) -> str:
        return identifier_of(self.new_plugin)

    @property
    def old_slug(self) -> str:
        return slug_of(self.old_plugin)

    @property
    def new_slug(self) -> str:
        return slug_of(self.new_plugin)

    def substitutions(self) -> list[tuple[re.Pattern[str], str]]:
        """Longest first, so a shorter rule never half-rewrites a longer name."""
        ordered = sorted(self.renames.items(), key=lambda pair: len(pair[0]), reverse=True)
        rules = [(re.compile(rf"(?<![\w-]){re.escape(old)}(?![\w-])"), new) for old, new in ordered]
        return rules + self._prefix_substitutions()

    def _prefix_substitutions(self) -> list[tuple[re.Pattern[str], str]]:
        """Bare prefixes written as whole string literals.

        Plugin code tests membership with `name.startswith("template_")` and
        `text.startswith("/template-")`. Those are prefixes, not names, so the
        exact-name rules above miss them. Matching only a complete quoted
        literal keeps this from touching `template_list`, which is a config
        field type belonging to the framework rather than to any plugin.
        """
        return [
            (re.compile(rf"""(?<=["']){re.escape(self.old_identifier)}_(?=["'])"""), f"{self.new_identifier}_"),
            (re.compile(rf"""(?<=["'])/{re.escape(self.old_slug)}-(?=["'])"""), f"/{self.new_slug}-"),
        ]


def plan(plugin_dir: Path, new_plugin: str, bundle: Any = None) -> RenamePlan:
    """Work out every rename from what the plugin registered.

    `bundle` is a loaded plugin. Without one only the plugin name is renamed.
    """
    manifest_path = next(
        (plugin_dir / name for name in ("plugin.yaml", "plugin.yml") if (plugin_dir / name).is_file()),
        None,
    )
    if manifest_path is None:
        msg = f"{plugin_dir} has no plugin.yaml"
        raise FileNotFoundError(msg)
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    old_plugin = str(raw.get("name") or plugin_dir.name)

    result = RenamePlan(old_plugin=old_plugin, new_plugin=new_plugin)
    result.renames[old_plugin] = new_plugin
    if bundle is None:
        return result

    old_id, new_id = result.old_identifier, result.new_identifier
    old_slug, new_slug = result.old_slug, result.new_slug

    # Only names this plugin actually registered are rewritten.
    for name in list(bundle.tools.tools) + list(bundle.subagents.list_agents()):
        if name.startswith(f"{old_id}_"):
            result.renames[name] = f"{new_id}_{name[len(old_id) + 1 :]}"

    commands = (bundle.plugin.config or {}).get("commands")
    for entry in commands if isinstance(commands, list) else []:
        command = str(entry.get("command", "")) if isinstance(entry, dict) else ""
        if command.startswith(f"/{old_slug}-"):
            result.renames[command] = f"/{new_slug}-{command[len(old_slug) + 2 :]}"

    return result


def apply(plugin_dir: Path, rename: RenamePlan, *, dry_run: bool = False) -> list[tuple[str, int]]:
    """Rewrite every text file. Returns (relative path, replacements) per file."""
    substitutions = rename.substitutions()
    touched: list[tuple[str, int]] = []

    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative_path = path.relative_to(plugin_dir)
        relative = relative_path.as_posix()
        if any(part in SKIP_DIRS for part in relative_path.parts):
            continue
        if relative.startswith(SKIP_RELATIVE):
            continue

        original = path.read_text(encoding="utf-8")
        updated = original
        count = 0
        for pattern, replacement in substitutions:
            updated, hits = pattern.subn(replacement, updated)
            count += hits
        if not count:
            continue
        touched.append((relative, count))
        if not dry_run:
            path.write_text(updated, encoding="utf-8")

    return touched


def rename_test_file(plugin_dir: Path, rename: RenamePlan, *, dry_run: bool = False) -> str | None:
    """The plugin-specific test file is named after the plugin; move it too."""
    candidates = (
        plugin_dir / "tests" / f"test_plugin_{rename.old_identifier}.py",
        plugin_dir / "tests" / f"test_{rename.old_identifier}.py",
    )
    old = next((path for path in candidates if path.is_file()), None)
    if old is None:
        return None
    new = old.with_name(f"test_plugin_{rename.new_identifier}.py")
    if new == old:
        return None
    if not dry_run:
        old.rename(new)
    return new.name
