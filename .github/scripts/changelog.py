"""Release notes built from Conventional Commit messages.

A release's notes are the commits since the previous release tag, grouped by
the type prefix of their subject line:

    feat(store): keep history per session      -> Features
    fix: tolerate an empty config form         -> Bug fixes
    refactor(web)!: rename the page route      -> Breaking changes + Refactoring

A ``!`` before the colon, or a ``BREAKING CHANGE:`` footer in the body, lists
the commit under Breaking changes as well. Subjects without a recognised prefix
land under Other changes rather than disappearing, so an unconventional commit
is still visible in the release it shipped in.

    python .github/scripts/changelog.py                     # notes for HEAD vs. the last release
    python .github/scripts/changelog.py --tag v1.2.0        # heading and range for that version
    python .github/scripts/changelog.py --output notes.md   # write instead of print
    python .github/scripts/changelog.py --prepend CHANGELOG.md

The release workflow runs it before the tag exists, so "the previous release" is
the newest ``v*`` tag reachable from HEAD. A stable version is compared against
the previous *stable* tag, so 1.2.0's notes include everything its release
candidates already shipped; a prerelease is compared against any earlier tag.
Only the standard library is used, so it runs before dependencies are installed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - only ever runs git with fixed arguments
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Order here is the order sections appear in. Types missing from this table
# (chore, ci, build, style, test by default) are left out of the notes: they do
# not change what a user installs. Add them here to publish them.
SECTIONS: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "revert": "Reverts",
}
HIDDEN_TYPES = {"chore", "ci", "build", "style", "test", "tests"}
BREAKING_TITLE = "Breaking changes"
OTHER_TITLE = "Other changes"

SUBJECT_PATTERN = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<text>.+)$")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:\s*(?P<text>.+)", re.MULTILINE)
PRERELEASE_PATTERN = re.compile(r"(?:a|b|rc)\d+$")
# Field and record separators that cannot appear in a commit message.
FIELD, RECORD = "\x1f", "\x1e"


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


@dataclass(frozen=True)
class Entry:
    kind: str | None
    scope: str | None
    text: str
    sha: str
    breaking: str | None


def git(*arguments: str) -> str:
    result = subprocess.run(  # nosec B603 B607 - git from PATH with fixed arguments
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def is_prerelease(tag: str) -> bool:
    return bool(PRERELEASE_PATTERN.search(tag))


def previous_tag(current: str | None) -> str | None:
    """Newest release tag reachable from HEAD that the current release follows."""
    stable_only = current is not None and not is_prerelease(current)
    candidates = [
        tag
        for tag in git("tag", "--merged", "HEAD", "--list", "v*").split()
        if tag != current and not (stable_only and is_prerelease(tag))
    ]
    # Nearest in history, not newest by date: tags cut in the same second, or
    # re-created later, would otherwise pick the wrong starting point.
    return min(candidates, key=lambda tag: int(git("rev-list", "--count", f"{tag}..HEAD")), default=None)


def read_commits(since: str | None) -> list[Commit]:
    revision = f"{since}..HEAD" if since else "HEAD"
    raw = git("log", "--no-merges", f"--format=%H{FIELD}%s{FIELD}%b{RECORD}", revision)
    commits = []
    for chunk in raw.split(RECORD):
        record = chunk.strip("\n")
        if not record:
            continue
        sha, subject, body = [*record.split(FIELD), "", ""][:3]
        commits.append(Commit(sha=sha, subject=subject.strip(), body=body.strip()))
    return commits


def parse(commit: Commit) -> Entry | None:
    """Classify one commit; None means it is deliberately left out of the notes."""
    footer = BREAKING_FOOTER.search(commit.body)
    match = SUBJECT_PATTERN.match(commit.subject)
    if not match:
        return Entry(None, None, commit.subject, commit.sha, footer.group("text").strip() if footer else None)
    kind = match.group("type").lower()
    breaking = footer.group("text").strip() if footer else (match.group("text") if match.group("breaking") else None)
    if kind in HIDDEN_TYPES and not breaking:
        return None
    scope = (match.group("scope") or "").strip() or None
    return Entry(kind, scope, match.group("text").strip(), commit.sha, breaking)


def repository_url() -> str | None:
    """Commit and compare links; only known for sure inside GitHub Actions."""
    server, repository = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
    return f"{server}/{repository}" if server and repository else None


def format_line(entry: Entry, text: str, url: str | None) -> str:
    scope = f"**{entry.scope}:** " if entry.scope else ""
    short = entry.sha[:7]
    link = f"[`{short}`]({url}/commit/{entry.sha})" if url else f"`{short}`"
    return f"- {scope}{text} ({link})"


def render(entries: list[Entry], tag: str | None, since: str | None, url: str | None) -> str:
    lines: list[str] = []
    if tag:
        lines += [f"## {tag} ({date.today().isoformat()})", ""]

    breaking = [entry for entry in entries if entry.breaking]
    if breaking:
        lines += [f"### {BREAKING_TITLE}", ""]
        lines += [format_line(entry, entry.breaking or entry.text, url) for entry in breaking]
        lines.append("")

    for kind, title in SECTIONS.items():
        group = [entry for entry in entries if entry.kind == kind]
        if group:
            lines += [f"### {title}", ""]
            lines += [format_line(entry, entry.text, url) for entry in group]
            lines.append("")

    others = [entry for entry in entries if entry.kind not in SECTIONS and entry.kind not in HIDDEN_TYPES]
    if others:
        lines += [f"### {OTHER_TITLE}", ""]
        lines += [format_line(entry, entry.text, url) for entry in others]
        lines.append("")

    if not any(line.startswith("### ") for line in lines):
        lines += ["No user-facing changes.", ""]

    if url and since and tag:
        lines += [f"**Full diff**: {url}/compare/{since}...{tag}", ""]
    return "\n".join(lines).rstrip() + "\n"


def build(tag: str | None) -> str:
    since = previous_tag(tag)
    entries = [entry for entry in map(parse, read_commits(since)) if entry is not None]
    return render(entries, tag, since, repository_url())


def prepend(path: Path, notes: str) -> None:
    """Insert the notes above the newest entry, keeping any title paragraph on top."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Changelog\n"
    marker = existing.find("\n## ")
    head, tail = (existing, "") if marker < 0 else (existing[: marker + 1], existing[marker + 1 :])
    path.write_text(f"{head.rstrip()}\n\n{notes}\n{tail}".rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", help="the release being described, e.g. v1.2.0 (omit for unreleased changes)")
    parser.add_argument("--output", type=Path, help="write the notes to this file instead of stdout")
    parser.add_argument("--prepend", type=Path, help="also insert the notes at the top of this changelog file")
    arguments = parser.parse_args()

    notes = build(arguments.tag)
    if arguments.output:
        arguments.output.write_text(notes, encoding="utf-8")
    if arguments.prepend:
        prepend(arguments.prepend, notes)
    if not arguments.output:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(notes)


if __name__ == "__main__":
    main()
