# Agency Roster · agency-agents-plugin

> 中文：[README.md](README.md)

A port of [agency-agents](https://github.com/msitarzewski/agency-agents) - 279 specialist AI
personas - into a plugin for this host. Upstream is a file-copying affair: `scripts/install.sh`
drops the markdown into `~/.claude/agents/` and you summon a persona by typing "activate Frontend
Developer mode". This plugin copies nothing. The personas stay in `catalog/` and are treated as
**data**: searchable, readable as prompts, consultable directly, and routable as subagents.

No third-party dependencies. The upstream files are vendored unchanged; the MIT licence and the
imported commit are recorded in [catalog/UPSTREAM.md](catalog/UPSTREAM.md).

## Thirty seconds

```text
/agency-help                             what is in here
/agency-find react performance           search the roster
/agency-brief frontend-developer         read that persona's full prompt
/agency-ask frontend-developer why is my LCP 4s
/agency-panel cold start for a new mobile game
/agency rework our checkout flow         let the planner route it
```

"Activate Frontend Developer mode and review this component" works too: a `before_route` hook knows
the upstream idiom and turns it into a routing hint.

## What was ported, and how it maps

| Upstream | Here |
| --- | --- |
| `<division>/*.md` persona files | `catalog/<division>/**/*.md`, unchanged, nested subgroups preserved |
| `divisions.json` | Vendored with them; labels and brand colours are read from it, not copied into code |
| `scripts/install.sh --tool claude-code` | Not needed: the host reads `catalog/` directly, so there is nowhere to install to |
| `scripts/convert.sh` + `integrations/` | Not needed: those convert to other tools' file formats |
| "activate X mode" in the README | Recognised by a `before_route` hook and turned into `router_hints` |
| "assemble your dream team" | `agency_agents_panel_tool`, which emits a ready vote / dialogue / consensus node |
| `strategy/`, `examples/` | Left out: no agent frontmatter, so nothing here could route to them |

## Four tools

| Tool | What it does | Network | consequential |
| --- | --- | --- | --- |
| `agency_agents_roster_tool` | Search the roster by keywords or division | no | no |
| `agency_agents_brief_tool` | Read one persona's full prompt | no | no |
| `agency_agents_panel_tool` | Assemble a panel and emit its node config | no | no |
| `agency_agents_consult_tool` | Answer a question as that persona | **yes** | **yes** |

The first three are disk reads, so the planner can use them freely. Only the fourth spends money,
which is why only it is `consequential`, and only it declares `timeout_sec` - `llm.timeout_sec` plus
five seconds, so the HTTP request times out first and comes back as a clean `provider_error` instead
of being cancelled as a tool timeout.

None of the four declares `allowed_subagents`, and the manifest declares no `tool_access`. That is
deliberate: which specialists exist is a configuration decision (`roster.subagent_divisions`), so a
static allow-list would go stale the first time someone enables a division - and would fail
silently. Narrowing happens on the subagent side, where each agent declares the tools it uses.

## SubAgents: one router, one per division

279 personas do **not** become 279 subagents. That would put 279 competing descriptions in the
planner's routing prompt and make every decision worse. What registers instead:

- `agency_agents_router`, domain `agency-roster`: for "who should look at this", and for requests
  that name a persona outright.
- `agency_<division>_specialist`, one per division in `roster.subagent_divisions`, with the division
  as its domain and its own personas as its capabilities (`backend-architect`, `sre`,
  `frontend-developer`, …) - which is what a task description actually looks like. Six are on by
  default: engineering, design, product, marketing, security, testing.

Choosing the persona is then a catalog lookup inside the chosen agent, not a planner decision, and
it costs nothing.

**A weak match is not answered.** Search is keyword matching over one-sentence descriptions, and no
persona's frontmatter mentions Kubernetes, so "our k8s cluster keeps evicting pods" tops out at a
score of 3. The router then returns candidates and the division list (`needs_selection: true`)
rather than having a finance tracker explain pod evictions with a straight face. A division
specialist behaves differently: the division was already the planner's decision, so it always
answers from inside it and sets `low_confidence` instead.

Every one of them can serve as a `vote` node voter. Given `_vote_spec` it votes in the persona's
voice, and the **first line** of the answer decides approve / reject / abstain - a persona that
explains itself mentions both words further down. With no provider configured it abstains and says
why rather than guessing.

## Configuration

| Key | Effect |
| --- | --- |
| `llm.*` | Which provider, model, temperature and request timeout a consultation uses |
| `roster.subagent_divisions` | Which divisions register as subagents. A longer list is a longer routing prompt |
| `roster.max_results` | Default search result count |
| `roster.brief_max_chars` | How much of a persona prompt a brief carries (it reports when clipped) |
| `roster.persona_max_chars` | Budget for the **whole** system prompt, preamble and language line included |
| `consult.auto_run` | Whether a specialist answers directly or returns the brief |
| `consult.answer_language` | The upstream personas are English; the default follows the question |
| `consult.system_prefix` | Deployment boundaries prepended to the persona, e.g. no invented numbers |
| `consult.keep_last` | Consultations retained per user |
| `panel.mode` / `panel.size` | What `/agency-panel` assembles by default |

The host's compiler is strict about collaboration nodes: a vote wants 2-7 voters, a dialogue
**exactly** two participants, consensus at most seven judges. The panel tool clamps to the chosen
mode, so the node config it hands back is one that compiles.

## Page and panel

"Agency Roster" in the sidebar: search, pick, read the prompt, ask a question, download the
markdown - in English and Chinese, following the dashboard's light/dark theme. The panel is declared
in `plugin.yaml` and rendered by the host, with no frontend code from the plugin: four stat tiles, a
divisions table, a distribution chart, an ask form, recent consultations, most-consulted personas,
runtime state, and a re-index button.

## The roster is cached

Indexing reads only each file's frontmatter, never the 4.5 MiB of prompt bodies - listing, searching
and the panels have no use for them, so only `brief` and a consultation read a whole file. The index
is built once per process; after replacing the files on disk, press "Re-read the catalog"
(`POST actions/refresh`) rather than restarting.

## Refreshing the roster

```bash
git clone --depth 1 https://github.com/msitarzewski/agency-agents.git /tmp/agency-agents
uv run python scripts/import_catalog.py /tmp/agency-agents --dry-run
uv run python scripts/import_catalog.py /tmp/agency-agents
uv run pytest
```

It touches `catalog/` and nothing else. The persona count is pinned in the tests, so an import that
quietly loses half the roster fails there rather than in production. When upstream adds a division,
add it to `roster.subagent_divisions` for its specialist to register.

## Development

```bash
uv run python main.py inspect                            # what it registers
uv run python main.py doctor                             # the generic checks
uv run python main.py call agency_agents_roster_tool '{"query":"tiktok"}'
uv run python main.py call agency_agents_consult_tool '{"question":"LCP 4s","dry_run":true}'
uv run python main.py hook before_route '{"message":{"text":"/agency-help"}}'
uv run python main.py web GET agents '{"q":"shader"}'
uv run python main.py serve                              # the page in a browser
```

Inside the host repository:

```bash
uv run pytest plugins/agency-agents-plugin/tests -q
uv run ruff check plugins/agency-agents-plugin --no-respect-gitignore
uv run bandit -r plugins/agency-agents-plugin -c ../../pyproject.toml
```

`tests/test_plugin_contract.py` and `tests/harness/` come from
[plugin-template](https://github.com/sxf51/plugin-template) unchanged;
`tests/test_plugin_agency_agents.py` is this plugin's own behaviour.

## Licence

Plugin code: MIT. The persona files under `catalog/` belong to their upstream authors and are MIT
too; the licence and imported commit are recorded in [catalog/UPSTREAM.md](catalog/UPSTREAM.md).
