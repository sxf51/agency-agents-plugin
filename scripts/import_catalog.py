"""Re-import the persona catalog from a checkout of the upstream repository.

    git clone --depth 1 https://github.com/msitarzewski/agency-agents.git /tmp/agency-agents
    uv run python scripts/import_catalog.py /tmp/agency-agents
    uv run pytest

What it does, and nothing else:

* walks the divisions named in the upstream `divisions.json`
* copies every `*.md` that opens with a `---` frontmatter fence, keeping the
  upstream directory layout so nested subgroups survive
* refreshes `divisions.json`, `LICENSE` and the commit and counts recorded in
  `catalog/UPSTREAM.md`

It never touches the plugin's own code. Personas that lost their frontmatter
upstream are reported and skipped rather than imported as prompts with no name,
which is what the roster index would have to throw away later anyway.

The plugin's own tests pin the expected persona count, so an import that quietly
halves the roster fails there rather than in production.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404 - used only to read the source checkout's own git metadata
import sys
from datetime import UTC, datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CATALOG_DIR = PLUGIN_DIR / "catalog"
NOTICE = CATALOG_DIR / "UPSTREAM.md"
# Upstream directories that hold no agent frontmatter: generated output, shell
# installers, playbooks and worked examples. `divisions.json` is the authority on
# what is a division, so this is only a guard for a malformed one.
NOT_DIVISIONS = frozenset({"scripts", "integrations", "strategy", "examples", ".git", ".github"})


def _git(source: Path, *args: str) -> str:
    """Read one value out of the source checkout's git metadata."""
    executable = shutil.which("git")
    if executable is None:
        return ""
    try:
        completed = subprocess.run(  # nosec B603 - fixed argument list, resolved executable
            [executable, "-C", str(source), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def divisions_of(source: Path) -> list[str]:
    """List the upstream divisions, from the file upstream calls the source of truth."""
    raw = json.loads((source / "divisions.json").read_text(encoding="utf-8"))
    declared = raw.get("divisions") if isinstance(raw, dict) else None
    if not isinstance(declared, dict) or not declared:
        msg = f"{source / 'divisions.json'} declares no divisions"
        raise SystemExit(msg)
    return [key for key in declared if key not in NOT_DIVISIONS]


def import_division(source: Path, division: str, *, dry_run: bool) -> tuple[int, list[str]]:
    """Copy one division's persona files, returning the count and what was skipped."""
    kept = 0
    skipped: list[str] = []
    for path in sorted((source / division).rglob("*.md")):
        relative = path.relative_to(source)
        if not path.read_text(encoding="utf-8", errors="replace").startswith("---"):
            skipped.append(relative.as_posix())
            continue
        kept += 1
        if dry_run:
            continue
        target = CATALOG_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return kept, skipped


def update_notice(agents: int, divisions: int, commit: str, committed: str) -> None:
    """Rewrite the facts recorded in `catalog/UPSTREAM.md`, leaving its prose alone."""
    if not NOTICE.is_file():
        return
    text = NOTICE.read_text(encoding="utf-8")
    today = datetime.now(UTC).date().isoformat()
    # `\g<1>` rather than `\1`: a replacement that continues with a digit, as a
    # date or a count does, would otherwise be read as a reference to group 12.
    replacements = (
        (r"(\| Commit \| )`[^`]*`", rf"\g<1>`{commit}`"),
        (r"(\| Committed \| )[^|\n]*", rf"\g<1>{committed or 'unknown'} "),
        (r"(\| Imported \| )[^|\n]*", rf"\g<1>{today} "),
        (r"(\| Agents \| )[^|\n]*", rf"\g<1>{agents} markdown personas across {divisions} divisions "),
        (r"\b\d+ markdown personas\b", f"{agents} markdown personas"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, count=1)
    NOTICE.write_text(text, encoding="utf-8")


def main() -> int:
    """Import the catalog and report what changed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a checkout of msitarzewski/agency-agents")
    parser.add_argument("--dry-run", action="store_true", help="report what would be imported and stop")
    parser.add_argument("--keep", action="store_true", help="merge into the existing catalog instead of replacing it")
    args = parser.parse_args()

    source: Path = args.source.expanduser().resolve()
    if not (source / "divisions.json").is_file():
        print(f"{source} does not look like the agency-agents repository (no divisions.json)")
        return 1

    divisions = divisions_of(source)
    if not args.dry_run and not args.keep:
        # A division renamed or dropped upstream should disappear here too;
        # merging would leave its personas behind as phantom entries.
        for existing in CATALOG_DIR.glob("*"):
            if existing.is_dir():
                shutil.rmtree(existing)

    total = 0
    for division in divisions:
        kept, skipped = import_division(source, division, dry_run=args.dry_run)
        total += kept
        note = f"  ({len(skipped)} without frontmatter)" if skipped else ""
        print(f"{division:22s} {kept:4d}{note}")
        for entry in skipped:
            print(f"    skipped {entry}")

    if not args.dry_run:
        for name in ("divisions.json", "LICENSE"):
            if (source / name).is_file():
                shutil.copy2(source / name, CATALOG_DIR / name)
        update_notice(total, len(divisions), _git(source, "rev-parse", "HEAD"), _git(source, "log", "-1", "--format=%cs"))

    print(f"\n{total} personas across {len(divisions)} divisions{' (dry run)' if args.dry_run else ''}")
    print("Run `uv run pytest` next: the expected persona count is pinned there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
