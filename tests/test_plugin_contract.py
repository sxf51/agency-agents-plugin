"""Checks every plugin should pass. Copy this file unchanged.

Nothing here names a tool, command, endpoint or page. Each test reads what the
plugin declares - in `plugin.yaml`, `_conf_schema.json`, `SKILL.md`, `pages/`
and its registrations - and confirms those declarations agree with each other.
Rewrite what the plugin does and these keep working.

They exist because the plugin API fails quietly. A command whose `tool` is
misspelled is dropped without a word; a widget pointing at an endpoint that was
never registered renders as an empty box; a page calling `apiGet('stat')` when
the endpoint is `stats` fails only in a browser, in production.

The checks themselves live in `harness/checks.py`, so `main.py doctor` reports
exactly what these assert.
"""

from __future__ import annotations

from typing import Any

import pytest
from harness import checks


def _assert_clean(findings: list[checks.Finding]) -> None:
    if findings:
        pytest.fail("\n" + "\n".join(f"  {finding.render()}" for finding in findings), pytrace=False)


# ---------------------------------------------------------------------------
# It loads at all
# ---------------------------------------------------------------------------


def test_plugin_loads_without_error(bundle: Any) -> None:
    assert bundle.plugin.status == "active"
    assert bundle.plugin.error is None


def test_plugin_registers_something(bundle: Any) -> None:
    """A plugin that registers nothing is almost always a loading failure."""
    registered = (
        len(bundle.tools.tools)
        + len(bundle.subagents.list_agents())
        + len(bundle.hooks.list_hooks())
        + len(bundle.web.list_for(bundle.plugin.name))
        + len(bundle.plugin.ui.pages)
    )
    assert registered > 0, "no tools, subagents, hooks, endpoints or pages were registered"


# ---------------------------------------------------------------------------
# The manifest agrees with the code
# ---------------------------------------------------------------------------


def test_manifest_is_consistent(check_context: Any) -> None:
    _assert_clean(checks.check_manifest(check_context))


def test_every_declared_command_survives_collection(check_context: Any) -> None:
    """The runtime drops a malformed command silently; this is the only warning."""
    _assert_clean(checks.check_commands(check_context))


def test_tool_access_names_real_tools(check_context: Any) -> None:
    _assert_clean(checks.check_tool_access(check_context))


# ---------------------------------------------------------------------------
# Registrations are complete enough to be usable
# ---------------------------------------------------------------------------


def test_tools_are_fully_declared(check_context: Any) -> None:
    """Description, input schema, side-effect flag and a plugin-specific name."""
    _assert_clean(checks.check_tools(check_context))


def test_subagents_are_routable(check_context: Any) -> None:
    _assert_clean(checks.check_subagents(check_context))


def test_hooks_are_registered_correctly(check_context: Any) -> None:
    _assert_clean(checks.check_hooks(check_context))


# ---------------------------------------------------------------------------
# Configuration and metadata
# ---------------------------------------------------------------------------


def test_config_schema_is_valid(check_context: Any) -> None:
    _assert_clean(checks.check_config_schema(check_context))


def test_requirements_are_accepted_by_the_loader(check_context: Any) -> None:
    _assert_clean(checks.check_requirements(check_context))


def test_skill_markdown_declares_its_frontmatter(check_context: Any) -> None:
    _assert_clean(checks.check_skill(check_context))


# ---------------------------------------------------------------------------
# The dashboard surface
# ---------------------------------------------------------------------------


def test_declared_pages_exist(check_context: Any) -> None:
    _assert_clean(checks.check_pages(check_context))


def test_page_assets_will_survive_the_sandbox(check_context: Any) -> None:
    """External assets, a self-loaded bridge, a head tag hidden in a comment.

    Also cross-checks every endpoint the page's own JavaScript calls against the
    endpoints this plugin registered.
    """
    _assert_clean(checks.check_page_assets(check_context))


def test_panel_widgets_point_at_real_endpoints_and_fields(check_context: Any) -> None:
    _assert_clean(checks.check_panels(check_context))


def test_endpoints_answer_instead_of_raising(check_context: Any) -> None:
    """An expected failure is `error_response`, not an exception."""
    _assert_clean(checks.check_endpoints_respond(check_context))


# ---------------------------------------------------------------------------
# The whole set, so `doctor` and the tests can never disagree
# ---------------------------------------------------------------------------


def test_doctor_reports_nothing(check_context: Any) -> None:
    """Every check at once - what `main.py doctor` prints."""
    grouped = checks.run_all(check_context)
    _assert_clean([finding for findings in grouped.values() for finding in findings])
