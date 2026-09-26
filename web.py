"""Web endpoints - what the roster page and the declarative panel call.

Each endpoint is published under this plugin's own namespace:

    /api/v1/plugins/extensions/agency-agents-plugin/<endpoint>

You never write that prefix and never mount a FastAPI route. Routes resolve at
request time, which is what lets a hot-reloaded plugin change its endpoints
without a restart. Authentication is the host's: every handler already knows who
is calling through `request.username`.

Failures return a stable snake_case code, never a sentence - the page renders the
text in whichever language its reader is using, and a message written here would
be stuck in one language.

The catalog is read-only vendored data, so most of this file is a read. The one
write path (consulting a persona) goes through the host's tool executor rather
than calling the tool object directly: that way a page click gets the same
authorisation checks, approval gate and tracing as a model-initiated call.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from extension.plugin_web import (
    bytes_response,
    error_response,
    plugin_web_api,
)

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

CONSULT_TOOL = "agency_agents_consult_tool"
CONSULT_TIMEOUT_SEC = 180
DEFAULT_PAGE_SIZE = 20


def _load_local_module(filename: str) -> ModuleType:
    """Import a sibling module by path; see the note in `tools.py`."""
    path = Path(__file__).with_name(filename)
    module_name = f"agency_agents_{path.stem}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable for a shipped file
        msg = f"cannot load {filename}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_catalog_module = _load_local_module("catalog.py")
_store = _load_local_module("store.py")
ROSTER = _catalog_module.Catalog()


# ---------------------------------------------------------------------------
# Decorator style: no closure, so nothing but the request is available.
# ---------------------------------------------------------------------------


@plugin_web_api("ping", methods=("GET",), description="Liveness probe for the roster page")
def ping(request: Any) -> dict[str, Any]:
    """Answer a liveness probe. Synchronous: the host awaits only what is awaitable."""
    return {
        "status": "success",
        "pong": True,
        # Who is calling, established by the host's auth rather than by the page.
        "user": request.username,
        "agents": ROSTER.count(),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Closure style: everything that needs plugin config, storage or the executor.
# ---------------------------------------------------------------------------


def register_web_apis(web: Any, plugin: Any, runtime_context: dict[str, Any]) -> None:  # noqa: PLR0915
    """Publish this plugin's HTTP surface.

    `web` is a facade bound to this plugin: it can only register under this
    plugin's namespace and cannot reach the global route table.
    """
    storage = runtime_context.get("storage")
    # The host's tool executor. Consulting a persona from the page goes through
    # it to get authorisation, the approval gate, retries and tracing - and it
    # costs no model turn of the agent loop.
    executor = runtime_context.get("tool_executor")

    def settings(section: str) -> dict[str, Any]:
        raw = (plugin.config or {}).get(section)
        return raw if isinstance(raw, dict) else {}

    def history() -> Any:
        return _store.ConsultationStore(storage)

    def registered_divisions() -> set[str]:
        declared = settings("roster").get("subagent_divisions")
        if not isinstance(declared, list):
            return set()
        return {str(item).strip().lower() for item in declared if str(item).strip()}

    def specialist(division: str) -> str:
        return f"agency_{division.replace('-', '_')}_specialist"

    # -- panel sources: the shapes the widgets expect -----------------------

    # `stat` widgets read one field out of this body via source.field.
    async def stats(request: Any) -> Any:
        divisions = ROSTER.divisions()
        ledger = history()
        return {
            "agents": ROSTER.count(),
            "divisions": len(divisions),
            "specialists": len(registered_divisions() & {item.key for item in divisions}),
            "consultations": ledger.count(request.username),
            "backend": ledger.backend,
            "version": plugin.version,
            "hint": f"{len(registered_divisions())} division(s) registered as subagents",
        }

    # `key-value` renders every entry of a flat object.
    async def summary(request: Any) -> Any:
        _ = request
        divisions = ROSTER.divisions()
        return {
            "plugin": plugin.name,
            "version": plugin.version,
            "personas": ROSTER.count(),
            "divisions": ", ".join(item.key for item in divisions) or "none",
            "specialist_subagents": ", ".join(sorted(specialist(key) for key in registered_divisions())) or "none",
            "catalog": "available" if ROSTER.available else "missing",
            "storage_backend": history().backend,
            "tool_executor": "available" if executor is not None else "absent",
            "upstream": "github.com/msitarzewski/agency-agents (MIT)",
        }

    # `table` wants an array of objects whose keys match the declared columns.
    async def divisions(request: Any) -> Any:
        _ = request
        enabled = registered_divisions()
        return [
            {
                "division": item.label,
                "key": item.key,
                "agents": item.agents,
                "subagent": specialist(item.key) if item.key in enabled else "-",
                "color": item.color,
            }
            for item in ROSTER.divisions()
        ]

    # Both chart widgets read the same array: one x_field plus one series.
    async def distribution(request: Any) -> Any:
        _ = request
        return [{"division": item.key, "agents": item.agents} for item in ROSTER.divisions()]

    # -- the roster ---------------------------------------------------------

    async def agents(request: Any) -> Any:
        """Search or browse. The page's main list."""
        if not ROSTER.available:
            return error_response("catalog_unavailable", 503)
        query = str(request.query.get("q") or request.query.get("query") or "").strip()
        division = str(request.query.get("division") or "").strip().lower()
        if division and ROSTER.division(division) is None:
            return error_response("unknown_division", 404)
        limit = int(request.query.get("limit") or settings("roster").get("max_results") or DEFAULT_PAGE_SIZE)
        return [
            {**agent.summary(), "score": score}
            for agent, score in ROSTER.search(query, division=division, limit=limit)
        ]

    async def agent(request: Any) -> Any:
        """One persona in full, including its prompt body."""
        if not ROSTER.available:
            return error_response("catalog_unavailable", 503)
        slug = str(request.query.get("slug") or "").strip()
        if not slug:
            return error_response("missing_slug", 400)
        limit = int(
            request.query.get("max_chars")
            or settings("roster").get("brief_max_chars")
            or _catalog_module.DEFAULT_BRIEF_CHARS
        )
        brief = ROSTER.brief(slug, limit)
        if brief is None:
            return error_response("agent_not_found", 404)
        enabled = registered_divisions()
        return {
            "status": "success",
            "agent": brief,
            "subagent": specialist(brief["division"]) if brief["division"] in enabled else "",
        }

    async def export_agent(request: Any) -> Any:
        """Serve one persona file, so a reader can take the prompt with them.

        The sandbox blocks a download the page starts itself, so the host has to
        be the one to save it - the page calls `bridge.download`.
        """
        if not ROSTER.available:
            return error_response("catalog_unavailable", 503)
        slug = str(request.query.get("slug") or "").strip()
        if not slug:
            return error_response("missing_slug", 400)
        found = ROSTER.get(slug)
        if found is None:
            return error_response("agent_not_found", 404)
        # Resolved through the catalog's own index rather than by joining the
        # caller's string onto a path: `slug` arrives from a web page.
        path = ROSTER.root / found.relative_path
        try:
            payload = path.read_bytes()
        except OSError:
            return error_response("agent_file_missing", 410)
        return bytes_response(payload, "text/markdown")

    # -- consultation history -----------------------------------------------

    async def recent(request: Any) -> Any:
        if storage is None:
            return error_response("storage_unavailable", 503)
        rows = history().recent(request.username, int(request.query.get("limit") or 10))
        return [
            {
                "id": row.get("id", ""),
                "agent": f"{row.get('emoji', '')} {row.get('agent', '')}".strip(),
                "division": row.get("division", ""),
                "question": str(row.get("question", ""))[:160],
                "answer": row.get("answer", ""),
                "created": row.get("created", ""),
            }
            for row in rows
        ]

    async def favourites(request: Any) -> Any:
        """Who this user keeps coming back to. Feeds the second table widget."""
        if storage is None:
            return error_response("storage_unavailable", 503)
        return history().agent_tally(request.username)[: int(request.query.get("limit") or 10)]

    async def delete_record(request: Any) -> Any:
        if storage is None:
            return error_response("storage_unavailable", 503)
        record_id = str(request.query.get("id") or "").strip()
        if not record_id:
            return error_response("missing_id", 400)
        # Scoped to the caller, so an id guessed from someone else's history
        # finds nothing rather than deleting their record.
        if not history().delete(request.username, record_id):
            return error_response("record_not_found", 404)
        return {"status": "success", "id": record_id}

    # -- consulting a persona, through the host's executor -------------------

    async def consult(request: Any) -> Any:
        """Ask one persona a question. Backs both the page and the form widget."""
        if executor is None:
            return error_response("executor_unavailable", 503)
        body = await request.json({})
        question = str(body.get("question") or body.get("text") or "").strip()
        if not question:
            return error_response("missing_question", 400)
        payload = {
            "actor_id": request.username,
            "agent": str(body.get("agent") or body.get("slug") or "").strip(),
            "division": str(body.get("division") or "").strip().lower(),
            "question": question,
            # A page must be able to resolve a persona without spending provider
            # budget; the reader asks for the real call explicitly.
            "dry_run": bool(body.get("dry_run")),
        }
        try:
            result = await asyncio.wait_for(
                executor.execute(
                    CONSULT_TOOL,
                    payload,
                    trace_id=f"plugin-page:{plugin.name}:{request.username}",
                ),
                timeout=CONSULT_TIMEOUT_SEC,
            )
        except TimeoutError:
            return error_response("consult_timeout", 504)
        except RuntimeError as exc:
            logger.warning("agency consult from page failed: %s", exc)
            return error_response("consult_failed", 502)
        if isinstance(result, dict) and result.get("status") == "error":
            # The tool's own stable code, passed through so the page can render
            # it in the reader's language.
            return error_response(str(result.get("error_code") or "consult_failed"), 422)
        return {"status": "success", "result": result if isinstance(result, dict) else {"value": str(result)}}

    # -- action widget ------------------------------------------------------

    async def refresh(request: Any) -> Any:
        """Re-read the catalog from disk. The host POSTs with no body.

        Needed after someone replaces `catalog/` on a running instance - the
        index is cached for the life of the process precisely because the files
        do not normally change.
        """
        _ = request
        ROSTER.invalidate()
        return {
            "status": "success",
            "agents": ROSTER.count(),
            "divisions": len(ROSTER.divisions()),
        }

    async def export_division(request: Any) -> Any:
        """Serve a whole division as one JSON file, for taking the roster elsewhere."""
        if not ROSTER.available:
            return error_response("catalog_unavailable", 503)
        division = str(request.query.get("division") or "").strip().lower()
        if division and ROSTER.division(division) is None:
            return error_response("unknown_division", 404)
        pool = ROSTER.in_division(division) if division else list(ROSTER.agents.values())
        payload = json.dumps(
            {
                "upstream": "https://github.com/msitarzewski/agency-agents",
                "licence": "MIT",
                "division": division or "all",
                "agents": [item.summary() for item in pool],
            },
            ensure_ascii=False,
            indent=2,
        )
        return bytes_response(payload.encode("utf-8"), "application/json")

    # -- publication --------------------------------------------------------
    # Endpoints are relative. Methods default to ("GET",) when omitted.

    web.register_web_api("stats", stats, ("GET",), "Panel stat tiles")
    web.register_web_api("summary", summary, ("GET",), "Runtime key-value panel")
    web.register_web_api("divisions", divisions, ("GET",), "Divisions table")
    web.register_web_api("distribution", distribution, ("GET",), "Personas per division, for the chart")
    web.register_web_api("agents", agents, ("GET",), "Search or browse the roster")
    web.register_web_api("agent", agent, ("GET",), "One persona, prompt included")
    web.register_web_api("export", export_agent, ("GET",), "Download one persona as markdown")
    web.register_web_api("export/division", export_division, ("GET",), "Download a division as JSON")
    web.register_web_api("recent", recent, ("GET",), "This user's recent consultations")
    web.register_web_api("favourites", favourites, ("GET",), "Most consulted personas")
    web.register_web_api("history/delete", delete_record, ("DELETE",), "Delete one consultation record")
    web.register_web_api("consult", consult, ("POST",), "Ask one persona a question")
    web.register_web_api("actions/refresh", refresh, ("POST",), "Re-read the catalog from disk")
