"""Tools - what a model can do with 279 vendored personas.

| Tool | Reads | Writes | Costs a model call |
| --- | --- | --- | --- |
| `agency_agents_roster_tool` | the index | - | no |
| `agency_agents_brief_tool` | one persona file | - | no |
| `agency_agents_panel_tool` | the index | - | no |
| `agency_agents_consult_tool` | one persona file | history | yes |

Three of the four are offline and deterministic: the roster is data on disk, so
finding the right specialist, reading its prompt, or assembling a panel needs no
provider. Only `consult` leaves the process, and it is the only one flagged
`consequential`.

No tool declares `allowed_subagents`, and the manifest declares no
`tool_access`. That is deliberate: which specialists exist is a configuration
decision (`roster.subagent_divisions`), so a static allow-list here would go
stale the first time someone enables a division and would then block the new
specialist silently. Narrowing happens on the subagent side instead, where each
agent declares the tools it uses - see `subagents.py`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib import error, request
from urllib.parse import urlparse

from extension.plugin import plugin_tool

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

# Headroom between the LLM request timeout and the executor's outer one, so a
# slow provider times out as a clean `provider_error` instead of a cancelled tool.
TIMEOUT_GRACE_SEC = 5.0
DEFAULT_TIMEOUT_SEC = 60.0
# A vote node wants 2-7 voters, a dialogue node exactly 2, consensus at most 7.
# The panel tool clamps to those so its suggestion actually compiles.
PANEL_LIMITS = {"vote": (2, 7), "dialogue": (2, 2), "consensus": (1, 7)}
DEFAULT_PANEL_MODE = "dialogue"
# How the parts of a system prompt are joined, and the floor the persona keeps
# however small `persona_max_chars` is set: a heading with no persona behind it
# is not the specialist anyone asked for.
_JOIN = "\n\n"
MIN_PERSONA_CHARS = 500


# ---------------------------------------------------------------------------
# Loading the modules the host does not import for you
# ---------------------------------------------------------------------------


def _load_local_module(filename: str) -> ModuleType:
    """Import a sibling module by path.

    The host imports only `hooks.py`, `tools.py`, `subagents.py` and `web.py`,
    under a synthetic module name and without putting the plugin directory on
    `sys.path`, so a plain `import catalog` fails. The module is registered in
    `sys.modules` before it executes because `dataclasses` resolves a class's
    annotations through `sys.modules[cls.__module__]` - leave it out and every
    dataclass in the loaded module raises at definition time.
    """
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

# One index per process. Building it reads 279 frontmatter blocks, and the files
# do not change while a plugin version is loaded; the refresh endpoint calls
# `invalidate()` when someone replaces them on disk.
ROSTER = _catalog_module.Catalog()


# ---------------------------------------------------------------------------
# Small helpers shared by the tools below
# ---------------------------------------------------------------------------


def _config(plugin: Any | None) -> dict[str, Any]:
    """`plugin.config` merges plugin.yaml's `config:` with the saved schema values."""
    config = getattr(plugin, "config", None)
    return config if isinstance(config, dict) else {}


def _section(plugin: Any | None, key: str) -> dict[str, Any]:
    section = _config(plugin).get(key)
    return section if isinstance(section, dict) else {}


def roster_settings(plugin: Any | None) -> dict[str, Any]:
    return _section(plugin, "roster")


def consult_settings(plugin: Any | None) -> dict[str, Any]:
    return _section(plugin, "consult")


def subagent_divisions(plugin: Any | None) -> list[str]:
    """Which divisions the operator asked to be registered as specialists."""
    declared = roster_settings(plugin).get("subagent_divisions")
    if not isinstance(declared, list):
        return []
    return [str(item).strip().lower() for item in declared if str(item).strip()]


def specialist_name(division: str) -> str:
    """Return the subagent name a division is registered under; one rule, one place."""
    return f"agency_{str(division).strip().lower().replace('-', '_')}_specialist"


def _actor(payload: dict[str, Any]) -> str:
    """Who is calling. History is keyed by it, so a wrong value leaks a user's log."""
    return str(payload.get("actor_id") or payload.get("user_id") or "unknown").strip() or "unknown"


def _text(payload: dict[str, Any]) -> str:
    """Pull the user's text out of wherever this particular route put it."""
    for key in ("question", "text", "query", "content", "prompt", "task"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _strip_command(text: str) -> str:
    """Drop a leading slash command so `/agency-find tiktok` searches `tiktok`."""
    if text.startswith("/"):
        _, _, rest = text.partition(" ")
        return rest.strip()
    return text


def _split_agent(text: str) -> tuple[str, str]:
    """Separate a leading agent reference from the rest of the request.

    `/agency-ask frontend-developer why is my LCP bad` names its agent; plain
    `/agency-ask why is my LCP bad` does not. The first word is treated as an
    agent only when the roster actually has it, so a question opening with a
    real word is never mistaken for a slug.
    """
    stripped = _strip_command(text)
    first, _, rest = stripped.partition(" ")
    candidate = first.strip().strip(",:")
    if candidate and rest.strip() and ROSTER.get(candidate) is not None:
        return candidate, rest.strip()
    return "", stripped


def _failure(tool: str, code: str, report: str, trace_id: str) -> dict[str, Any]:
    """One shape for every expected failure, so callers can branch on a code."""
    return {"status": "error", "tool": tool, "trace_id": trace_id, "error_code": code, "report": report}


def _catalog_missing(tool: str, trace_id: str) -> dict[str, Any]:
    return _failure(
        tool,
        "catalog_unavailable",
        "No personas were found under catalog/. Re-import the roster; see catalog/UPSTREAM.md.",
        trace_id,
    )


def _llm_timeout(llm: Any | None) -> float:
    """Return the request timeout the host resolved for this plugin.

    Always positive when an LLM config was injected: a 0 saved in
    `llm.timeout_sec` means "not set", and the host falls through to the
    provider's value and then the global one.
    """
    return float(getattr(llm, "timeout_sec", None) or DEFAULT_TIMEOUT_SEC)


# ---------------------------------------------------------------------------
# 1. Find the right specialist - offline, deterministic
# ---------------------------------------------------------------------------


@plugin_tool("agency_agents_roster_tool", tags=("agency", "roster", "read", "command"))
class AgencyRosterTool:
    """Search the vendored roster and report which personas fit a request."""

    description = (
        "Search the Agency roster of specialist AI personas by keywords or division and return the best matches "
        "with their speciality, division and slug. Read-only, offline: no model call."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What the work is about, e.g. 'react performance' or 'tiktok launch'. Empty lists the roster.",
            },
            "division": {
                "type": "string",
                "description": "Restrict to one division, e.g. engineering, design, marketing, security.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 8},
        },
    }
    consequential = False

    def __init__(self, plugin: Any | None = None, **_: Any) -> None:
        self.plugin = plugin

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(payload.get("trace_id") or "trace-agency-roster")
        if not ROSTER.available:
            return _catalog_missing("agency_agents_roster_tool", trace_id)
        query = _strip_command(_text(payload))
        division = str(payload.get("division") or "").strip().lower()
        if division and ROSTER.division(division) is None:
            return _failure(
                "agency_agents_roster_tool",
                "unknown_division",
                f"No division {division!r}. Known: {', '.join(item.key for item in ROSTER.divisions())}.",
                trace_id,
            )
        limit = int(payload.get("limit") or roster_settings(self.plugin).get("max_results") or 8)
        matches = ROSTER.search(query, division=division, limit=limit)
        return {
            "status": "success",
            "tool": "agency_agents_roster_tool",
            "trace_id": trace_id,
            "query": query,
            "division": division,
            "total_agents": ROSTER.count(),
            "matches": [{**agent.summary(), "score": score} for agent, score in matches],
            "divisions": [
                {"key": item.key, "label": item.label, "agents": item.agents} for item in ROSTER.divisions()
            ],
            "report": (
                f"{len(matches)} of {ROSTER.count()} personas match {query!r}."
                if query
                else f"{ROSTER.count()} personas across {len(ROSTER.divisions())} divisions."
            ),
        }


# ---------------------------------------------------------------------------
# 2. Read one persona - offline, the prompt itself
# ---------------------------------------------------------------------------


@plugin_tool("agency_agents_brief_tool", tags=("agency", "persona", "read", "command"))
class AgencyBriefTool:
    """Return one persona's full prompt, so a caller can adopt it verbatim."""

    description = (
        "Return one Agency persona in full - identity, mission, rules and deliverables - ready to use as a system "
        "prompt. Takes an agent slug or display name, or picks the best match for a described task. Offline."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Agent slug or display name, e.g. engineering-backend-architect."},
            "query": {"type": "string", "description": "Used to pick an agent when `agent` is empty."},
            "division": {"type": "string", "description": "Narrow the pick to one division."},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 40000},
        },
    }
    consequential = False

    def __init__(self, plugin: Any | None = None, **_: Any) -> None:
        self.plugin = plugin

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(payload.get("trace_id") or "trace-agency-brief")
        if not ROSTER.available:
            return _catalog_missing("agency_agents_brief_tool", trace_id)
        settings = roster_settings(self.plugin)
        requested, remainder = _split_agent(_text(payload))
        wanted = str(payload.get("agent") or payload.get("slug") or requested).strip()
        division = str(payload.get("division") or "").strip().lower()

        agent = ROSTER.get(wanted) if wanted else ROSTER.best(remainder, division=division)
        if agent is None:
            return _failure(
                "agency_agents_brief_tool",
                "agent_not_found",
                f"No persona matches {wanted or remainder!r}. Use agency_agents_roster_tool to search.",
                trace_id,
            )
        limit = int(
            payload.get("max_chars") or settings.get("brief_max_chars") or _catalog_module.DEFAULT_BRIEF_CHARS
        )
        brief = ROSTER.brief(agent.slug, limit) or {}
        return {
            "status": "success",
            "tool": "agency_agents_brief_tool",
            "trace_id": trace_id,
            "agent": brief,
            "matched_by": "slug" if wanted else "query",
            "report": f"{agent.emoji} {agent.name} ({agent.division}) - {agent.description[:160]}",
        }


# ---------------------------------------------------------------------------
# 3. Assemble a panel - offline, and shaped so the suggestion compiles
# ---------------------------------------------------------------------------


@plugin_tool("agency_agents_panel_tool", tags=("agency", "panel", "read", "command"))
class AgencyPanelTool:
    """Pick complementary personas for one task and name the node that runs them."""

    description = (
        "Assemble a panel of complementary Agency personas for a task - one per division - and return the vote, "
        "dialogue or consensus node configuration that would put them to work. Offline, no model call."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the panel should deliberate on."},
            "size": {"type": "integer", "minimum": 1, "maximum": 7, "description": "How many personas to pick."},
            "mode": {
                "type": "string",
                "enum": ["dialogue", "vote", "consensus"],
                "description": "dialogue is exactly two voices, vote needs two or more, consensus judges one proposal.",
            },
        },
        "required": ["task"],
    }
    consequential = False

    def __init__(self, plugin: Any | None = None, **_: Any) -> None:
        self.plugin = plugin

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(payload.get("trace_id") or "trace-agency-panel")
        if not ROSTER.available:
            return _catalog_missing("agency_agents_panel_tool", trace_id)
        task = _strip_command(_text(payload))
        if not task:
            return _failure("agency_agents_panel_tool", "missing_task", "Describe the task first.", trace_id)

        settings = _section(self.plugin, "panel")
        mode = str(payload.get("mode") or settings.get("mode") or DEFAULT_PANEL_MODE).strip().lower()
        if mode not in PANEL_LIMITS:
            return _failure(
                "agency_agents_panel_tool",
                "unknown_mode",
                f"mode must be one of {', '.join(sorted(PANEL_LIMITS))}.",
                trace_id,
            )
        low, high = PANEL_LIMITS[mode]
        size = max(low, min(int(payload.get("size") or settings.get("size") or low), high))

        picked = ROSTER.spread(task, size)
        if not picked:
            # Nothing in the roster's frontmatter overlaps the task's words. A
            # panel of nobody is not a panel, so say so rather than returning an
            # empty node for the compiler to choke on.
            return _failure(
                "agency_agents_panel_tool",
                "no_match",
                f"No persona matches {task[:60]!r}. Search with agency_agents_roster_tool and name them instead.",
                trace_id,
            )
        registered = set(subagent_divisions(self.plugin))
        panel = [
            {
                **agent.summary(),
                # A persona only becomes routable when its division is registered
                # as a specialist; saying so is the difference between a panel
                # that runs and one the compiler rejects.
                "subagent": specialist_name(agent.division) if agent.division in registered else "",
            }
            for agent in picked
        ]
        voters = list(dict.fromkeys(entry["subagent"] for entry in panel if entry["subagent"]))
        return {
            "status": "success",
            "tool": "agency_agents_panel_tool",
            "trace_id": trace_id,
            "task": task,
            "mode": mode,
            "panel": panel,
            "voters": voters,
            "suggested_node": self._node(mode, task, voters),
            "routable": len(voters) >= low,
            "report": self._report(mode, panel, voters, low),
        }

    @staticmethod
    def _node(mode: str, task: str, voters: list[str]) -> dict[str, Any]:
        """Build the node config the host's compiler accepts for this mode."""
        if mode == "vote":
            return {"type": "vote", "config": {"topic": task, "voters": voters, "on_voter_error": "abstain"}}
        if mode == "consensus":
            return {"type": "consensus", "config": {"question": task, "judges": voters, "mode": "refute"}}
        return {"type": "dialogue", "config": {"topic": task, "participants": voters, "max_rounds": 3}}

    @staticmethod
    def _report(mode: str, panel: list[dict[str, Any]], voters: list[str], minimum: int) -> str:
        names = ", ".join(f"{entry['emoji']} {entry['name']}".strip() for entry in panel)
        if len(voters) >= minimum:
            return f"{mode} panel: {names}."
        return (
            f"{mode} panel: {names}. Only {len(voters)} of {len(panel)} sit in a division registered as a "
            f"subagent, and this mode needs {minimum}; add the missing divisions to roster.subagent_divisions "
            "to run it as a node."
        )


# ---------------------------------------------------------------------------
# 4. Consult a persona - the one tool that spends money
# ---------------------------------------------------------------------------


class AgencyConsultTool:
    """Answer a question as one of the roster's personas, through the host's LLM config."""

    description = (
        "Ask one Agency persona a question and return its answer. The persona's own prompt becomes the system "
        "prompt, so the reply carries that specialist's priorities. Sends one model request."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Agent slug or display name. Empty picks the best match."},
            "question": {"type": "string", "description": "What to ask the persona."},
            "division": {"type": "string", "description": "Narrow the automatic pick to one division."},
            "dry_run": {
                "type": "boolean",
                "description": "Resolve the persona and report what would be sent, without calling the provider.",
            },
        },
        "required": ["question"],
    }
    # It leaves the process and spends provider budget, so the approval gate
    # should see it even though nothing local changes.
    consequential = True

    def __init__(self, plugin: Any | None = None, runtime_context: dict[str, Any] | None = None, **_: Any) -> None:
        self.plugin = plugin
        context = runtime_context or {}
        # Resolved per plugin: config.llm here overrides the chosen provider,
        # which overrides the global default, field by field.
        self.llm = context.get("llm_config")
        self.storage = context.get("storage")

    @property
    def timeout_sec(self) -> float:
        """The executor's outer limit for one call.

        Without it the host's 30s default would cancel a request that
        `llm.timeout_sec` still allows. A property, not a plain attribute, so a
        changed timeout applies on the very next call.
        """
        return _llm_timeout(self.llm) + TIMEOUT_GRACE_SEC

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(payload.get("trace_id") or "trace-agency-consult")
        if not ROSTER.available:
            return _catalog_missing("agency_agents_consult_tool", trace_id)

        named, remainder = _split_agent(_text(payload))
        wanted = str(payload.get("agent") or payload.get("slug") or named).strip()
        question = str(payload.get("question") or "").strip() or remainder
        if not question:
            return _failure(
                "agency_agents_consult_tool", "missing_question", "Ask something: /agency-ask <agent> <question>.", trace_id
            )

        division = str(payload.get("division") or "").strip().lower()
        agent = ROSTER.get(wanted) if wanted else ROSTER.best(question, division=division)
        if agent is None:
            return _failure(
                "agency_agents_consult_tool",
                "agent_not_found",
                f"No persona matches {wanted or question[:40]!r}.",
                trace_id,
            )

        resolved = self._resolved()
        prompt = self._system_prompt(agent)
        if payload.get("dry_run"):
            # The offline path: everything resolved, nothing sent. What the
            # tests and `main.py call` use, and what an unconfigured host gets
            # instead of a failure.
            return {
                "status": "success",
                "tool": "agency_agents_consult_tool",
                "trace_id": trace_id,
                "dry_run": True,
                "agent": agent.summary(),
                "resolved": resolved,
                "would_send": {"model": resolved["model"], "system_chars": len(prompt), "question": question},
                "report": f"Would ask {agent.name} through {resolved['provider'] or 'no provider'}/{resolved['model'] or 'no model'}.",
            }
        if not resolved["api_key_present"] or not resolved["base_url"]:
            return _failure(
                "agency_agents_consult_tool",
                "llm_unconfigured",
                "No API key or base URL was resolved for this plugin. Pass dry_run to see the persona instead.",
                trace_id,
            )
        # The provider call is synchronous (urllib urlopen with its own timeout), so it
        # cannot run on the event loop: measured at 26.85s per call, which froze every
        # HTTP request the host was serving -- including unauthenticated ones -- for the
        # duration. A worker thread keeps the loop free while this waits on the network.
        return await asyncio.to_thread(
            self._ask,
            agent=agent,
            prompt=prompt,
            question=question,
            resolved=resolved,
            payload=payload,
            trace_id=trace_id,
        )

    # -- the provider call ---------------------------------------------------
    def _ask(
        self,
        *,
        agent: Any,
        prompt: str,
        question: str,
        resolved: dict[str, Any],
        payload: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        body = {
            "model": resolved["model"],
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": question}],
            "temperature": resolved["temperature"],
        }
        try:
            response = _post_json(
                f"{str(resolved['base_url']).rstrip('/')}/chat/completions",
                body,
                {"Authorization": f"Bearer {self.llm.api_key}", "Content-Type": "application/json"},
                resolved["timeout_sec"],
            )
        except (OSError, ValueError, error.HTTPError) as exc:
            # Catch what this call can actually raise; a bare `except Exception`
            # would swallow programming bugs above too. The provider's own error
            # text can carry server paths or key fragments, so it is logged.
            logger.warning("agency consult failed for %s: %s", agent.slug, type(exc).__name__)
            return _failure("agency_agents_consult_tool", "provider_error", "The provider call failed.", trace_id)

        choices = response.get("choices") or [{}]
        answer = str(choices[0].get("message", {}).get("content", ""))
        record = self._record(agent, question, answer, payload, resolved)
        return {
            "status": "success",
            "tool": "agency_agents_consult_tool",
            "trace_id": trace_id,
            "dry_run": False,
            "agent": agent.summary(),
            "answer": answer,
            "resolved": resolved,
            **({"record_id": record["id"]} if record else {}),
            "report": f"{agent.emoji} {agent.name} answered in {len(answer)} characters.",
        }

    def _record(
        self,
        agent: Any,
        question: str,
        answer: str,
        payload: dict[str, Any],
        resolved: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Log the consultation, trimmed to the retention setting."""
        if self.storage is None:
            return None
        actor = _actor(payload)
        history = _store.ConsultationStore(self.storage)
        record = history.add(actor, agent.summary(), question, answer, model=resolved["model"])
        history.prune(actor, int(consult_settings(self.plugin).get("keep_last") or _store.DEFAULT_KEEP_LAST))
        return record

    # -- prompt and configuration --------------------------------------------

    def _system_prompt(self, agent: Any) -> str:
        """Build the system prompt: the persona file, bounded, behind the preamble.

        `persona_max_chars` bounds the whole system prompt, not just the persona,
        so the preamble and the language instruction are measured first and the
        persona takes the room that is left. A configured limit is a budget the
        caller can rely on; one that the plugin's own boilerplate pushes past is
        not.
        """
        settings = consult_settings(self.plugin)
        limit = int(
            roster_settings(self.plugin).get("persona_max_chars") or _catalog_module.DEFAULT_PERSONA_CHARS
        )
        prefix = str(settings.get("system_prefix") or "").strip()
        language = str(settings.get("answer_language") or "auto").strip().lower()
        instruction = {
            "zh": "Reply in Chinese.",
            "en": "Reply in English.",
        }.get(language, "Reply in the language the question was asked in.")
        heading = f"You are {agent.name}, {agent.description}".strip()
        framing = [part for part in (prefix, heading, instruction) if part]
        # The separators count too: three parts joined by blank lines.
        budget = max(MIN_PERSONA_CHARS, limit - len(_JOIN.join(framing)) - len(_JOIN) * len(framing))
        persona = ROSTER.persona(agent)[:budget]
        return _JOIN.join(part for part in (prefix, heading, persona, instruction) if part)

    def _resolved(self) -> dict[str, Any]:
        """Report what the host handed this plugin, never the key itself."""
        llm = self.llm
        return {
            "provider": getattr(llm, "provider_name", "") or getattr(llm, "provider", ""),
            "model": getattr(llm, "model", ""),
            "base_url": getattr(llm, "base_url", "") or "",
            "temperature": getattr(llm, "temperature", 0.3),
            "max_tokens": getattr(llm, "max_tokens", 0),
            "timeout_sec": _llm_timeout(llm),
            "api_key_present": bool(getattr(llm, "api_key", "")),
        }


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST JSON over http(s) only."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        msg = "LLM base URL must use http or https and include a hostname"
        raise ValueError(msg)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(url=url, data=payload, headers=headers, method="POST")
    # Scheme and hostname are validated directly above.
    with request.urlopen(req, timeout=timeout) as response:  # nosec B310
        parsed_body = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed_body, dict):
        msg = "provider response is not a JSON object"
        raise ValueError(msg)
    return parsed_body


# ---------------------------------------------------------------------------
# The explicit entry point. Called before decorated members are scanned.
# ---------------------------------------------------------------------------


def register_tools(tool_registry: Any, plugin: Any, runtime_context: dict[str, Any]) -> None:
    """Register the tool that needs constructor arguments of its own.

    `tool_registry` is a guard scoped to this plugin: it refuses names reserved
    by builtin tools, which is why every name here carries the plugin's own.
    """
    tool_registry.register(
        AgencyConsultTool(plugin=plugin, runtime_context=runtime_context),
        name="agency_agents_consult_tool",
        tags=("agency", "persona", "llm", "explicit-registration"),
    )
