"""Fixtures, and the one line that decides which host backend runs.

`select_host()` in `harness/host.py` walks up for the project's source and
actually imports it before committing. Source on disk is not enough: running
from the plugin's own environment finds the project above but has none of its
dependencies installed.

    uv run pytest plugins/agency-agents-plugin/tests   # project env -> real host
    cd plugins/agency-agents-plugin && uv run pytest   # plugin env  -> stand-in
    cd <copied-out plugin> && uv run pytest       # no source   -> stand-in

Every run says which backend it used, so a fallback is never silent, and prints
the plugin's capability inventory at the end.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# `harness` lives next to this file. pytest normally puts this directory on the
# path itself; saying so explicitly makes the import work however the run was
# started, including from `main.py`. The plugin root deliberately stays off the
# path - `tools.py` there would shadow the project's own `tools` package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    checks,
    report,
    select_host,
)

_HOST = select_host(PLUGIN_DIR)


def pytest_report_header() -> str:
    """Say which half of the plugin API these results actually exercised."""
    if _HOST.is_real:
        return "plugin host: the project's own PluginManager"
    return "plugin host: stand-in (harness/) - host-only checks will skip"


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print what the plugin registered, so gaps are visible without reading code."""
    # Loading the plugin needs a storage root; a temporary one keeps the run
    # from leaving anything behind next to the tests.
    storage_root = Path(tempfile.mkdtemp(prefix="plugin-inventory-"))
    try:
        bundle = _HOST.load(PLUGIN_DIR, storage_root)
        rendered = report.render(report.inventory(PLUGIN_DIR, _HOST, bundle))
    except Exception as exc:
        # A broken inventory must never hide the test results.
        rendered = f"could not build the capability inventory: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(storage_root, ignore_errors=True)
    terminalreporter.write_sep("=", "plugin capabilities")
    terminalreporter.write_line(rendered)


@pytest.fixture(scope="session")
def host() -> Any:
    """The backend in use. `host.is_real` says which."""
    return _HOST


@pytest.fixture
def plugin_dir() -> Path:
    return PLUGIN_DIR


@pytest.fixture
def bundle(host: Any, tmp_path: Path) -> Any:
    """This plugin, loaded and registered, on a fresh storage root."""
    return host.load(PLUGIN_DIR, tmp_path)


@pytest.fixture
def check_context(host: Any, bundle: Any) -> Any:
    """What the generic checks need: the plugin on disk and as loaded."""
    return checks.CheckContext(plugin_dir=PLUGIN_DIR, host=host, bundle=bundle)
