"""The roster index: 279 vendored persona files, read as data.

`catalog/` holds the upstream agency-agents personas unchanged - one markdown
file per agent, YAML frontmatter followed by the prompt itself. This module is
the only place that knows that layout. Everything else (tools, subagents, web
endpoints) asks it questions.

Two deliberate choices:

* **Frontmatter only, until asked.** Building the index reads the first few
  lines of each file, not the 4.5 MiB of prompt bodies behind them. A body is
  read when something actually needs that agent's prompt - `brief()` - so
  listing, searching and the dashboard panels never touch it.
* **Cached per directory, invalidated by the set of files.** The catalog is
  static for the life of a plugin version, so the index is built once per
  process. `invalidate()` drops it, which is what the refresh endpoint calls
  after someone replaces the files on disk.

Like `store.py`, this is not one of the four module names the host imports; it
is loaded by file path from `tools.py` / `web.py` / `subagents.py`. See
`_load_local_module` there for why.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The frontmatter block is a handful of short lines in every upstream file.
# Reading a bounded prefix keeps indexing off the 4.5 MiB of prompt bodies.
FRONTMATTER_MAX_BYTES = 4096
# An upstream persona runs 10-40 KiB. Anything past this is a sign the file is
# not a persona at all, and a model has no use for that much system prompt.
PERSONA_MAX_BYTES = 200_000
DEFAULT_BRIEF_CHARS = 6000
DEFAULT_PERSONA_CHARS = 12000
# Two characters is not a search term. It is also what turns matching into
# nonsense: "my" and "me" appear inside "Multiplayer" and "Remediation", so a
# substring rule would rank a game audio engineer top for "fix my react page".
MIN_TOKEN_LENGTH = 3
# Words a reader types to make a sentence, not to name a speciality.
STOP_WORDS = frozenset(
    {
        "a", "an", "and", "the", "for", "with", "into", "from", "that", "this",
        "who", "how", "what", "why", "when", "can", "you", "your", "our", "its",
        "help", "need", "want", "please", "agent", "agents", "expert", "make",
        "build", "write", "give", "get", "use", "using", "about", "some", "any",
        "are", "was", "has", "have", "does", "should", "would", "could", "will",
        "app", "new", "one", "all", "out", "not", "but", "his", "her", "them",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+")
_SCALAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")

# Score weights. Name and slug matches are what a reader means by "find the
# TikTok one"; description and vibe matches broaden a vague task description.
WEIGHT_EXACT = 100
WEIGHT_NAME = 12
WEIGHT_SLUG = 8
# A query word that is the start of a longer indexed word: "test" for "testing",
# "optimis" for "optimisation". Worth less than the real thing.
WEIGHT_PREFIX = 5
MIN_PREFIX_LENGTH = 4
# A quoted scalar needs both quotes; `catalog/<division>/<group>/<file>.md` needs
# three path parts before the middle one names a subgroup.
MIN_QUOTED_LENGTH = 2
GROUPED_PATH_PARTS = 3
WEIGHT_DESCRIPTION = 3
WEIGHT_DIVISION = 2
WEIGHT_GROUP = 2
# Per query word matched beyond the first. Covering more of what was asked is
# worth as much as carrying one of the words in your name: "react performance"
# means the person who does both, not the one with "performance" in their title.
WEIGHT_COVERAGE = 12
# A word carried by more than this share of the roster describes the collection,
# not any one member of it: "engineer", "specialist", "systems", "expert". They
# are dropped from a query rather than down-weighted, because with 279 personas
# one such word outweighs the word that actually mattered.
COMMON_WORD_SHARE = 0.25


@dataclass(frozen=True)
class Agent:
    """One persona file, as the rest of the plugin sees it."""

    slug: str
    name: str
    division: str
    group: str
    description: str
    emoji: str
    vibe: str
    color: str
    tools: tuple[str, ...]
    relative_path: str
    bytes: int
    # Indexed words, split out by where they came from so a name match can
    # outrank a description match. Whole words, never substrings.
    name_words: frozenset[str] = frozenset()
    slug_words: frozenset[str] = frozenset()
    text_words: frozenset[str] = frozenset()

    def summary(self) -> dict[str, Any]:
        """Return the shape every tool, endpoint and page row uses; no prompt body."""
        return {
            "slug": self.slug,
            "name": self.name,
            "division": self.division,
            "group": self.group,
            "description": self.description,
            "emoji": self.emoji,
            "vibe": self.vibe,
            "color": self.color,
            "declared_tools": list(self.tools),
            "path": self.relative_path,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class Division:
    """A top-level catalog directory, with the upstream presentation metadata."""

    key: str
    label: str
    icon: str
    color: str
    agents: int


def tokenize(text: Any) -> list[str]:
    """Lowercase word tokens, minus the words that match everything."""
    return [
        token
        for token in _TOKEN.findall(str(text or "").lower())
        if len(token) >= MIN_TOKEN_LENGTH and token not in STOP_WORDS
    ]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split an upstream persona file into its frontmatter and its body.

    Deliberately not a YAML parse. The host's loader has PyYAML, but a plugin
    should not need it to read its own data files, and the upstream contract is
    narrow: a `---` fence, then `key: value` scalars, then `- item` lists under
    `tools:`. Anything that does not match that is skipped rather than raised on,
    because one odd file must not take the whole roster down.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return {}, text

    data: dict[str, Any] = {}
    current_list: str = ""
    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current_list:
            data[current_list].append(_unquote(line.lstrip()[2:]))
            continue
        matched = _SCALAR.match(line)
        if matched is None:
            continue
        key, value = matched.group(1).strip(), _unquote(matched.group(2))
        if value:
            data[key] = value
            current_list = ""
            continue
        # `tools:` with nothing after it opens a list; the `- item` lines follow.
        data[key] = []
        current_list = key
    return data, "\n".join(lines[end + 1 :]).strip()


def _has_prefix(token: str, words: frozenset[str]) -> bool:
    """Whether a query word starts a longer indexed word.

    Only from four characters up, and only forwards: `test` reaching `testing`
    is a near miss worth counting, `ai` reaching `aircraft` is not.
    """
    if len(token) < MIN_PREFIX_LENGTH:
        return False
    return any(word.startswith(token) for word in words)


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= MIN_QUOTED_LENGTH and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _split_tools(value: Any) -> tuple[str, ...]:
    """Frontmatter `tools` is a YAML list in some files and CSV in others."""
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


class Catalog:
    """The vendored roster, indexed once per process."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).with_name("catalog")
        self._agents: dict[str, Agent] | None = None
        self._divisions: dict[str, Division] | None = None
        self._words: dict[str, int] | None = None

    # -- state ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the vendored files are where this plugin expects them."""
        return self.root.is_dir() and bool(self.agents)

    def invalidate(self) -> None:
        """Drop the cached index. The refresh endpoint calls this."""
        self._agents = None
        self._divisions = None
        self._words = None

    @property
    def agents(self) -> dict[str, Agent]:
        if self._agents is None:
            self._agents = self._build()
        return self._agents

    def count(self) -> int:
        return len(self.agents)

    # -- reads ---------------------------------------------------------------

    def get(self, slug: str) -> Agent | None:
        """One agent by slug, or by display name, case-insensitively."""
        wanted = str(slug or "").strip().lower().replace(" ", "-")
        if not wanted:
            return None
        found = self.agents.get(wanted)
        if found is not None:
            return found
        return next(
            (
                agent
                for agent in self.agents.values()
                if agent.name.lower() == str(slug).strip().lower() or agent.slug.endswith(f"-{wanted}")
            ),
            None,
        )

    def divisions(self) -> list[Division]:
        """Every division with at least one agent, in upstream order."""
        if self._divisions is None:
            self._divisions = self._build_divisions()
        return list(self._divisions.values())

    def division(self, key: str) -> Division | None:
        if self._divisions is None:
            self._divisions = self._build_divisions()
        return self._divisions.get(str(key or "").strip().lower())

    def in_division(self, key: str) -> list[Agent]:
        wanted = str(key or "").strip().lower()
        return sorted(
            (agent for agent in self.agents.values() if agent.division == wanted),
            key=lambda agent: agent.name.lower(),
        )

    def search(self, query: str = "", division: str = "", limit: int = 10) -> list[tuple[Agent, int]]:
        """Rank the roster against a free-text query.

        Returns `(agent, score)` pairs, best first. An empty query is a listing
        rather than a search, so everything scores 0 and the order is by name -
        which is what the dashboard's browse view wants.
        """
        pool = self.in_division(division) if division else sorted(self.agents.values(), key=lambda a: a.name.lower())
        capped = max(1, min(int(limit or 10), 100))
        tokens = self.query_words(query)
        if not tokens:
            return [(agent, 0) for agent in pool[:capped]]

        wanted = str(query or "").strip().lower()
        scored = [(agent, self._score(agent, tokens, wanted)) for agent in pool]
        ranked = sorted(
            (pair for pair in scored if pair[1] > 0),
            key=lambda pair: (-pair[1], pair[0].name.lower()),
        )
        return ranked[:capped]

    def query_words(self, query: str) -> list[str]:
        """Return the words of a query that still say something about this roster.

        A word carried by a quarter of the personas - `engineer`, `specialist`,
        `systems` - describes the collection rather than any member of it, so it
        is dropped. If that empties the query, the words are kept: a search for
        "engineer" should still return engineers.
        """
        tokens = list(dict.fromkeys(tokenize(query)))
        if not tokens:
            return []
        ceiling = max(2, int(len(self.agents) * COMMON_WORD_SHARE))
        frequency = self._frequency()
        narrowing = [token for token in tokens if frequency.get(token, 0) <= ceiling]
        return narrowing or tokens

    def best(self, query: str = "", division: str = "") -> Agent | None:
        """Return the single best match, which is what a subagent routes to."""
        ranked = self.search(query, division=division, limit=1)
        if ranked:
            return ranked[0][0]
        # Nothing matched the words, but the caller still needs someone from
        # that division; the roster is more useful than a refusal.
        pool = self.in_division(division) if division else []
        return pool[0] if pool else None

    def spread(self, query: str = "", size: int = 3) -> list[Agent]:
        """Pick complementary agents: the best of each of several divisions.

        A panel of three engineers is not a panel. One agent per division until
        the divisions run out, then the next-best regardless of division.
        """
        wanted = max(1, min(int(size or 3), 12))
        ranked = self.search(query, limit=wanted * 6)
        picked: list[Agent] = []
        seen: set[str] = set()
        for agent, _ in ranked:
            if agent.division in seen:
                continue
            picked.append(agent)
            seen.add(agent.division)
            if len(picked) == wanted:
                return picked
        for agent, _ in ranked:
            if agent in picked:
                continue
            picked.append(agent)
            if len(picked) == wanted:
                break
        return picked

    def brief(self, slug: str, max_chars: int = DEFAULT_BRIEF_CHARS) -> dict[str, Any] | None:
        """Return an agent's summary plus its prompt body, bounded.

        The body is read here and nowhere else. `truncated` is reported rather
        than hidden: a caller pasting a clipped persona into a system prompt
        should know it was clipped.
        """
        agent = self.get(slug)
        if agent is None:
            return None
        body = self.persona(agent)
        limit = max(200, int(max_chars or DEFAULT_BRIEF_CHARS))
        return {
            **agent.summary(),
            "prompt": body[:limit],
            "prompt_chars": len(body),
            "truncated": len(body) > limit,
        }

    def persona(self, agent: Agent) -> str:
        """Read the prompt body of one agent, as the model should receive it."""
        path = self.root / agent.relative_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:PERSONA_MAX_BYTES]
        except OSError:
            return ""
        _, body = parse_frontmatter(text)
        return body

    # -- index construction ---------------------------------------------------

    def _build(self) -> dict[str, Agent]:
        if not self.root.is_dir():
            return {}
        agents: dict[str, Agent] = {}
        for path in sorted(self.root.rglob("*.md")):
            if path.parent == self.root:
                # UPSTREAM.md and friends sit at the catalog root. A persona
                # always lives under a division directory.
                continue
            agent = self._read(path)
            if agent is not None:
                agents[agent.slug] = agent
        return agents

    def _read(self, path: Path) -> Agent | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(FRONTMATTER_MAX_BYTES)
            size = path.stat().st_size
        except OSError:
            return None
        data, _ = parse_frontmatter(head)
        name = str(data.get("name") or "").strip()
        if not name:
            # No frontmatter name means it is documentation, not a persona.
            return None
        relative = path.relative_to(self.root)
        parts = relative.parts
        division = parts[0]
        group = parts[1] if len(parts) >= GROUPED_PATH_PARTS else ""
        description = str(data.get("description") or "").strip()
        vibe = str(data.get("vibe") or "").strip()
        return Agent(
            slug=path.stem.lower(),
            name=name,
            division=division,
            group=group,
            description=description,
            emoji=str(data.get("emoji") or "").strip(),
            vibe=vibe,
            color=str(data.get("color") or "").strip(),
            tools=_split_tools(data.get("tools")),
            relative_path=relative.as_posix(),
            bytes=size,
            name_words=frozenset(tokenize(name)),
            slug_words=frozenset(tokenize(path.stem.replace("-", " "))),
            text_words=frozenset(tokenize(f"{description} {vibe}")),
        )

    def _frequency(self) -> dict[str, int]:
        """How many personas carry each indexed word. Built once with the index."""
        if self._words is None:
            tally: Counter[str] = Counter()
            for agent in self.agents.values():
                tally.update(agent.name_words | agent.slug_words | agent.text_words)
            self._words = dict(tally)
        return self._words

    def _build_divisions(self) -> dict[str, Division]:
        """Division labels come from the upstream `divisions.json` when present."""
        meta = self._division_metadata()
        counts: dict[str, int] = {}
        for agent in self.agents.values():
            counts[agent.division] = counts.get(agent.division, 0) + 1
        ordered = [key for key in meta if key in counts] + sorted(key for key in counts if key not in meta)
        return {
            key: Division(
                key=key,
                label=str(meta.get(key, {}).get("label") or key.replace("-", " ").title()),
                icon=str(meta.get(key, {}).get("icon") or ""),
                color=str(meta.get(key, {}).get("color") or ""),
                agents=counts[key],
            )
            for key in ordered
        }

    def _division_metadata(self) -> dict[str, dict[str, Any]]:
        path = self.root / "divisions.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Counts and keys come from the directories themselves, so a missing
            # or broken metadata file costs labels and colours, nothing more.
            return {}
        divisions = raw.get("divisions") if isinstance(raw, dict) else None
        if not isinstance(divisions, dict):
            return {}
        return {str(key): value for key, value in divisions.items() if isinstance(value, dict)}

    @staticmethod
    def _score(agent: Agent, tokens: list[str], phrase: str) -> int:
        """Whole-word scoring: where a query word appears decides what it is worth.

        Matching is against indexed word sets, not substrings. Substring
        matching over 279 personas finds "unity" inside "opportunity" and
        "test" inside "latest", and those false hits outnumber the real ones.
        """
        if phrase in {agent.slug, agent.name.lower()}:
            return WEIGHT_EXACT
        score = 0
        matched = 0
        for token in tokens:
            hit = 0
            if token in agent.name_words:
                hit += WEIGHT_NAME
            elif token in agent.slug_words:
                hit += WEIGHT_SLUG
            elif _has_prefix(token, agent.name_words | agent.slug_words):
                hit += WEIGHT_PREFIX
            if token in agent.text_words:
                hit += WEIGHT_DESCRIPTION
            if token == agent.division or token in agent.division.split("-"):
                hit += WEIGHT_DIVISION
            if agent.group and token in agent.group.split("-"):
                hit += WEIGHT_GROUP
            score += hit
            matched += 1 if hit else 0
        return score + WEIGHT_COVERAGE * max(0, matched - 1)
