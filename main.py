"""Run this plugin without the dashboard.

A plugin normally only comes alive inside the host, which makes trying one out a
slow loop: start the backend, enable the plugin, click through the console. This
loads the plugin on its own and lets you poke at every part of it.

    uv run python main.py inspect                     what this plugin registers
    uv run python main.py doctor                      the checks, with fixes
    uv run python main.py call <tool> '<json>'        run a tool through the executor
    uv run python main.py hook <point> '<json>'       fire one lifecycle hook
    uv run python main.py web <METHOD> <endpoint>     call an endpoint
    uv run python main.py command '/thing some text'  route a slash command
    uv run python main.py serve                       open the page in a browser
    uv run python main.py rename <new-plugin-name>    rename a copied plugin

It uses the project's own PluginManager when the project imports, and the
stand-ins in `tests/harness/` otherwise - the same choice `pytest` makes, so
both always agree about what is being exercised.

Nothing here names this plugin's tools or endpoints, so it copies over unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent

# The harness lives under tests/. Only that directory goes on the path: the
# plugin root must not, because `tools.py` there would shadow the project's own
# `tools` package and `main.py` would shadow the project's entry point.
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

from harness import checks, devserver, report, select_host  # noqa: E402
from harness import rename as rename_module  # noqa: E402


def _load(storage_root: Path) -> tuple[Any, Any]:
    host = select_host(PLUGIN_DIR)
    return host, host.load(PLUGIN_DIR, storage_root)


def _payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("payload must be a JSON object")
    return parsed


def _show(value: Any) -> None:
    """Print a result, unwrapping whichever response shape produced it."""
    if isinstance(value, dict | list):
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return
    content = getattr(value, "content", None)
    if content is not None:
        status = getattr(value, "status_code", 200)
        print(f"HTTP {status}")
        print(json.dumps(content, indent=2, ensure_ascii=False, default=str))
        return
    path = getattr(value, "path", "")
    if path:
        print(f"file: {path} ({Path(path).stat().st_size} bytes, {getattr(value, 'media_type', '')})")
        return
    if getattr(value, "generator", None) is not None:
        print(f"stream: {getattr(value, 'media_type', '')} (use `serve` to watch it)")
        return
    body = bytes(getattr(value, "body", b""))
    print(f"{getattr(value, 'media_type', 'bytes')}: {len(body)} bytes")
    if body[:1] in (b"{", b"[", b"<"):
        print(body[:400].decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    _ = args
    print(report.render(report.inventory(PLUGIN_DIR, host, bundle)))
    return 0


def cmd_doctor(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    _ = args
    context = checks.CheckContext(plugin_dir=PLUGIN_DIR, host=host, bundle=bundle)
    grouped = checks.run_all(context)
    print(report.render_findings(grouped))
    if not host.is_real:
        print("\nRunning against the stand-in host; the project's static review was not part of this.")
    return 1 if any(grouped.values()) else 0


def cmd_call(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    _ = host
    # `tools` is a dict on both backends; `has()` exists only on the stand-in's
    # registry, so asking for it breaks `call` against the real host.
    if args.tool not in bundle.tools.tools:
        print(f"{args.tool} is not registered. Known: {', '.join(sorted(bundle.tools.tools))}")
        return 1
    payload = {"actor_id": args.actor, **_payload(args.payload)}
    # Through the executor, so before_tool_call and after_tool_call both fire.
    _show(asyncio.run(bundle.executor.execute(args.tool, payload, trace_id="main.py")))
    return 0


def cmd_hook(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    _ = host
    if args.point not in checks.HOOK_POINTS:
        print(f"{args.point} is not a hook point. Known: {', '.join(sorted(checks.HOOK_POINTS))}")
        return 1
    _show(asyncio.run(bundle.hooks.trigger(args.point, _payload(args.payload))))
    return 0


def cmd_web(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    body = _payload(args.payload)
    kwargs: dict[str, Any] = {"username": args.actor}
    if args.method.upper() in ("GET", "DELETE"):
        kwargs["query"] = {key: str(value) for key, value in body.items()}
    elif body:
        kwargs["body"] = body
    _show(asyncio.run(host.call_web(bundle, args.endpoint, args.method.upper(), **kwargs)))
    return 0


def cmd_command(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    """Route a slash command the way the runtime would.

    First the inbound hook gets a chance to answer it outright, then the
    manifest decides whether it reaches a tool or the planner.
    """
    text = _normalise_command(args.text)
    outcome = asyncio.run(bundle.hooks.trigger("before_route", {"message": {"text": text}}))
    route_outcome = outcome.get("route_outcome")
    if isinstance(route_outcome, dict) and route_outcome.get("action") in ("respond", "drop"):
        print(f"before_route: {route_outcome['action']}")
        if route_outcome.get("response"):
            print(route_outcome["response"])
        return 0

    name = text.split(" ", maxsplit=1)[0].lower()
    spec = host.collect_commands(bundle).get(name)
    if spec is None:
        print(f"{name} is not a command this plugin publishes, and no hook claimed it.")
        print("Run `doctor` if you expected it to be: the runtime drops malformed commands silently.")
        return 1

    rest = text[len(name) :].strip()
    if str(spec.get("route")) == "tool":
        payload = {"actor_id": args.actor, "text": rest, "query": rest, **outcome.get("payload", {})}
        _show(asyncio.run(bundle.executor.execute(str(spec["tool"]), payload, trace_id="main.py")))
        return 0

    hints = spec.get("planner_hints") or {}
    target = hints.get("preferred_subagent")
    if not target:
        print(f"{name} routes to the planner with no preferred subagent; nothing to run here.")
        return 0
    agent = bundle.subagents.get(str(target))
    if agent is None:
        print(f"{name} prefers subagent {target!r}, which is not registered.")
        return 1
    _show(asyncio.run(agent.run({"query": rest, "actor_id": args.actor, "trace_id": "main.py"}, {}, {})))
    return 0


def _normalise_command(raw: str) -> str:
    """Accept `/thing`, `thing`, and what Git Bash does to a leading slash.

    MSYS rewrites a bare `/agency-help` argument into `C:/Git/agency-help`
    before Python ever sees it, so the command name is taken from the last path
    segment. A command name can never contain a slash, so nothing is lost.
    """
    text = raw.strip()
    if not text:
        return text
    head, _, rest = text.partition(" ")
    name = head.rsplit("/", maxsplit=1)[-1]
    return f"/{name} {rest}".rstrip() if name else text


def cmd_serve(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    pages = [page.name for page in bundle.plugin.ui.pages]
    if not pages:
        print("This plugin declares no ui.pages, so there is nothing to serve.")
        return 1
    page = args.page or pages[0]
    if page not in pages:
        print(f"{page!r} is not declared. Available: {', '.join(pages)}")
        return 1

    url = devserver.serve(
        plugin_dir=PLUGIN_DIR,
        page=page,
        host=host,
        bundle=bundle,
        port=args.port,
        locale=args.locale,
        dark=args.dark,
    )
    print(f"serving pages/{page}/ at {url}")
    print("Every bridge call goes to this plugin's own handlers. Ctrl-C to stop.")
    print("In the browser console, window.__setContext({locale:'zh', isDark:true}) flips language and theme.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_rename(args: argparse.Namespace, host: Any, bundle: Any) -> int:
    _ = host
    # The bundle supplies the exact names this plugin registered, so nothing
    # that merely looks like one gets rewritten.
    plan = rename_module.plan(PLUGIN_DIR, args.new_name, bundle)
    if plan.old_plugin == plan.new_plugin:
        print(f"already named {plan.new_plugin}")
        return 0

    print(f"renaming {len(plan.renames)} name(s):")
    for old, new in sorted(plan.renames.items()):
        print(f"  {old}  ->  {new}")
    print()
    touched = rename_module.apply(PLUGIN_DIR, plan, dry_run=args.dry_run)
    for path, count in touched:
        print(f"  {count:>4}  {path}")
    moved = rename_module.rename_test_file(PLUGIN_DIR, plan, dry_run=args.dry_run)
    if moved:
        print(f"  moved tests -> {moved}")
    print()
    if args.dry_run:
        print(f"{len(touched)} file(s) would change. Drop --dry-run to apply.")
        return 0
    print(f"{len(touched)} file(s) changed. Rename the directory to {plan.new_plugin} as well, then run `doctor`.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description=__doc__.split("\n\n")[1])
    parser.add_argument("--actor", default="dev", help="caller identity handed to tools and endpoints")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", help="print what this plugin registers")
    sub.add_parser("doctor", help="run every generic check and explain what to fix")

    call = sub.add_parser("call", help="run one tool through the executor")
    call.add_argument("tool")
    call.add_argument("payload", nargs="?", help="JSON object")

    hook = sub.add_parser("hook", help="fire one lifecycle hook")
    hook.add_argument("point")
    hook.add_argument("payload", nargs="?", help="JSON object for the hook context")

    web = sub.add_parser("web", help="call one of this plugin's endpoints")
    web.add_argument("method")
    web.add_argument("endpoint")
    web.add_argument("payload", nargs="?", help="JSON object: query for GET/DELETE, body otherwise")

    command = sub.add_parser("command", help="route a slash command")
    command.add_argument("text")

    serve = sub.add_parser("serve", help="serve a page in a browser against real endpoints")
    serve.add_argument("--page", default="", help="which ui.pages entry; the first by default")
    serve.add_argument("--port", type=int, default=8800)
    serve.add_argument("--locale", default="en")
    serve.add_argument("--dark", action="store_true", help="start in the dark theme")

    rename = sub.add_parser("rename", help="rename a copied plugin in one pass")
    rename.add_argument("new_name")
    rename.add_argument("--dry-run", action="store_true")

    return parser


HANDLERS = {
    "inspect": cmd_inspect,
    "doctor": cmd_doctor,
    "call": cmd_call,
    "hook": cmd_hook,
    "web": cmd_web,
    "command": cmd_command,
    "serve": cmd_serve,
    "rename": cmd_rename,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Storage lands in a temp directory: running the CLI should never leave
    # anything behind in the plugin, and never touch the host's data root.
    storage_root = Path(tempfile.mkdtemp(prefix="plugin-main-"))
    try:
        host, bundle = _load(storage_root)
        if not host.is_real and args.command in ("inspect", "doctor"):
            print("(stand-in host: the project is not importable here)\n")
        return HANDLERS[args.command](args, host, bundle)
    finally:
        shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
