# Where these files come from

Everything under `catalog/` except this file is vendored, unmodified, from
[msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents).

| | |
| --- | --- |
| Upstream | https://github.com/msitarzewski/agency-agents |
| Commit | `053ddbbf392a1688fc7043d81529f47ef2cf86c8` |
| Committed | 2026-09-21 |
| Imported | 2026-09-25 |
| Licence | MIT - the upstream copy is kept beside this file as `LICENSE` |
| Agents | 279 markdown personas across 18 divisions |

## What was imported

- every `<division>/**/*.md` whose first line is `---`, i.e. every file carrying
  agent frontmatter, with the upstream directory layout preserved (so
  `game-development/unity/unity-architect.md` still sits under its subgroup)
- `divisions.json`, the upstream source of truth for division labels, icons and
  brand colours

## What was left out, and why

| Upstream path | Why |
| --- | --- |
| `scripts/` | shell installers that copy personas into `~/.claude/agents`, `.cursor/rules` and so on. This host loads personas from `catalog/` directly, so there is nothing to install. |
| `integrations/` | generated output of `scripts/convert.sh` for other tools' file formats. |
| `strategy/`, `examples/` | playbooks, runbooks and worked examples with no agent frontmatter. Nothing in the plugin can route to them. |
| `.github/`, `README.md`, `CONTRIBUTING*.md`, `tools.json` | upstream project machinery about other tools' install contracts. |

## Refreshing the catalog

The frontmatter contract this plugin parses is `name`, `description`, `color`,
`emoji`, `vibe` and an optional `tools` list - the same five-or-six keys every
upstream file carries. A refresh is therefore a copy:

```bash
git clone --depth 1 https://github.com/msitarzewski/agency-agents.git /tmp/agency-agents
uv run python scripts/import_catalog.py /tmp/agency-agents --dry-run
uv run python scripts/import_catalog.py /tmp/agency-agents
uv run pytest
```

The script walks the divisions named in the upstream `divisions.json`, keeps only
frontmatter files, and rewrites the commit, date and counts in the table above.
It never touches the plugin's own code. The plugin's tests pin the expected
persona count, so an import that quietly loses half the roster fails there.
