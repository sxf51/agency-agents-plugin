"""SubAgents - one router plus one specialist per registered division.

A subagent is a class with one method:

    async def run(task: dict, context: dict, decision: dict) -> dict

`domain` and `capabilities` are what the planner matches a task against, so they
are routing metadata rather than documentation. That shapes the whole design
here. The roster holds 279 personas; registering 279 subagents would put 279
competing descriptions in front of the planner and make every routing decision
worse. So the planner sees:

* `agency_agents_router` - domain `agency-roster`, for "which specialist should
  look at this" and for requests that name a persona outright.
* `agency_<division>_specialist` - one per division named in
  `roster.subagent_divisions`, with the division as its domain and its own
  personas as its capabilities. `engineering` advertises `backend-architect`,
  `frontend-developer`, `sre` and so on, which is what a task description
  actually looks like.

Picking the persona is then a catalog lookup inside the chosen agent, not a
planner decision, and it costs nothing: the roster is data on disk.

Each agent works through this plugin's own tools, via the restricted registry
view the host injects - never by reaching for a provider itself. That keeps one
copy of the provider call, in `tools.py`, behind the approval gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from extension.plugin import plugin_subagent

if TYPE_CHECKING:
    from types import ModuleType

# Vote answers the runtime understands. Anything unparseable counts as abstain.
APPROVE = "approve"
REJECT = "reject"
ABSTAIN = "abstain"
# What a persona's answer has to open with for the vote to be counted. Checked
# against the first line only: a body that argues both sides mentions both words.
VOTE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (APPROVE, ("approve", "yes", "agree", "support", "+1", "赞成", "同意", "支持")),
    (REJECT, ("reject", "no", "disagree", "oppose", "-1", "反对", "不同意", "拒绝")),
    (ABSTAIN, ("abstain", "unsure", "弃权", "无法判断")),
)
VOTE_FIRST_LINE_CHARS = 200
# How many personas a division advertises to the planner. All 64 engineering
# slugs would be a wall of text in the routing prompt for no extra precision.
MAX_CAPABILITIES = 12
TOOLS_FOR_SPECIALIST = ("agency_agents_roster_tool", "agency_agents_brief_tool", "agency_agents_consult_tool")
# The score a match needs before this agent acts on it alone. Search is keyword
# matching over one-sentence descriptions, so a weak top score means the words
# happened to overlap, not that the right specialist was found - and answering a
# Kubernetes question in the voice of a finance tracker is worse than asking.
# 12 is one name-word hit, or any two query words matched: see catalog.py.
MIN_CONFIDENT_SCORE = 12
CANDIDATE_COUNT = 4


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


def _settings(plugin: Any | None, key: str) -> dict[str, Any]:
    config = getattr(plugin, "config", None)
    section = config.get(key) if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def specialist_name(division: str) -> str:
    """Return the subagent name a division is registered under; mirrors `tools.py`."""
    return f"agency_{str(division).strip().lower().replace('-', '_')}_specialist"


def division_capabilities(division: str) -> tuple[str, ...]:
    """Return the personas a division advertises, shortest-slug first.

    Upstream slugs are prefixed with their division (`engineering-sre`), which
    is redundant next to `domain="engineering"`, so the prefix is dropped: the
    planner matches `sre`, not `engineering-sre`.
    """
    agents = ROSTER.in_division(division)
    trimmed = sorted(
        {agent.slug.removeprefix(f"{division}-") for agent in agents},
        key=lambda slug: (len(slug), slug),
    )
    return tuple(trimmed[:MAX_CAPABILITIES])


def parse_vote(answer: str) -> tuple[str, str]:
    """Read a choice out of a persona's prose reply.

    Returns `(choice, reason)`. Only the opening of the answer decides the
    choice - a persona that explains itself will mention "reject" while arguing
    for approval, and scanning the whole body would pick up whichever word came
    last.
    """
    text = str(answer or "").strip()
    if not text:
        return ABSTAIN, "The persona returned nothing."
    opening = text.split("\n", 1)[0][:VOTE_FIRST_LINE_CHARS].lower()
    # The earliest verdict word wins, not the first one this table happens to
    # list: "Reject - I would approve a version with tests" is a rejection, and
    # checking approvals first would read it as the opposite.
    earliest: tuple[int, str] | None = None
    for choice, words in VOTE_WORDS:
        positions = [opening.find(word) for word in words if word in opening]
        if positions and (earliest is None or min(positions) < earliest[0]):
            earliest = (min(positions), choice)
    return (earliest[1] if earliest else ABSTAIN), text[:600]


class PersonaAgent:
    """Shared behaviour: pick a persona, then either consult it or brief it.

    One class, two registrations. The router searches the whole roster; a
    specialist is the same logic pinned to one division. Subclassing per
    division would produce 18 identical classes.
    """

    def __init__(
        self,
        name: str,
        division: str = "",
        plugin: Any | None = None,
        runtime_context: dict[str, Any] | None = None,
        tools: Any | None = None,
    ) -> None:
        self.name = name
        self.division = division
        self.plugin = plugin
        self.runtime_context = runtime_context or {}
        # `tools` arrives already filtered to what this subagent may see. The
        # registry guard also assigns it here for an explicit registration, from
        # metadata["allowed_tools"] - which is why the attribute must exist.
        self.tools: Any | None = tools

    # -- the host's entry point ----------------------------------------------

    async def run(self, task: dict[str, Any], context: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        """Answer a task, or a vote node's ballot, as the best-matched persona."""
        _ = (context, decision)
        trace_id = str(task.get("trace_id") or f"trace-{self.name}")
        if not ROSTER.available:
            return self._error(trace_id, "catalog_unavailable", "No personas were found under catalog/.")

        query = self._query(task)
        vote_spec = task.get("_vote_spec")
        if isinstance(vote_spec, dict):
            return await self._vote(vote_spec, task, trace_id)
        if not query:
            return self._error(trace_id, "missing_query", "The task carried no text to match a persona against.")

        selected = self._select(query, trace_id)
        if "result" in selected:
            # Nothing to speak as: either the choice went back to the caller or
            # the division turned out to be empty.
            return selected["result"]
        return await self._respond(selected, query, task, trace_id)

    def _select(self, query: str, trace_id: str) -> dict[str, Any]:
        """Choose the persona to answer as, or the answer to give instead."""
        ranked = ROSTER.search(query, division=self.division, limit=CANDIDATE_COUNT)
        confident = bool(ranked) and ranked[0][1] >= MIN_CONFIDENT_SCORE
        if not confident and not self.division:
            # The router had the whole roster to choose from and still found
            # nothing convincing, so there is no specialist to speak as.
            return {"result": self._ask_the_caller(query, ranked, trace_id)}
        if not ranked:
            # A specialist was routed to for its division, so it answers from
            # inside it even when the words barely overlap - that routing
            # decision was already made, and `low_confidence` says how firm the
            # persona pick underneath it is.
            fallback = ROSTER.best("", division=self.division)
            if fallback is None:
                return {"result": self._error(trace_id, "division_empty", f"No personas for {self.division!r}.")}
            ranked = [(fallback, 0)]
        return {"agent": ranked[0][0], "score": ranked[0][1], "confident": confident}

    async def _respond(
        self,
        selected: dict[str, Any],
        query: str,
        task: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        """Answer as the selected persona, or hand its brief back when it cannot."""
        agent, score, confident = selected["agent"], selected["score"], selected["confident"]

        if self._auto_run():
            consulted = await self._consult(agent.slug, query, task)
            if consulted is not None and consulted.get("status") == "success":
                return {
                    "status": "success",
                    "subagent": self.name,
                    "trace_id": trace_id,
                    "domain": self.domain,
                    "agent": agent.summary(),
                    "match_score": score,
                    "low_confidence": not confident,
                    "answer": str(consulted.get("answer") or ""),
                    "report": f"{agent.emoji} {agent.name} answered as {self.name}.",
                }
            # Falling back rather than failing: an unconfigured provider still
            # leaves the persona itself useful to whoever asked.
            fallback = str((consulted or {}).get("error_code") or "consult_unavailable")
        else:
            fallback = "auto_run_disabled"

        brief = await self._brief(agent.slug, task)
        return {
            "status": "success",
            "subagent": self.name,
            "trace_id": trace_id,
            "domain": self.domain,
            "agent": brief or agent.summary(),
            "match_score": score,
            "low_confidence": not confident,
            "consulted": False,
            "fallback_reason": fallback,
            "report": (
                f"Selected {agent.emoji} {agent.name} ({agent.division}) and returned the persona brief "
                f"instead of an answer ({fallback})."
            ),
        }

    def _ask_the_caller(
        self,
        query: str,
        ranked: list[tuple[Any, int]],
        trace_id: str,
    ) -> dict[str, Any]:
        """Hand back candidates instead of acting on a weak match.

        Keyword search over one-sentence descriptions has no answer for a task
        whose words appear nowhere in the roster - "our k8s cluster keeps
        evicting pods" matches nothing, because no persona's frontmatter says
        Kubernetes. Picking the top of that ranking produces a confident answer
        from the wrong specialist, which is the one outcome worth avoiding, so
        the choice goes back to the caller with what the roster does have.
        """
        pool = ranked or ROSTER.search("", division=self.division, limit=CANDIDATE_COUNT)
        return {
            "status": "success",
            "subagent": self.name,
            "trace_id": trace_id,
            "domain": self.domain,
            "needs_selection": True,
            "query": query,
            "candidates": [{**agent.summary(), "score": score} for agent, score in pool],
            "divisions": [
                {"key": item.key, "label": item.label, "agents": item.agents} for item in ROSTER.divisions()
            ],
            "report": (
                f"No persona clearly matches {query[:60]!r}. "
                "Name one of the candidates, or search the roster with agency_agents_roster_tool."
            ),
        }

    # -- routing metadata the planner reads ----------------------------------

    @property
    def domain(self) -> str:
        return self.division or "agency-roster"

    # -- the two things it can do -------------------------------------------

    async def _consult(self, slug: str, question: str, task: dict[str, Any]) -> dict[str, Any] | None:
        tool = self._tool("agency_agents_consult_tool")
        if tool is None:
            return None
        return await tool.execute(
            {
                "agent": slug,
                "question": question,
                "actor_id": task.get("actor_id") or task.get("user_id"),
                "trace_id": task.get("trace_id"),
            }
        )

    async def _brief(self, slug: str, task: dict[str, Any]) -> dict[str, Any] | None:
        tool = self._tool("agency_agents_brief_tool")
        if tool is None:
            return None
        result = await tool.execute({"agent": slug, "trace_id": task.get("trace_id")})
        agent = result.get("agent") if isinstance(result, dict) else None
        return agent if isinstance(agent, dict) else None

    async def _vote(self, spec: dict[str, Any], task: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """Answer a `vote` node, as whichever persona fits the proposal.

        The task carries `_vote_spec` with `topic`, `proposal`, `objective` and
        `choices`. A choice plus a reason goes back, at the top level.
        """
        proposal = str(spec.get("proposal") or spec.get("topic") or "").strip()
        # A specialist was put on the ballot for its division, so it votes from
        # whichever of its own personas is closest even when the overlap is
        # thin. The router has no such mandate: with no clear match it has no
        # point of view to vote from, and abstaining says so.
        topic = f"{spec.get('topic', '')} {proposal}"
        agent = ROSTER.best(topic, division=self.division) if self.division else self._confident(topic)
        if agent is None:
            return {
                **self._ballot(ABSTAIN, "No persona matches this proposal closely enough to vote on it."),
                "trace_id": trace_id,
            }

        question = (
            f"Proposal: {proposal}\n"
            f"Objective: {str(spec.get('objective') or '').strip() or 'judge the proposal on its merits'}\n\n"
            "Answer with approve, reject or abstain on the first line, from your own speciality's point of view, "
            "then one short paragraph of reasoning."
        )
        consulted = await self._consult(agent.slug, question, task)
        if consulted is None or consulted.get("status") != "success":
            reason = str((consulted or {}).get("report") or "The consult tool is unavailable to this subagent.")
            return {**self._ballot(ABSTAIN, reason), "trace_id": trace_id, "agent": agent.summary()}
        choice, reason = parse_vote(str(consulted.get("answer") or ""))
        return {**self._ballot(choice, reason), "trace_id": trace_id, "agent": agent.summary()}

    @staticmethod
    def _confident(query: str) -> Any | None:
        """Return the best match, or None when the ranking is too weak to stand on."""
        ranked = ROSTER.search(query, limit=1)
        if not ranked or ranked[0][1] < MIN_CONFIDENT_SCORE:
            return None
        return ranked[0][0]

    def _ballot(self, choice: str, reason: str) -> dict[str, Any]:
        return {"status": "success", "subagent": self.name, "choice": choice, "reason": reason}

    # -- plumbing ------------------------------------------------------------

    def _tool(self, name: str) -> Any | None:
        registry = self.tools or self.runtime_context.get("tool_registry")
        if registry is None:
            return None
        # A None here usually means an allow-list excluded the tool, not that it
        # failed to register.
        return registry.get(name)

    def _auto_run(self) -> bool:
        return bool(_settings(self.plugin, "consult").get("auto_run", True))

    @staticmethod
    def _query(task: dict[str, Any]) -> str:
        for key in ("query", "question", "text", "content", "prompt"):
            value = str(task.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _error(self, trace_id: str, code: str, report: str) -> dict[str, Any]:
        return {
            "status": "error",
            "subagent": self.name,
            "trace_id": trace_id,
            "error_code": code,
            "report": report,
        }


@plugin_subagent(
    "agency_agents_router",
    domain="agency-roster",
    capabilities=(
        "specialist-selection",
        "persona-briefing",
        "expert-consultation",
        "multi-division-routing",
    ),
    tools=TOOLS_FOR_SPECIALIST,
)
class AgencyRouter(PersonaAgent):
    """Pick the right specialist out of the whole Agency roster and put it to work."""

    def __init__(
        self,
        domain: str = "agency-roster",
        capabilities: tuple[str, ...] = (),
        plugin: Any | None = None,
        runtime_context: dict[str, Any] | None = None,
        tools: Any | None = None,
        **_: Any,
    ) -> None:
        # The host injects by parameter name, so only what is used is declared.
        super().__init__(
            "agency_agents_router",
            division="",
            plugin=plugin,
            runtime_context=runtime_context,
            tools=tools,
        )
        self._domain = domain or "agency-roster"
        self.capabilities = capabilities

    @property
    def domain(self) -> str:
        return self._domain


class DivisionSpecialist(PersonaAgent):
    """One division's personas, behind a single routable agent."""

    def __init__(self, division: str, plugin: Any | None, runtime_context: dict[str, Any]) -> None:
        super().__init__(specialist_name(division), division=division, plugin=plugin, runtime_context=runtime_context)
        self.capabilities = division_capabilities(division)


def register_subagents(subagent_registry: Any, plugin: Any, runtime_context: dict[str, Any]) -> None:
    """Register one specialist per division the operator asked for.

    The division list is configuration, not code, because how many domain
    experts belong in a deployment's routing prompt is a deployment decision.
    A division with no personas on disk is skipped rather than registered with
    empty capabilities, which the planner could never match.

    The decorated router above is still discovered afterwards, so both styles
    coexist.
    """
    declared = _settings(plugin, "roster").get("subagent_divisions")
    divisions = [str(item).strip().lower() for item in declared if str(item).strip()] if isinstance(declared, list) else []

    for division in dict.fromkeys(divisions):
        meta = ROSTER.division(division)
        capabilities = division_capabilities(division)
        if meta is None or not capabilities:
            continue
        subagent_registry.register(
            specialist_name(division),
            DivisionSpecialist(division, plugin, runtime_context),
            domain=division,
            capabilities=capabilities,
            metadata={
                "description": (
                    f"{meta.label} division of the Agency roster: {meta.agents} specialist personas, "
                    "consulted through their own prompts."
                ),
                # The registry guard reads this, builds the filtered tool view
                # and assigns it to the instance's `tools` attribute.
                "allowed_tools": TOOLS_FOR_SPECIALIST,
            },
        )
