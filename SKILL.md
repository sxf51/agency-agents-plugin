---
name: agency-agents-plugin
description: A roster of 279 specialist AI personas - engineering, design, marketing, security, game development and more - that can be searched, read as prompts, consulted directly, or routed to as subagents. Use it when a task would be done better by a domain specialist than by a generalist.
---

## Description

A port of the [agency-agents](https://github.com/msitarzewski/agency-agents)
collection: 279 markdown personas across 18 divisions, each one a specialist with
its own identity, priorities, critical rules and deliverables. The files are
vendored unchanged under the plugin's `catalog/` directory and treated as data -
searched by keyword, read as prompts, or used as the system prompt for one
question.

Three of the four tools are offline: finding the right specialist, reading its
prompt and assembling a panel are all catalog lookups. Only consulting a persona
sends a model request.

## Capabilities

- Commands: `/agency`, `/agency-find`, `/agency-brief`, `/agency-panel`, `/agency-ask`
- Hook-only command: `/agency-help` is answered by a `before_route` hook, without reaching the agent
- Tools: `agency_agents_roster_tool` (search), `agency_agents_brief_tool` (one persona's full prompt), `agency_agents_panel_tool` (assemble a vote/dialogue/consensus panel), `agency_agents_consult_tool` (ask a persona; sends one model request)
- SubAgents: `agency_agents_router` routes across the whole roster; one `agency_<division>_specialist` per division listed in `roster.subagent_divisions`. All of them also serve as `vote` node voters.
- Hooks: `before_route` (help plus routing hints, including "activate Frontend Developer mode"), `before_node_execute` (caller identity), `after_tool_call` (persona attribution)
- Web: an Agency Roster page, a panel of stat tiles, tables, a distribution chart and a consult form, and thirteen endpoints under this plugin's own namespace
- Storage: per-user consultation history, on the host's storage service, with a JSON-file fallback when Redis is down

## Usage Hints

- `/agency-find <keywords>` searches the roster. `/agency-find tiktok` finds the TikTok Strategist; `/agency-find react performance` finds the Frontend Developer. Add a division to narrow it.
- `/agency-brief <agent>` returns one persona in full, ready to paste into a system prompt. Takes a slug (`engineering-backend-architect`) or a display name (`Backend Architect`).
- `/agency-ask <agent> <question>` answers in that persona's voice. The agent may be omitted - `/agency-ask how do I cut my LCP` picks the best match itself.
- `/agency-panel <task>` picks complementary personas, one per division, and returns the vote, dialogue or consensus node configuration that would run them.
- `/agency <task>` goes through the planner to `agency_agents_router`, which selects a specialist and either answers as it or hands back its brief, depending on `consult.auto_run`.
- "Activate Frontend Developer mode and review this component" works too: a `before_route` hook recognises a persona named that way and hints the router towards it.
- A division is only routable when it is listed in `roster.subagent_divisions`. Everything in the roster stays searchable either way.
- Open the Agency Roster page to browse divisions, read a persona, ask it something, or download its markdown.
