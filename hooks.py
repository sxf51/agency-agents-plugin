"""Hooks - three handlers, each earning its place in every inbound message.

The contract is the same at every point:

    async def handler(context: dict) -> dict | None

Return a new context to apply changes, or None to pass through. Copy the dict
rather than mutating the one you were given: other plugins' handlers run on the
same object, and an in-place edit makes registration order part of your
behaviour. Handlers run in ascending priority, and the runtime times each
plugin's handlers separately, so a slow one is attributable to you by name.

Why these three:

* `before_route` answers `/agency-help` without an LLM turn, and recognises the
  upstream idiom - "activate Frontend Developer mode" - as a routing hint. That
  phrasing is how the agency-agents README tells people to summon a persona, so
  a port that ignores it loses the habit its users already have.
* `before_node_execute` attaches the caller identity. Consultation history is
  keyed by it, and the model must never be the source of an actor id.
* `after_tool_call` names the persona behind an answer, so a reply that came
  from a vendored prompt says so.

Unregistering is automatic: the host clears everything this plugin registered
when it is disabled, reloaded or uninstalled.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from extension.hook import HookPoint

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

# Every tool this plugin owns. Hooks fire for the whole runtime, so the first
# thing each handler does is check whether the event is even ours.
AGENCY_TOOLS = frozenset(
    {
        "agency_agents_roster_tool",
        "agency_agents_brief_tool",
        "agency_agents_panel_tool",
        "agency_agents_consult_tool",
    }
)
ROUTER_SUBAGENT = "agency_agents_router"
# "activate Frontend Developer mode", "act as the TikTok Strategist", "扮演
# Backend Architect" - the ways the upstream README tells a reader to summon a
# persona. The trigger only says where a name might start; what follows is
# matched against the roster, so a sentence that merely reads like this does not
# become a hint.
SUMMON_TRIGGER = re.compile(
    r"(?:\b(?:activate|acting as|act as|channel|summon|as)\b|扮演|使用|切换到)\s*(?:the\s+)?",
    re.IGNORECASE,
)
NAME_WORD = re.compile(r"[A-Za-z0-9][\w.&/-]*")
# "Frontend Developer mode", "TikTok Strategist persona" - the noun the reader
# adds after the name, which is not part of it.
NAME_SUFFIXES = frozenset({"mode", "persona", "agent", "specialist", "模式", "身份"})
# A persona name is one to four words: "SRE", "Backend Architect", "Section 508
# Accessibility Specialist". Reading further just wastes lookups.
MAX_NAME_WORDS = 4
HELP_COMMAND = "/agency-help"
HELP_TEXT = (
    "Agency roster - {count} specialist personas in {divisions} divisions.\n"
    "/agency <task>            route the task to the best-matched specialist\n"
    "/agency-find <keywords>   search the roster\n"
    "/agency-brief <agent>     read one persona's full prompt\n"
    "/agency-ask <agent> <q>   ask that persona a question\n"
    "/agency-panel <task>      assemble a vote, dialogue or consensus panel\n"
    "Open the Agency Roster page to browse divisions and consult a persona there."
)


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
ROSTER = _catalog_module.Catalog()


def _message_text(context: dict[str, Any]) -> str:
    message = context.get("message")
    return str(message.get("text", "") if isinstance(message, dict) else "").strip()


def _node_target(context: dict[str, Any]) -> str:
    """Which tool or subagent a DAG node points at."""
    node = context.get("node")
    runtime_config = node.get("runtime_config", {}) if isinstance(node, dict) else {}
    return str(runtime_config.get("target", "")) if isinstance(runtime_config, dict) else ""


def summoned_agent(text: str) -> Any | None:
    """Return the persona a message asks for by name, or None.

    Matching the trigger is not enough - "as soon as possible" matches it. After
    the trigger, the following words are tried longest-first against the roster,
    and only a real persona produces a hint, so a false trigger costs a few
    dictionary lookups and nothing else.

    A single word has to name a persona exactly. The roster's loose lookup
    resolves "developer" to whichever slug happens to end that way, which would
    turn "as a developer, review this" into a routing hint for one arbitrary
    frontend agent.
    """
    if not text or not ROSTER.available:
        return None
    for match in SUMMON_TRIGGER.finditer(text):
        words = NAME_WORD.findall(text[match.end() :])[:MAX_NAME_WORDS]
        for size in range(len(words), 0, -1):
            span = words[:size]
            # "Section 508 Accessibility Specialist" ends in one of the words a
            # reader also tacks on ("... Specialist mode"), so the span is tried
            # whole first and only then without its last word.
            for candidate_words in ([span, span[:-1]] if span[-1].lower() in NAME_SUFFIXES else [span]):
                if not candidate_words:
                    continue
                candidate = " ".join(candidate_words)
                found = ROSTER.get(candidate) if len(candidate_words) > 1 else _exact_agent(candidate)
                if found is not None:
                    return found
    return None


def _exact_agent(word: str) -> Any | None:
    """Resolve a one-word reference, but only when it is unambiguous.

    A whole slug or name, or a slug tail that exactly one persona has: "sre"
    reaches `engineering-sre`, while "developer" reaches a dozen and so reaches
    none of them.
    """
    lowered = word.strip().lower()
    found = ROSTER.agents.get(lowered)
    if found is not None:
        return found
    named = [agent for agent in ROSTER.agents.values() if agent.name.lower() == lowered]
    if named:
        return named[0]
    tails = [agent for agent in ROSTER.agents.values() if agent.slug.endswith(f"-{lowered}")]
    return tails[0] if len(tails) == 1 else None


def register_hooks(hook_manager: Any, plugin: Any, runtime_context: dict[str, Any]) -> None:
    """Register this plugin's three handlers."""
    _ = runtime_context
    name = str(getattr(plugin, "name", "agency-agents-plugin"))

    # -- 1. before_route ----------------------------------------------------
    # The only point that can end a turn on its own, through
    # `route_outcome.action`: continue (the default), respond, or drop. The
    # first respond or drop short-circuits every plugin's remaining handlers,
    # so a message is claimed only when it is unambiguously ours.
    async def before_route(context: dict[str, Any]) -> dict[str, Any]:
        updated = dict(context)
        text = _message_text(updated)
        lowered = text.lower()

        if lowered.startswith(HELP_COMMAND):
            # Answered here and nowhere else: no command entry in plugin.yaml,
            # no tool, no model call.
            updated["route_outcome"] = {
                "action": "respond",
                "response": HELP_TEXT.format(count=ROSTER.count(), divisions=len(ROSTER.divisions())),
            }
            return updated

        agent = summoned_agent(text) if not lowered.startswith("/") else None
        if lowered.startswith("/agency") or agent is not None:
            # A soft hint the planner may weigh, rather than a decision hard-coded
            # in a hook. When the persona was named, say which one: the router
            # would find it anyway, but the hint survives into the trace.
            hints: dict[str, Any] = {
                "plugin": name,
                "preferred_route": "subagent",
                "preferred_subagent": ROUTER_SUBAGENT,
                "fallback_route": "tool",
                "fallback_tool": "agency_agents_roster_tool",
            }
            if agent is not None:
                hints["preferred_agent"] = agent.slug
                hints["preferred_domain"] = agent.division
            updated["router_hints"] = hints
        return updated

    # -- 2. before_node_execute ---------------------------------------------
    # One node's payload, immediately before it runs. This is where caller
    # identity gets attached: the planner builds payloads from what the model
    # produced, and the model must never be the source of an actor id.
    async def before_node_execute(context: dict[str, Any]) -> dict[str, Any]:
        if _node_target(context) not in AGENCY_TOOLS:
            return context
        payload = dict(context.get("payload") or {})
        task = context.get("task")
        if isinstance(task, dict):
            actor_id = str(task.get("actor_id") or task.get("user_id") or "").strip()
            if actor_id:
                payload.setdefault("actor_id", actor_id)
        return {**context, "payload": payload}

    # -- 3. after_tool_call -------------------------------------------------
    # The result on its way back out of the executor. The `result` key must
    # survive, so the dict is copied and added to, never replaced.
    async def after_tool_call(context: dict[str, Any]) -> dict[str, Any]:
        if str(context.get("tool") or "") not in AGENCY_TOOLS:
            return context
        result = context.get("result")
        if not isinstance(result, dict):
            return context
        enriched = dict(result)
        enriched.setdefault("plugin", name)
        agent = enriched.get("agent")
        if isinstance(agent, dict) and agent.get("name"):
            # Attribution, not decoration: this answer came out of a vendored
            # persona file, and the reader should be able to see which one.
            enriched.setdefault(
                "persona_source",
                {
                    "agent": agent.get("name"),
                    "slug": agent.get("slug"),
                    "division": agent.get("division"),
                    "upstream": "msitarzewski/agency-agents",
                },
            )
        return {**context, "result": enriched}

    hook_manager.register(name, HookPoint.before_route, before_route, priority=30)
    hook_manager.register(name, HookPoint.before_node_execute, before_node_execute, priority=40)
    hook_manager.register(name, HookPoint.after_tool_call, after_tool_call, priority=90)
