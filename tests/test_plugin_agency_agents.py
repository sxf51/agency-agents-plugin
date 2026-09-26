"""What *this* plugin does: the vendored roster, and everything built on it.

`test_plugin_contract.py` next to it holds the checks every plugin should pass;
those are generic. This file is the other half - the behaviour only this plugin
has.

The pattern: ask for the `bundle` fixture, reach for a tool, subagent, hook or
endpoint by name, and assert on what comes back. The bodies never ask which
backend is running, so the same tests cover the real PluginManager inside the
project and the stand-ins outside it.

No Redis and no network either way. Storage falls back to a JSON file, and the
one tool that would call a provider is exercised through its `dry_run` path -
which is also the path an unconfigured deployment takes, so it is worth pinning
down rather than mocking around.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

EXPECTED_TOOLS = {
    "agency_agents_roster_tool",
    "agency_agents_brief_tool",
    "agency_agents_panel_tool",
    "agency_agents_consult_tool",
}
EXPECTED_COMMANDS = {"/agency", "/agency-find", "/agency-brief", "/agency-panel", "/agency-ask"}
ROUTER = "agency_agents_router"
# The upstream import, as `catalog/UPSTREAM.md` records it. A catalog that loses
# personas to a bad re-import should fail here rather than quietly shrink.
EXPECTED_AGENTS = 279
EXPECTED_DIVISIONS = 18
DIALOGUE_PARTICIPANTS = 2
MIN_VOTE_VOTERS = 2
MAX_PANEL = 7
HOST_DEFAULT_TOOL_TIMEOUT = 30
# Mirrors subagents.MIN_CONFIDENT_SCORE: one name-word hit, or two words matched.
MIN_CONFIDENT_SCORE = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module(plugin_dir: Path, filename: str) -> Any:
    """Load one of this plugin's by-path modules the way the plugin loads it."""
    name = f"test_agency_{Path(filename).stem}"
    spec = importlib.util.spec_from_file_location(name, plugin_dir / filename)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Registered before execution because dataclasses resolves annotations
    # through sys.modules - the same reason the plugin's own loader does it.
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _catalog(plugin_dir: Path) -> Any:
    return _module(plugin_dir, "catalog.py").Catalog()


def _call(host: Any, bundle: Any, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
    return asyncio.run(host.call_web(bundle, endpoint, method, **kwargs))


def _body(response: Any) -> Any:
    """Read a helper response, whichever backend produced it."""
    if isinstance(response, dict | list):
        return response
    content = getattr(response, "content", None)
    if content is not None:
        return content
    return json.loads(bytes(response.body))


def _hook(bundle: Any, point: str, context: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(bundle.hooks.trigger(point, context))


def _run(tool: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(tool.execute(payload))


def _names(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("name") or "") for row in rows]


# ---------------------------------------------------------------------------
# The vendored catalog
# ---------------------------------------------------------------------------


def test_catalog_ships_with_the_roster_it_documents(plugin_dir: Path) -> None:
    catalog = _catalog(plugin_dir)
    assert catalog.available
    assert catalog.count() == EXPECTED_AGENTS
    assert len(catalog.divisions()) == EXPECTED_DIVISIONS
    # Labels come from the upstream divisions.json, not from a copy in the code.
    labels = {item.key: item.label for item in catalog.divisions()}
    assert labels["game-development"] == "Game Development"
    assert labels["gis"] == "GIS"


def test_every_persona_carries_what_routing_needs(plugin_dir: Path) -> None:
    """A persona with no description cannot be searched for or explained."""
    thin = [
        agent.slug
        for agent in _catalog(plugin_dir).agents.values()
        if not agent.name or not agent.description or not agent.division
    ]
    assert thin == []


def test_upstream_attribution_travels_with_the_files(plugin_dir: Path) -> None:
    """The catalog is MIT-licensed third-party content; the notice ships with it."""
    catalog_dir = plugin_dir / "catalog"
    assert (catalog_dir / "LICENSE").is_file()
    notice = (catalog_dir / "UPSTREAM.md").read_text(encoding="utf-8")
    assert "msitarzewski/agency-agents" in notice
    assert re.search(r"\b[0-9a-f]{40}\b", notice), "the notice should name the imported commit"


def test_nested_divisions_keep_their_subgroup(plugin_dir: Path) -> None:
    """Upstream nests some personas one level deeper; the layout is preserved."""
    unity = _catalog(plugin_dir).get("unity-architect")
    assert unity is not None
    assert (unity.division, unity.group) == ("game-development", "unity")


def test_frontmatter_parsing_handles_what_upstream_actually_writes(plugin_dir: Path) -> None:
    parse = _module(plugin_dir, "catalog.py").parse_frontmatter
    data, body = parse(
        "---\n"
        'name: "Quoted Name"\n'
        "color: '#000000'\n"
        "tools:\n"
        "  - Read\n"
        "  - Write\n"
        "---\n"
        "# Body\n"
    )
    assert data["name"] == "Quoted Name"
    assert data["color"] == "#000000"
    assert data["tools"] == ["Read", "Write"]
    assert body == "# Body"
    # No fence at all: the whole file is the body, and nothing raises.
    assert parse("# Just a document")[0] == {}


def test_persona_bodies_are_only_read_when_asked_for(plugin_dir: Path) -> None:
    """Indexing must not pull 4.5 MiB of prompts into memory.

    `summary()` is what listing, searching and the panels use, so a prompt body
    leaking into it would make every roster listing enormous.
    """
    catalog = _catalog(plugin_dir)
    summary = catalog.get("engineering-frontend-developer").summary()
    assert "prompt" not in summary
    brief = catalog.brief("engineering-frontend-developer", 200)
    assert brief["prompt"].startswith("#")
    assert brief["truncated"] is True
    assert brief["prompt_chars"] > len(brief["prompt"])


# ---------------------------------------------------------------------------
# Search: whole words, and a bias towards covering the whole question
# ---------------------------------------------------------------------------


def test_search_matches_whole_words_not_substrings(plugin_dir: Path) -> None:
    """The reason MIN_TOKEN_LENGTH and word sets exist.

    With substring matching, "my" hits "Multiplayer" and "me" hits
    "Remediation", and a question about a slow React page comes back with a game
    audio engineer at the top.
    """
    ranked = _catalog(plugin_dir).search("help me fix slow page load in my react app", limit=5)
    assert ranked, "a plain question should still match somebody"
    assert "Game Audio Engineer" not in _names([agent.summary() for agent, _ in ranked])


def test_search_prefers_the_persona_covering_the_whole_query(plugin_dir: Path) -> None:
    """"react performance" is the person who does both, not the one titled Performance."""
    top = _catalog(plugin_dir).search("react performance", limit=1)[0][0]
    assert top.name == "Frontend Developer"


def test_search_drops_words_that_describe_the_whole_roster(plugin_dir: Path) -> None:
    catalog = _catalog(plugin_dir)
    # "engineering" is carried by a quarter of the roster, "kubernetes" is not.
    assert catalog.query_words("kubernetes engineering") == ["kubernetes"]
    # Unless that is all there was to go on.
    assert catalog.query_words("engineering") == ["engineering"]


def test_exact_name_or_slug_wins_outright(plugin_dir: Path) -> None:
    catalog = _catalog(plugin_dir)
    assert catalog.search("tiktok strategist", limit=1)[0][0].slug == "marketing-tiktok-strategist"
    assert catalog.get("TikTok Strategist").slug == "marketing-tiktok-strategist"
    # A slug without its division prefix still resolves, which is how a reader
    # types it: "frontend-developer", not "engineering-frontend-developer".
    assert catalog.get("frontend-developer").slug == "engineering-frontend-developer"
    assert catalog.get("no-such-persona") is None


def test_a_panel_spread_does_not_stack_one_division(plugin_dir: Path) -> None:
    picked = _catalog(plugin_dir).spread("launch a mobile game with paid user acquisition", 4)
    assert len(picked) == 4
    assert len({agent.division for agent in picked}) == 4


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_manifest_records_where_the_catalog_came_from(plugin_dir: Path) -> None:
    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin_dir.name
    assert manifest["metadata"]["upstream"].endswith("agency-agents")
    assert manifest["metadata"]["license"] == "MIT"


def test_every_documented_tool_registers(bundle: Any) -> None:
    assert set(bundle.tools.tools) == EXPECTED_TOOLS


def test_only_the_tool_that_spends_money_is_consequential(bundle: Any) -> None:
    """Which tools have side effects is a claim about this plugin.

    Three of the four read files off disk; the approval gate should not be asked
    about those, and must be asked about the one that leaves the process.
    """
    flags = {name: spec.consequential for name, spec in bundle.tools.tools.items()}
    assert flags == {
        "agency_agents_roster_tool": False,
        "agency_agents_brief_tool": False,
        "agency_agents_panel_tool": False,
        "agency_agents_consult_tool": True,
    }


def test_subagents_follow_the_configured_divisions(bundle: Any) -> None:
    """The specialist set is configuration, so it is derived, never hard-coded."""
    configured = [str(item).lower() for item in bundle.plugin.config["roster"]["subagent_divisions"]]
    registered = set(bundle.subagents.list_agents())

    assert ROUTER in registered
    for division in configured:
        name = f"agency_{division.replace('-', '_')}_specialist"
        assert name in registered, f"{division} is configured but registered nothing"
        profile = bundle.subagents.get_profile(name)
        assert profile["domain"] == division
        # Capabilities are the division's own personas: what a task looks like.
        assert profile["capabilities"], f"{name} advertises nothing for the planner to match"

    # A division nobody asked for stays searchable but unroutable.
    assert "academic" not in configured
    assert "agency_academic_specialist" not in registered


def test_commands_and_endpoints_register(bundle: Any) -> None:
    declared = {entry["command"] for entry in bundle.plugin.config["commands"]}
    assert declared == EXPECTED_COMMANDS

    routes = {route.endpoint for route in bundle.web.list_for(bundle.plugin.name)}
    # "ping" comes from the decorator, the rest from register_web_apis.
    assert {"ping", "stats", "divisions", "agents", "agent", "consult", "export", "recent"} <= routes


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_roster_tool_searches_and_lists(bundle: Any) -> None:
    searched = _run(bundle.tools.get("agency_agents_roster_tool"), {"query": "tiktok", "limit": 3})
    assert searched["status"] == "success"
    assert searched["matches"][0]["slug"] == "marketing-tiktok-strategist"
    assert searched["total_agents"] == EXPECTED_AGENTS

    listed = _run(bundle.tools.get("agency_agents_roster_tool"), {"query": "", "limit": 5})
    assert len(listed["matches"]) == 5
    assert len(listed["divisions"]) == EXPECTED_DIVISIONS


def test_roster_tool_rejects_a_division_that_does_not_exist(bundle: Any) -> None:
    result = _run(bundle.tools.get("agency_agents_roster_tool"), {"query": "x", "division": "accounting"})
    assert result["error_code"] == "unknown_division"
    # The message names the real ones rather than leaving the caller guessing.
    assert "engineering" in result["report"]


def test_brief_tool_takes_a_slug_a_name_or_a_description(bundle: Any) -> None:
    tool = bundle.tools.get("agency_agents_brief_tool")

    by_slug = _run(tool, {"agent": "design-whimsy-injector"})
    assert by_slug["agent"]["name"] == "Whimsy Injector"
    assert by_slug["matched_by"] == "slug"

    by_name = _run(tool, {"agent": "Whimsy Injector"})
    assert by_name["agent"]["slug"] == "design-whimsy-injector"

    by_query = _run(tool, {"query": "penetration test our web app"})
    assert by_query["matched_by"] == "query"
    assert by_query["agent"]["slug"] == "security-penetration-tester"

    assert _run(tool, {"agent": "chief-vibes-officer"})["error_code"] == "agent_not_found"


def test_brief_tool_splits_a_command_line_into_agent_and_rest(bundle: Any) -> None:
    """`/agency-brief frontend-developer` arrives as one string of text."""
    result = _run(bundle.tools.get("agency_agents_brief_tool"), {"text": "/agency-brief frontend-developer please"})
    assert result["agent"]["slug"] == "engineering-frontend-developer"


def test_panel_tool_respects_each_modes_own_limits(bundle: Any) -> None:
    tool = bundle.tools.get("agency_agents_panel_tool")

    dialogue = _run(tool, {"task": "should we rewrite the checkout flow", "mode": "dialogue", "size": 5})
    assert dialogue["mode"] == "dialogue"
    # The host's compiler requires exactly two participants, so a bigger size is
    # clamped rather than producing a node that will not compile.
    assert len(dialogue["panel"]) == DIALOGUE_PARTICIPANTS
    assert dialogue["suggested_node"]["config"]["participants"] == dialogue["voters"]

    vote = _run(tool, {"task": "ship the redesign on friday", "mode": "vote", "size": 99})
    assert len(vote["panel"]) <= MAX_PANEL
    assert vote["suggested_node"]["type"] == "vote"
    assert vote["suggested_node"]["config"]["on_voter_error"] == "abstain"

    consensus = _run(tool, {"task": "review this migration plan", "mode": "consensus", "size": 3})
    assert consensus["suggested_node"]["config"]["question"]
    assert consensus["suggested_node"]["config"]["mode"] == "refute"

    assert _run(tool, {"task": "x", "mode": "brainstorm"})["error_code"] == "unknown_mode"
    assert _run(tool, {"task": ""})["error_code"] == "missing_task"


def test_panel_tool_says_when_a_pick_is_not_routable(bundle: Any) -> None:
    """A persona is only routable when its division is registered as a subagent.

    Returning a panel the compiler would reject, without saying so, is the
    failure this guards against.
    """
    result = _run(
        bundle.tools.get("agency_agents_panel_tool"),
        {"task": "write a grant proposal about migration history", "mode": "vote", "size": 3},
    )
    configured = {str(item) for item in bundle.plugin.config["roster"]["subagent_divisions"]}
    for entry in result["panel"]:
        expected = f"agency_{entry['division'].replace('-', '_')}_specialist" if entry["division"] in configured else ""
        assert entry["subagent"] == expected
    assert result["routable"] is (len(result["voters"]) >= MIN_VOTE_VOTERS)
    if not result["routable"]:
        assert "roster.subagent_divisions" in result["report"]


def test_consult_tool_resolves_a_persona_without_calling_out(bundle: Any) -> None:
    result = _run(
        bundle.tools.get("agency_agents_consult_tool"),
        {"question": "our LCP is 4s on mobile, where do I start", "dry_run": True},
    )
    assert result["dry_run"] is True
    assert result["agent"]["slug"]
    assert result["resolved"]["api_key_present"] is False
    # The persona itself is what gets sent, and it is bounded.
    # The configured budget covers the whole system prompt, preamble included.
    assert 0 < result["would_send"]["system_chars"] <= bundle.plugin.config["roster"]["persona_max_chars"]
    # The resolved config reports whether a key exists, never the key itself.
    assert "api_key" not in result["resolved"]


def test_consult_tool_refuses_a_live_call_with_no_provider(bundle: Any) -> None:
    result = _run(
        bundle.tools.get("agency_agents_consult_tool"),
        {"agent": "engineering-sre", "question": "what should I alert on"},
    )
    assert result["error_code"] == "llm_unconfigured"
    # The advice is actionable: there is still something useful to ask for.
    assert "dry_run" in result["report"]


def test_consult_tool_needs_a_question(bundle: Any) -> None:
    assert _run(bundle.tools.get("agency_agents_consult_tool"), {"agent": "engineering-sre"})["error_code"] == (
        "missing_question"
    )


def test_consult_timeout_outlives_the_request_it_wraps(bundle: Any) -> None:
    tool = bundle.tools.get("agency_agents_consult_tool")
    resolved = _run(tool, {"question": "hi", "dry_run": True})["resolved"]["timeout_sec"]
    # The schema default is 0, meaning "keep the provider's timeout", and must
    # never reach the tool as an immediate timeout.
    assert resolved > 0
    # Longer than the request, so the request times out first and comes back as
    # a clean provider_error; and longer than the host's own default, which
    # would otherwise cancel a legitimate call.
    assert tool.timeout_sec > resolved
    assert tool.timeout_sec > HOST_DEFAULT_TOOL_TIMEOUT
    assert type(tool)(runtime_context={"llm_config": SimpleNamespace(timeout_sec=120.0)}).timeout_sec > 120


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_before_route_answers_help_without_the_agent(bundle: Any) -> None:
    context = _hook(bundle, "before_route", {"message": {"text": "/agency-help"}})
    assert context["route_outcome"]["action"] == "respond"
    assert "/agency-find" in context["route_outcome"]["response"]
    assert str(EXPECTED_AGENTS) in context["route_outcome"]["response"]


def test_before_route_hints_instead_of_hard_routing(bundle: Any) -> None:
    context = _hook(bundle, "before_route", {"message": {"text": "/agency draft a launch plan"}})
    assert "route_outcome" not in context
    assert context["router_hints"]["preferred_subagent"] == ROUTER
    assert context["router_hints"]["fallback_tool"] == "agency_agents_roster_tool"


def test_before_route_recognises_a_persona_named_in_prose(bundle: Any) -> None:
    """The upstream idiom: "activate Frontend Developer mode"."""
    context = _hook(
        bundle,
        "before_route",
        {"message": {"text": "Activate Frontend Developer mode and review this component"}},
    )
    assert context["router_hints"]["preferred_agent"] == "engineering-frontend-developer"
    assert context["router_hints"]["preferred_domain"] == "engineering"


def test_summon_detection_does_not_fire_on_ordinary_sentences(plugin_dir: Path) -> None:
    """"as" is too common to trust; only a real persona name produces a hint."""
    hooks = _module(plugin_dir, "hooks.py")
    assert hooks.summoned_agent("as soon as possible, ship the build") is None
    # "developer" is the tail of a dozen slugs, so it names none of them.
    assert hooks.summoned_agent("as a developer, review this") is None
    assert hooks.summoned_agent("") is None
    # One word is enough when exactly one persona answers to it.
    assert hooks.summoned_agent("channel SRE and tell me what to alert on").slug == "engineering-sre"
    # And a trailing noun the reader added is not part of the name.
    assert hooks.summoned_agent("act as the TikTok Strategist persona").slug == "marketing-tiktok-strategist"


def test_before_node_execute_backfills_the_actor(bundle: Any) -> None:
    context = _hook(
        bundle,
        "before_node_execute",
        {
            "node": {"runtime_config": {"target": "agency_agents_consult_tool"}},
            "payload": {},
            "task": {"actor_id": "alice"},
        },
    )
    assert context["payload"]["actor_id"] == "alice"

    other = _hook(
        bundle,
        "before_node_execute",
        {"node": {"runtime_config": {"target": "some_other_tool"}}, "payload": {}, "task": {"actor_id": "alice"}},
    )
    assert other["payload"] == {}


def test_after_tool_call_attributes_the_persona(bundle: Any) -> None:
    result = asyncio.run(
        bundle.executor.execute("agency_agents_brief_tool", {"agent": "design-whimsy-injector"})
    )
    assert result["plugin"] == "agency-agents-plugin"
    assert result["persona_source"]["slug"] == "design-whimsy-injector"
    assert result["persona_source"]["upstream"] == "msitarzewski/agency-agents"


def test_after_tool_call_leaves_other_plugins_results_alone(bundle: Any) -> None:
    context = _hook(bundle, "after_tool_call", {"tool": "some_other_tool", "result": {"status": "success"}})
    assert context["result"] == {"status": "success"}


# ---------------------------------------------------------------------------
# SubAgents
# ---------------------------------------------------------------------------


def test_router_picks_a_persona_and_hands_back_its_brief(bundle: Any) -> None:
    """With no provider configured the consult step fails, and that is reported.

    Falling back to the brief matters: the persona is still useful, and the
    reason is named rather than swallowed.
    """
    router = bundle.subagents.get(ROUTER)
    result = asyncio.run(
        router.run({"query": "review this react component for accessibility", "actor_id": "alice"}, {}, {})
    )
    assert result["status"] == "success"
    assert result["consulted"] is False
    assert result["fallback_reason"] == "llm_unconfigured"
    assert result["agent"]["prompt"], "the brief should carry the persona prompt"
    assert result["match_score"] >= MIN_CONFIDENT_SCORE


def test_router_asks_rather_than_guessing_on_a_weak_match(bundle: Any) -> None:
    """No persona's frontmatter mentions Kubernetes, so nothing really matches.

    Answering anyway means a finance tracker explaining pod evictions with a
    straight face, which is worse than handing the choice back.
    """
    result = asyncio.run(
        bundle.subagents.get(ROUTER).run({"query": "our k8s cluster keeps evicting pods"}, {}, {})
    )
    assert result["needs_selection"] is True
    assert "agent" not in result
    assert len(result["divisions"]) == EXPECTED_DIVISIONS
    assert "agency_agents_roster_tool" in result["report"]


def test_a_specialist_answers_from_its_own_division_either_way(bundle: Any) -> None:
    """A division was chosen by the planner, so the specialist stays in it.

    Even a query its ten personas barely match is answered from inside the
    division - with `low_confidence` set, so the caller knows how firm the pick
    underneath that routing decision was.
    """
    specialist = bundle.subagents.get("agency_design_specialist")
    clear = asyncio.run(specialist.run({"query": "our brand feels inconsistent across screens"}, {}, {}))
    assert clear["domain"] == "design"
    assert clear["agent"]["division"] == "design"
    assert clear["low_confidence"] is False

    vague = asyncio.run(specialist.run({"query": "zzzz qqqq"}, {}, {}))
    assert vague["agent"]["division"] == "design"
    assert vague["low_confidence"] is True


def test_a_specialist_reports_a_task_with_nothing_to_match(bundle: Any) -> None:
    result = asyncio.run(bundle.subagents.get("agency_testing_specialist").run({}, {}, {}))
    assert result["status"] == "error"
    assert result["error_code"] == "missing_query"


def test_subagents_answer_a_vote_node(bundle: Any) -> None:
    """A vote must produce a ballot even when the persona cannot be consulted.

    The runtime counts an unparseable answer as an abstention anyway; saying so
    explicitly, with a reason, is the difference between a quiet default and a
    result an operator can act on.
    """
    spec = {"proposal": "store the api credentials in the frontend bundle", "choices": ["approve", "reject"]}
    result = asyncio.run(bundle.subagents.get("agency_security_specialist").run({"_vote_spec": spec}, {}, {}))
    assert result["choice"] == "abstain"
    assert result["reason"]
    # It still says which persona would have voted, so the trace is not empty.
    assert result["agent"]["division"] == "security"


def test_vote_answers_are_read_from_the_first_line_only(plugin_dir: Path) -> None:
    """A persona that argues its case mentions both words; position decides."""
    parse = _module(plugin_dir, "subagents.py").parse_vote
    assert parse("Approve. Rejecting this would cost us the quarter.")[0] == "approve"
    assert parse("Reject - I would approve a version with tests.")[0] == "reject"
    assert parse("赞成，理由是回滚成本低。")[0] == "approve"
    assert parse("")[0] == "abstain"
    # No verdict at the top is an abstention, not a guess.
    assert parse("It depends on how much traffic the endpoint sees.")[0] == "abstain"


def test_division_capabilities_drop_the_redundant_prefix(plugin_dir: Path) -> None:
    """`domain="engineering"` already says engineering; the slugs need not."""
    capabilities = _module(plugin_dir, "subagents.py").division_capabilities("engineering")
    assert capabilities
    assert all(not item.startswith("engineering-") for item in capabilities)
    assert "sre" in capabilities


# ---------------------------------------------------------------------------
# Web endpoints
# ---------------------------------------------------------------------------


def test_panel_endpoints_return_what_the_widgets_declare(host: Any, bundle: Any) -> None:
    stats = _body(_call(host, bundle, "stats", username="alice"))
    assert stats["agents"] == EXPECTED_AGENTS
    assert stats["divisions"] == EXPECTED_DIVISIONS
    assert stats["specialists"] == len(bundle.plugin.config["roster"]["subagent_divisions"])

    divisions = _body(_call(host, bundle, "divisions", username="alice"))
    assert len(divisions) == EXPECTED_DIVISIONS
    assert set(divisions[0]) >= {"division", "agents", "subagent"}
    # An unregistered division is marked, not omitted.
    assert [row for row in divisions if row["subagent"] == "-"]

    points = _body(_call(host, bundle, "distribution", username="alice"))
    assert set(points[0]) == {"division", "agents"}
    assert sum(point["agents"] for point in points) == EXPECTED_AGENTS


def test_agents_endpoint_searches_and_filters(host: Any, bundle: Any) -> None:
    hits = _body(_call(host, bundle, "agents", username="alice", query={"q": "shader", "limit": 3}))
    assert hits[0]["division"] == "game-development"

    scoped = _body(_call(host, bundle, "agents", username="alice", query={"division": "healthcare"}))
    assert {row["division"] for row in scoped} == {"healthcare"}

    missing = _call(host, bundle, "agents", username="alice", query={"division": "accounting"})
    assert _body(missing)["message"] == "unknown_division"


def test_agent_endpoint_serves_one_persona(host: Any, bundle: Any) -> None:
    body = _body(_call(host, bundle, "agent", username="alice", query={"slug": "design-whimsy-injector"}))
    assert body["agent"]["name"] == "Whimsy Injector"
    assert body["subagent"] == "agency_design_specialist"

    assert _body(_call(host, bundle, "agent", username="alice"))["message"] == "missing_slug"
    assert _body(_call(host, bundle, "agent", username="alice", query={"slug": "nope"}))["message"] == (
        "agent_not_found"
    )


def test_export_serves_the_persona_file_itself(host: Any, bundle: Any, plugin_dir: Path) -> None:
    served = _call(host, bundle, "export", username="alice", query={"slug": "design-whimsy-injector"})
    assert served.media_type == "text/markdown"
    on_disk = (plugin_dir / "catalog" / "design" / "design-whimsy-injector.md").read_bytes()
    assert bytes(served.body) == on_disk

    assert _body(_call(host, bundle, "export", username="alice"))["message"] == "missing_slug"

    whole = _call(host, bundle, "export/division", username="alice", query={"division": "healthcare"})
    payload = json.loads(bytes(whole.body))
    assert payload["licence"] == "MIT"
    assert len(payload["agents"]) == len([row for row in payload["agents"] if row["division"] == "healthcare"])


def test_consult_endpoint_goes_through_the_executor(host: Any, bundle: Any) -> None:
    answered = _body(
        _call(
            host,
            bundle,
            "consult",
            "POST",
            username="alice",
            body={"agent": "engineering-sre", "question": "what should I alert on", "dry_run": True},
        )
    )
    assert answered["result"]["agent"]["slug"] == "engineering-sre"
    # The hook fired on the way out, which only happens through the executor.
    assert answered["result"]["plugin"] == "agency-agents-plugin"

    assert _body(_call(host, bundle, "consult", "POST", username="alice", body={}))["message"] == "missing_question"
    # A tool-level refusal reaches the page as the tool's own code, not a 500.
    unconfigured = _call(host, bundle, "consult", "POST", username="alice", body={"question": "live please"})
    assert _body(unconfigured)["message"] == "llm_unconfigured"


def _history(bundle: Any, plugin_dir: Path) -> Any:
    """This plugin's history store, on whichever storage the host injected.

    Taken off the consult tool because that is the only object holding it: a
    real consultation needs a provider, and `bundle.storage` is only populated
    by the stand-in host.
    """
    storage = bundle.tools.get("agency_agents_consult_tool").storage
    return _module(plugin_dir, "store.py").ConsultationStore(storage)


def test_history_is_per_user_and_deletable(host: Any, bundle: Any, plugin_dir: Path) -> None:
    """The store is exercised directly: a real consultation needs a provider."""
    store = _history(bundle, plugin_dir)
    agent = {"slug": "engineering-sre", "name": "SRE", "division": "engineering", "emoji": "🚨"}
    record = store.add("alice", agent, "what should I alert on", "Error budget burn rate.")
    assert store.backend == "file", "no Redis here, so the documented fallback must engage"

    rows = _body(_call(host, bundle, "recent", username="alice"))
    assert [row["id"] for row in rows] == [record["id"]]
    assert rows[0]["division"] == "engineering"
    # Another user sees none of it.
    assert _body(_call(host, bundle, "recent", username="bob")) == []

    tally = _body(_call(host, bundle, "favourites", username="alice"))
    assert tally[0]["calls"] == 1

    # Nor can they delete it by guessing the id.
    denied = _call(host, bundle, "history/delete", "DELETE", username="bob", query={"id": record["id"]})
    assert _body(denied)["message"] == "record_not_found"

    removed = _body(_call(host, bundle, "history/delete", "DELETE", username="alice", query={"id": record["id"]}))
    assert removed["id"] == record["id"]
    assert _body(_call(host, bundle, "recent", username="alice")) == []
    assert _body(_call(host, bundle, "history/delete", "DELETE", username="alice"))["message"] == "missing_id"


def test_history_is_trimmed_to_the_retention_setting(bundle: Any, plugin_dir: Path) -> None:
    store = _history(bundle, plugin_dir)
    agent = {"slug": "engineering-sre", "name": "SRE", "division": "engineering"}
    for index in range(5):
        store.add("carol", agent, f"question {index}", "answer")
    dropped = store.prune("carol", 2)
    assert len(dropped) == 3
    # Newest first, so pruning keeps the most recent questions.
    assert [row["question"] for row in store.all_for("carol")] == ["question 4", "question 3"]


def test_refresh_reindexes_without_a_restart(host: Any, bundle: Any) -> None:
    body = _body(_call(host, bundle, "actions/refresh", "POST", username="alice"))
    assert body["agents"] == EXPECTED_AGENTS
    assert body["divisions"] == EXPECTED_DIVISIONS


def test_ping_reports_the_caller_the_host_authenticated(host: Any, bundle: Any) -> None:
    body = _body(_call(host, bundle, "ping", username="alice"))
    assert body["user"] == "alice"
    assert body["agents"] == EXPECTED_AGENTS


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_can_say_everything_in_both_languages(plugin_dir: Path) -> None:
    """A key present in one table and missing from the other shows up as English
    text in a Chinese page, or as a raw code like `llm_unconfigured`."""
    source = (plugin_dir / "pages" / "roster" / "app.js").read_text(encoding="utf-8")
    tables = {
        locale: set(re.findall(r"^\s{6}(\w+):", block, re.MULTILINE))
        for locale, block in re.findall(r"\n\s{4}(en|zh): \{\n(.*?)\n\s{4}\},", source, re.DOTALL)
    }
    assert set(tables) == {"en", "zh"}
    assert tables["en"] == tables["zh"]


def test_every_error_code_the_backend_returns_has_page_text(plugin_dir: Path) -> None:
    """A code the page cannot translate reaches the reader as a bare identifier."""
    web = (plugin_dir / "web.py").read_text(encoding="utf-8")
    codes = set(re.findall(r"error_response\(\s*\"([a-z_]+)\"", web))
    page = (plugin_dir / "pages" / "roster" / "app.js").read_text(encoding="utf-8")
    untranslated = {code for code in codes if f"{code}:" not in page}
    assert untranslated == set(), f"add page text for: {', '.join(sorted(untranslated))}"
